"""Render preflight. Default is OFFLINE and never requests a signal or order.
--live checks Telegram + Mini App bootstrap only.
--live --request-signal consumes ONE provider signal and MUST use dry-run=1.
No Pocket Option connection, MongoDB write or Telegram interactive login here.
"""
import argparse
import asyncio
import os
import sys

import config
from diagnostics import SafeIssue, diagnose_exception
from meta_api_client import MetaHTTPSettings
from telegram_bootstrap import session_from_env, signal_username


def offline():
    settings = MetaHTTPSettings()
    backend = os.getenv('META_SIGNAL_BACKEND','auto').strip().lower()
    if backend not in ('auto','http','browser','telegram'):
        raise SafeIssue('META_API_CONFIG')
    username = signal_username(config.SIGNAL_BOT_USERNAME)
    print('META_PREFLIGHT: configuration valid')
    print(f'backend={backend}; api_origin_verified_by_launch=required; dry_run={int(settings.dry_run)}')
    print(f'bot_username=@{username}; bot_id={settings.bot_id}')
    print(f'poll_seconds={settings.poll}; http_timeout={settings.timeout}; operation_timeout={settings.operation_timeout}')
    if backend=='http' and username.lower()!='signal_meta_bot':
        print('WARNING: expected SIGNAL_BOT_USERNAME=@signal_meta_bot for the captured provider.')
    print('No network request, signal request, database write or trading order was made.')
    print('Existing MongoDB trading duration is NOT overridden by DURATION_SECONDS; select M1/60 in the control bot.')
    return settings


async def live(args, settings):
    if os.getenv('META_SIGNAL_BACKEND','auto').strip().lower() not in ('auto','http'):
        raise SafeIssue('META_API_CONFIG')
    if args.request_signal and not settings.dry_run:
        raise SafeIssue('META_API_CONFIG')
    if not config.TELEGRAM_API_ID or not config.TELEGRAM_API_HASH:
        raise SafeIssue('TG_API_INVALID')
    from telethon import TelegramClient
    from synchronized_signals import SynchronizedSignalListener
    client = TelegramClient(session_from_env(config.TELEGRAM_STRING_SESSION),
                            config.TELEGRAM_API_ID,config.TELEGRAM_API_HASH)
    listener = None
    try:
        await asyncio.wait_for(client.connect(),config.CONNECT_TIMEOUT_SECONDS)
        if not await client.is_user_authorized(): raise SafeIssue('TG_SESSION_UNAUTHORIZED')
        listener = SynchronizedSignalListener(client)
        await listener.init()
        await listener.nav.ensure()
        web = listener.nav.web
        if web is None or not hasattr(web,'settings'):
            raise SafeIssue('META_API_NAVIGATION')
        print('META_PREFLIGHT: authorized Mini App bootstrap succeeded; no order was sent.')
        print(f'current_screen={web.screen_name()}; allowed_timeframes={web.profile.get("allowed_timeframes",[])}')
        if args.request_signal:
            await listener.next_direction(args.asset,args.duration)
    finally:
        if listener: await listener.close()
        await client.disconnect()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true')
    parser.add_argument('--request-signal',action='store_true')
    parser.add_argument('--asset',default='AUDCAD_otc')
    parser.add_argument('--duration',type=int,default=60)
    args = parser.parse_args()
    if args.request_signal and not args.live:
        parser.error('--request-signal requires --live and META_DRY_RUN=1')
    try:
        settings = offline()
        if args.live: asyncio.run(live(args,settings))
    except SafeIssue as exc:
        if exc.code=='META_DRY_RUN_COMPLETE':
            print('META_DRY_RUN_COMPLETE: exactly one provider result read; ZERO trading orders sent.')
            return 0
        print(diagnose_exception(exc,'meta_preflight').public_text())
        return 1
    except Exception as exc:
        # Never print raw exception strings or stack frames with request locals.
        print(diagnose_exception(exc,'meta_preflight').public_text())
        return 1
    return 0


if __name__=='__main__':
    sys.exit(main())
