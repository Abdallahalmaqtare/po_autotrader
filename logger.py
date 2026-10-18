"""
logger.py - Console logging with millisecond-precision timestamps (UTC+3 like the signal bot).
"""
from datetime import datetime, timezone, timedelta

TZ_PLUS3 = timezone(timedelta(hours=3))  # matches the bot's "Current time (UTC+3)"


def log(msg: str):
    now = datetime.now(TZ_PLUS3).strftime("%H:%M:%S.%f")[:-3]  # HH:MM:SS.mmm
    print(f"[{now}] {msg}", flush=True)
