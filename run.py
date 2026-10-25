"""Render entry point: bind health port FIRST, then Mongo lease and Telegram polling."""
import asyncio
from runtime_support import timestamp_print as print
import os
import logging
import signal
import time
from contextlib import suppress
from uuid import uuid4
from aiohttp import web
from aiogram import Bot
import config
from database import Repository
from worker import TradingWorker
from control_bot import build_dispatcher
from runtime_support import telegram_startup, poll_controller
from hosting_health import public_health_url,heartbeat_interval,keep_alive_loop


def health_application(state):
    app = web.Application()

    async def root(request):
        return web.json_response({'service': 'po_autotrader', 'status': state.get('phase', 'starting')})

    async def health(request):
        # Liveness endpoint deliberately permits a standby during rolling deploy:
        # new instance must pass health before Render stops the old lease owner.
        status = 503 if state.get('phase') == 'stopping' else 200
        return web.json_response({'alive': status == 200,'phase':state.get('phase'),
                                  'keepalive':state.get('keepalive',{'enabled':False})}, status=status)

    async def ready(request):
        ready = state.get('ready', False)
        return web.json_response({'ready': ready}, status=200 if ready else 503)

    app.router.add_get('/', root)
    app.router.add_get('/health', health)
    app.router.add_get('/ready', ready)
    return app


async def keep_alive(public_url,shutdown,metrics=None):
    """Compatibility entry point with supervision independent of the trader."""
    metrics=metrics if metrics is not None else {}
    url=public_health_url({'RENDER_EXTERNAL_URL':public_url})
    if not url:
        metrics.update(enabled=False,last_error='public_url_missing');return
    while not shutdown.is_set():
        try:
            await keep_alive_loop(url,shutdown,metrics,interval=heartbeat_interval(os.environ))
        except asyncio.CancelledError:raise
        except Exception as exc:
            metrics.update(last_error=type(exc).__name__)
            print('[HOST] health worker restart:',type(exc).__name__,flush=True)
            await wait_or_shutdown(shutdown,30)


async def wait_or_shutdown(event, seconds):
    try:
        await asyncio.wait_for(event.wait(), seconds)
    except asyncio.TimeoutError:
        pass


async def main():
    logging.basicConfig(level=logging.WARNING, format='[%(asctime)s.%(msecs)03d] %(levelname)s %(name)s: %(message)s',datefmt='%H:%M:%S')
    state = {'phase': 'starting', 'ready': False}
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, shutdown.set)
    runner = web.AppRunner(health_application(state), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', config.PORT).start()
    print('[APP] build=v24-v20-results-account-controls', flush=True)
    print('[APP] result_engine=v20-original; send/result methods restored from attached v20',flush=True)
    print(f'[APP] health server bound port={config.PORT}; starting dependencies', flush=True)
    repo = None
    worker = None
    bot = None
    polling = None
    heartbeat = None
    stop_waiter = None
    keepalive = None
    metrics=state.setdefault('keepalive',{'enabled':False})
    try:
        try:health_url=public_health_url(os.environ)
        except ValueError:
            health_url=None;metrics.update(enabled=False,last_error='invalid_public_url')
            print('[HOST] invalid public URL; health worker disabled, trading service unaffected',flush=True)
        if health_url:
            # Start as soon as the public health port is bound, not after DB/TG.
            keepalive=asyncio.create_task(keep_alive(health_url[:-len('/health')],shutdown,metrics))
            print('[HOST] public health worker enabled; first ping immediate',flush=True)
        else:
            metrics.update(enabled=False,last_error=metrics.get('last_error') or 'public_url_missing')
            print('[HOST] no public URL; set RENDER_EXTERNAL_URL or APP_PUBLIC_URL',flush=True)
        config.validate_startup()
        repo = Repository(config.MONGO_URI, config.DB_NAME)
        # DB first-load retry is bounded per operation, cancellable during shutdown.
        while not shutdown.is_set():
            try:
                await repo.initialize()
                if await repo.acquire():
                    break
                state['phase'] = 'standby'
                print('[APP] standby: previous deployment owns lease; health ready, trading/polling not started', flush=True)
            except Exception as e:
                state['phase'] = 'database_retry'
                print('[APP] database not ready:', type(e).__name__, flush=True)
            await wait_or_shutdown(shutdown, 3)
        if shutdown.is_set():
            return
        # Auto-reseed placeholder account credentials from env (first deploy may
        # have seeded MongoDB from template values; a real env value wins then).
        def _bad(v):
            v = (v or '').replace(' ', '')
            return (not v) or '...' in v or '"uid":0' in v
        for _key, _envkey in (('real_account', 'PO_SSID_REAL'),
                              ('demo_account', 'PO_SSID_DEMO')):
            _env_val = os.getenv(_envkey, '').strip().strip("'\"")
            if _bad(repo.doc[_key].get('ssid')) and _env_val and not _bad(_env_val):
                try:
                    await repo.update({_key + '.ssid': _env_val})
                    print(f'[APP] seeded {_key} credentials from environment', flush=True)
                except Exception:
                    pass

        worker = TradingWorker(repo)
        bot = Bot(token=config.CONTROL_BOT_TOKEN)
        dp = build_dispatcher(repo, worker, bot)

        async def heartbeat_loop():
            while not shutdown.is_set():
                await wait_or_shutdown(shutdown, 8)
                if shutdown.is_set():
                    break
                try:
                    await repo.renew()
                except Exception:
                    repo.leader = False
                    state['ready'] = False
                    worker.request_stop()
                    print('[APP] database lease unavailable; disabling entries and shutting down', flush=True)
                    shutdown.set()
                    break

        heartbeat = asyncio.create_task(heartbeat_loop())
        # No inbound webhook, single long-polling owner. Do not drop updates.
        await telegram_startup(lambda:bot.delete_webhook(drop_pending_updates=False),
                               shutdown,'delete webhook')
        if shutdown.is_set():return
        polling = asyncio.create_task(poll_controller(dp,bot,shutdown))
        stop_waiter = asyncio.create_task(shutdown.wait())
        state.update(phase='ready', ready=True)
        print('[APP] controller ready; trading remains OFF until administrator presses Start', flush=True)
        done, _ = await asyncio.wait([polling, stop_waiter], return_when=asyncio.FIRST_COMPLETED)
        if polling in done and not shutdown.is_set():
            # Re-raise so Render sees an unsuccessful exit, without raw exception text.
            error = polling.exception()
            print('[APP] polling ended:', type(error).__name__ if error else 'completed', flush=True)
            worker.request_stop()
            raise RuntimeError('Controller polling ended unexpectedly') from None
    except Exception as e:
        print('[APP] startup failed:', type(e).__name__, flush=True)
        # Config error messages contain names only; safe to show required env keys.
        if isinstance(e, ValueError) and str(e).startswith('Missing variables:'):
            print(str(e), flush=True)
        raise RuntimeError('Application startup failed; check required environment variables and credentials') from None
    finally:
        state.update(phase='stopping', ready=False)
        shutdown.set()
        if worker:
            await worker.close()
        for task in (polling, heartbeat, stop_waiter, keepalive):
            if task is not None and not task.done():
                task.cancel()
            if task:
                with suppress(asyncio.CancelledError, Exception):
                    await task
        if bot:
            await bot.session.close()
        if repo:
            if repo.leader:
                with suppress(Exception):
                    await repo.release()
            await repo.close()
        await runner.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
