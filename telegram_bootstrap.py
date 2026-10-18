"""Validated Telethon input normalization. Never repair/invent session auth keys."""
import re
from urllib.parse import urlparse, unquote
from telethon.sessions import StringSession
from diagnostics import SafeIssue


def session_from_env(raw):
    value = str(raw or '').strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('\"', "'"):
        value = value[1:-1].strip()
    prefix = 'TELEGRAM_STRING_SESSION='
    # Accept accidentally pasted dotenv assignment and wrapping quotes/newlines.
    if value.startswith(prefix):
        value = value[len(prefix):].strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('\"', "'"):
        value = value[1:-1].strip()
    value = ''.join(value.split())
    if not value or not value.startswith('1') or not re.fullmatch(r'[A-Za-z0-9_\-=]+', value):
        raise SafeIssue('TG_SESSION_INVALID')
    try:
        session = StringSession(value)
        if not session.auth_key or not session.dc_id or not session.server_address or not session.port:
            raise ValueError
    except Exception:
        raise SafeIssue('TG_SESSION_INVALID') from None
    return session


def signal_username(raw):
    value = str(raw or '').strip().strip('\"\'').strip()
    prefix = 'SIGNAL_BOT_USERNAME='
    if value.startswith(prefix):
        value = value[len(prefix):].strip().strip('\"\'')
    if value.startswith(('https://t.me/', 'http://t.me/', 't.me/')):
        link = urlparse(value if '://' in value else 'https://' + value)
        path = link.path.strip('/').split('/')
        if len(path) != 1:
            raise SafeIssue('SIGNAL_NAME_INVALID')
        value = unquote(path[0])
    value = value.lstrip('@')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{3,31}', value):
        raise SafeIssue('SIGNAL_NAME_INVALID')
    return value
