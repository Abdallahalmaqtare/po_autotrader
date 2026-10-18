"""Cloud configuration. Environment wins over secret/local dotenv files.
No network connections or interactive login on import.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
for file in (os.getenv('ENV_FILE'), '/etc/secrets/.env', ROOT / '.env'):
    if file:
        load_dotenv(file, override=False, interpolate=False)


def number(key, default, integer=False):
    raw = os.getenv(key, str(default)).strip()
    try:
        return int(raw) if integer else float(raw)
    except ValueError:
        raise ValueError(f'Invalid numeric environment variable: {key}') from None

TELEGRAM_API_ID = number('TELEGRAM_API_ID', 0, True)
TELEGRAM_API_HASH = os.getenv('TELEGRAM_API_HASH', '').strip().strip("'\"")
ADMIN_TELEGRAM_ID = number('ADMIN_TELEGRAM_ID', 0, True) if os.getenv('ADMIN_TELEGRAM_ID','').strip() else 0
CONTROL_BOT_TOKEN = os.getenv('CONTROL_BOT_TOKEN', '')
TELEGRAM_SESSION_NAME = os.getenv('TELEGRAM_SESSION_NAME', 'po_session')
TELEGRAM_STRING_SESSION = os.getenv('TELEGRAM_STRING_SESSION', '')
SIGNAL_BOT_USERNAME = os.getenv('SIGNAL_BOT_USERNAME', '@AboodTradingOTCbot').lstrip('@')
NEW_SIGNAL_BUTTON_TEXT = os.getenv('NEW_SIGNAL_BUTTON_TEXT', 'New signal')
MONGO_URI = os.getenv('MONGO_URI', '')
DB_NAME = os.getenv('DB_NAME', 'abood_trading_db')
PORT = number('PORT', 10000, True)
DEMO_URL = 'wss://demo-api-eu.po.market/socket.io/?EIO=4&transport=websocket'
REAL_URL = 'wss://api-eu.po.market/socket.io/?EIO=4&transport=websocket'
ACCOUNT_TYPE = os.getenv('ACCOUNT_TYPE', 'PRACTICE').upper()
IS_DEMO = 1 if ACCOUNT_TYPE == 'PRACTICE' else 0
ASSET = os.getenv('ASSET', 'EURUSD_otc')
TRADE_AMOUNT = number('TRADE_AMOUNT', 1)
DURATION_SECONDS = number('DURATION_SECONDS', 5, True)
ENTRY_DELAY_SECONDS = number('ENTRY_DELAY_SECONDS', .8)
MARTINGALE_STEPS = number('MARTINGALE_STEPS', 4, True)
LOOP_DELAY_SECONDS = number('LOOP_DELAY_SECONDS', 0)
RESULT_TIMEOUT_EXTRA = number('RESULT_TIMEOUT_EXTRA', 2)
BALANCE_SETTLE_SECONDS = number('BALANCE_SETTLE_SECONDS', 1)
LOSS_RECHECK_SECONDS = number('LOSS_RECHECK_SECONDS', 2)
SIGNAL_TIMEOUT_SECONDS = number('SIGNAL_TIMEOUT_SECONDS', 15)
CONNECT_TIMEOUT_SECONDS = number('CONNECT_TIMEOUT_SECONDS', 25)
SHUTDOWN_GRACE_SECONDS = number('SHUTDOWN_GRACE_SECONDS', 20)
# Compatibility only. Cloud account credentials come from MongoDB, never this value.
PO_SSID = ''
PO_WS_URL = DEMO_URL


def validate_startup():
    required = {'TELEGRAM_API_ID': TELEGRAM_API_ID,
                'TELEGRAM_API_HASH': TELEGRAM_API_HASH,
                'ADMIN_TELEGRAM_ID': ADMIN_TELEGRAM_ID,
                'CONTROL_BOT_TOKEN': CONTROL_BOT_TOKEN,
                'TELEGRAM_STRING_SESSION': TELEGRAM_STRING_SESSION,
                'MONGO_URI': MONGO_URI}
    missing = [k for k, v in required.items() if not v]
    if missing:
        raise ValueError('Missing variables: ' + ', '.join(missing))
    if ADMIN_TELEGRAM_ID <= 0 or TELEGRAM_API_ID <= 0:
        raise ValueError('Telegram IDs must be positive')
    import math
    for key, value in [('SIGNAL_TIMEOUT_SECONDS', SIGNAL_TIMEOUT_SECONDS),
                       ('CONNECT_TIMEOUT_SECONDS', CONNECT_TIMEOUT_SECONDS),
                       ('SHUTDOWN_GRACE_SECONDS', SHUTDOWN_GRACE_SECONDS)]:
        if not math.isfinite(value) or value <= 0:
            raise ValueError('Invalid positive timeout: ' + key)
    if not 1 <= PORT <= 65535:
        raise ValueError('Invalid PORT')
    if not MONGO_URI.startswith(('mongodb://', 'mongodb+srv://')):
        raise ValueError('Invalid MONGO_URI scheme')
