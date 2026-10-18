"""LOCAL ONLY: create TELEGRAM_STRING_SESSION without uploading SQLite sessions.
python session_tool.py --from-file po_session.session
python session_tool.py --login
Uses local .env API_ID/API_HASH. Output is a sensitive account credential.
"""
import argparse
import asyncio
from pathlib import Path
from telethon import TelegramClient
from telethon.sessions import StringSession
import config


async def run():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--from-file')
    group.add_argument('--login', action='store_true')
    args = parser.parse_args()
    if config.TELEGRAM_API_ID <= 0 or not config.TELEGRAM_API_HASH:
        raise SystemExit('Set TELEGRAM_API_ID and TELEGRAM_API_HASH in your local .env first')
    if args.from_file and not Path(args.from_file).is_file():
        raise SystemExit('Session file not found. Pass the exact existing .session path.')
    session = args.from_file if args.from_file else StringSession()
    client = TelegramClient(session, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
    try:
        if args.login:
            await client.start()  # interactive only in this local helper
        else:
            await client.connect()
            if not await client.is_user_authorized():
                raise SystemExit('Session is not authorized; use --login locally')
        print('\nKeep the following secret; paste only into Render environment:')
        print('TELEGRAM_STRING_SESSION=' + StringSession.save(client.session))
    finally:
        await client.disconnect()


if __name__ == '__main__':
    asyncio.run(run())
