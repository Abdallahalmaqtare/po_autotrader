"""Small operational layer: timestamped logs and cancellable Bot API startup.

Do NOT retry order sends here. A network failure after a send may mean that the
broker accepted it; retrying could place a second trade. aiogram already retries
getUpdates internally. The supervisor below covers getMe/startup failures only.
"""
import asyncio
import builtins
from datetime import datetime, timezone, timedelta
from aiogram.exceptions import TelegramNetworkError, TelegramServerError, TelegramRetryAfter
from aiogram.utils.backoff import BackoffConfig

TZ = timezone(timedelta(hours=3))
POLL_BACKOFF = BackoffConfig(min_delay=1.0, max_delay=15.0, factor=1.5, jitter=0.1)


def timestamp_print(*args, **kwargs):
    prefix=datetime.now(TZ).strftime('[%H:%M:%S.%f]')[:-4]+']'
    from tenant_scope import current
    scope=current()
    if scope:prefix+=f'[user={scope.uid}]'
    builtins.print(prefix,*args,**kwargs)


async def sleep_or_stop(shutdown, seconds):
    try:await asyncio.wait_for(shutdown.wait(),seconds)
    except asyncio.TimeoutError:pass


async def telegram_startup(operation, shutdown, label, waiter=sleep_or_stop):
    """Retry safe startup requests, never resend trading operations or messages.
    Unauthorized/conflict/config errors propagate. Logs contain no API payload.
    """
    attempt=0
    while not shutdown.is_set():
        try:return await operation()
        except (TelegramNetworkError,TelegramServerError,TelegramRetryAfter,OSError,TimeoutError) as exc:
            attempt+=1
            delay=min(15,2**min(attempt-1,4))
            if isinstance(exc,TelegramRetryAfter):delay=max(delay,exc.retry_after)
            timestamp_print(f'[TG] {label}: {type(exc).__name__}; retry={attempt}; wait={delay}s',flush=True)
            await waiter(shutdown,delay)
    return None


async def poll_controller(dp, bot, shutdown):
    # getUpdates transient failures stay inside aiogram's documented retry loop.
    # Transient getMe errors can escape before that loop starts: retry those here.
    return await telegram_startup(
        lambda:dp.start_polling(bot,handle_signals=False,close_bot_session=False,
                               backoff_config=POLL_BACKOFF),
        shutdown,'controller polling')
