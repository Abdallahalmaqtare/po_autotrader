"""Render entry point: bind health port FIRST, then Mongo lease and Telegram polling."""
import asyncio
import logging
import signal
import time
from contextlib import suppress
from aiohttp import web
from aiogram import Bot
import config
from database import Repository
from worker import TradingWorker
from control_bot import build_dispatcher


def health_application(state):
    app = web.Application()

    async def root(request):
        return web.json_response({'service': 'po_autotrader', 'status': state.get('phase', 'starting')})

    async def health(request):
        # Liveness endpoint deliberately permits a standby during rolling deploy:
        # new instance must pass health before Render stops the old lease owner.
        status = 503 if state.get('phase') == 'stopping' else 200
        return web.json_response({'alive': status == 200}, status=status)

    async def ready(request):
        ready = state.get('ready', False)
        return web.json_response({'ready': ready}, status=200 if ready else 503)

    app.router.add_get('/', root)
    app.router.add_get('/health', health)
    app.router.add_get('/ready', ready)
    return app


async def wait_or_shutdown(event, seconds):
    try:
        await asyncio.wait_for(event.wait(), seconds)
    except asyncio.TimeoutError:
        pass


async def main():
    logging.basicConfig(level=logging.WARNING, format='%(levelname)s %(name)s: %(message)s')
    state = {'phase': 'starting', 'ready': False}
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with suppress(NotImplementedError):
            loop.add_signal_handler(sig, shutdown.set)
    runner = web.AppRunner(health_application(state), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, '0.0.0.0', config.PORT).start()
    print('[APP] build=v12-startup-fix', flush=True)
    print('[APP] health server bound; starting dependencies', flush=True)
    repo = None
    worker = None
    bot = None
    polling = None
    heartbeat = None
    stop_waiter = None
    try:
        config.validate_startup()
        repo = Repository(config.MONGO_URI, config.DB_NAME)
        # DB first-load retry is bounded per operation, cancellable during shutdown.
        while not shutdown.is_set():
            try:
                await repo.initialize()
                if await repo.acquire():
                    break
                state['phase'] = 'standby'
                print('[APP] standby: another instance holds Mongo lease', flush=True)
            except Exception as e:
                state['phase'] = 'database_retry'
                print('[APP] database not ready:', type(e).__name__, flush=True)
            await wait_or_shutdown(shutdown, 3)
        if shutdown.is_set():
            return
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
        await bot.delete_webhook(drop_pending_updates=False)
        polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False,
                                                       close_bot_session=False))
        stop_waiter = asyncio.create_task(shutdown.wait())
        state.update(phase='ready', ready=True)
        print('[APP] controller ready; trading remains OFF until administrator presses Start', flush=True)
        done, _ = await asyncio.wait([polling, stop_waiter], return_when=asyncio.FIRST_COMPLETED)
        if polling in done and not shutdown.is_set():
            # Re-raise so Render sees an unsuccessful exit, without raw exception text.
            error = polling.exception()
            print('[APP] polling ended:', type(error).__name__ if error else 'completed', flush=True)
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
        for task in (polling, heartbeat, stop_waiter):
            if task and not task.done():
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
