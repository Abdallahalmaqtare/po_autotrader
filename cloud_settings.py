"""Validated settings/document model; secrets never included in public displays."""
from copy import deepcopy
import json
import math
import os
from urllib.parse import urlparse
import config
from forex_assets import FOREX_OTC, SYMBOL_NAMES, STORED_ASSET_NAMES
from risk_limits import risk_defaults, validate_risk
from recovery import recovery_defaults, state_defaults, validate_recovery
from payout_guard import payout_defaults, validate_payout
from work_cycle import cycle_defaults, cycle_state, validate_cycle

MODES = ('PRACTICE', 'REAL')
DURATIONS = (5, 15, 30, 60, 180, 300, 600, 900, 1800)


def valid_number(value, lo=0, hi=1000000):
    if isinstance(value, bool):
        raise ValueError('يجب إدخال رقم صالح')
    n = float(value)
    if not math.isfinite(n) or not lo <= n <= hi:
        raise ValueError(f'القيمة يجب أن تكون بين {lo:g} و{hi:g}')
    return n


def valid_url(value):
    value = str(value).strip()
    p = urlparse(value)
    host = (p.hostname or '').lower()
    allowed = any(host == d or host.endswith('.' + d) for d in ('po.market', 'pocketoption.com', 'api-po.market'))
    if p.scheme != 'wss' or not allowed or p.username or p.password or p.fragment:
        raise ValueError('أدخل رابط wss صالحاً على نطاق منصة التداول')
    return value


def auth_frame(value, mode, uid=0):
    """Normalize only account flag; preserve session/UID/platform in full frames.
    Raw session tokens require a real UID, not an invented server-resolved UID.
    """
    value = str(value).strip().strip("'\"")
    if not value or value in ('...', 'your_ssid') or len(value) > 20000:
        raise ValueError('بيانات الجلسة ناقصة أو غير صالحة')
    if value.startswith('42'):
        try:
            arr = json.loads(value[2:])
            if arr[0] != 'auth' or not isinstance(arr[1], dict):
                raise ValueError
            payload = deepcopy(arr[1])
            if not payload.get('session') or payload['session'] == '...':
                raise ValueError
            if int(payload.get('uid', 0)) <= 0:
                raise ValueError
        except (ValueError, TypeError, KeyError, IndexError, json.JSONDecodeError):
            raise ValueError('أرسل إطار auth الكامل وفيه session وuid الفعلي، لا uid=0') from None
    else:
        if not isinstance(uid, int) or uid <= 0:
            raise ValueError('للـ SSID الخام يلزم PO_UID_REAL/DEMO؛ الأسهل إرسال إطار auth الكامل')
        payload = {'session': value, 'uid': uid, 'platform': 2}
    payload['isDemo'] = 0 if mode == 'REAL' else 1
    return '42' + json.dumps(['auth', payload], separators=(',', ':'))


def empty_stats():
    return dict(total_trades=0, won_trades=0, lost_trades=0, draws=0,
                unconfirmed=0, pending_trades=0, net_pnl=0.0,
                total_staked=0.0, total_returned=0.0, financial_trades=0,
                sequence_losses=0, lost_amount=0.0)


def defaults():
    return {
        '_id': 'current_settings', 'schema_version': 1,
        'account_mode': config.ACCOUNT_TYPE,
        'demo_account': {'ssid': os.getenv('PO_SSID_DEMO', ''),
                         'ws_url': os.getenv('PO_WS_URL_DEMO', config.DEMO_URL),
                         'uid': int(os.getenv('PO_UID_DEMO', '0') or 0)},
        'real_account': {'ssid': os.getenv('PO_SSID_REAL', ''),
                         'ws_url': os.getenv('PO_WS_URL_REAL', config.REAL_URL),
                         'uid': int(os.getenv('PO_UID_REAL', '0') or 0)},
        'trading': dict(asset=config.ASSET, trade_amount=config.TRADE_AMOUNT,
                        duration_seconds=config.DURATION_SECONDS,
                        martingale_steps=config.MARTINGALE_STEPS,
                        entry_delay_seconds=config.ENTRY_DELAY_SECONDS,
                        loop_delay_seconds=config.LOOP_DELAY_SECONDS,
                        result_timeout_extra=config.RESULT_TIMEOUT_EXTRA,
                        balance_settle_seconds=config.BALANCE_SETTLE_SECONDS,
                        loss_recheck_seconds=config.LOSS_RECHECK_SECONDS),
        'risk': risk_defaults(),
        'recovery': recovery_defaults(),
        'payout': payout_defaults(),
        'schedule': cycle_defaults(),
        'accounts': {}, 'active_accounts': {'PRACTICE':None,'REAL':None},
        'active_account_id': None, 'trade_ledger': [],
        'session_stats': empty_stats(),
        'account_stats': {m: empty_stats() for m in MODES},
        'runtime': {'pending': None, 'martingale_step': 0, 'cycle': cycle_state(),
                    'recovery': {m: state_defaults() for m in MODES}},
    }


def validate_document(doc):
    if doc.get('account_mode') not in MODES:
        raise ValueError('نوع حساب غير صالح')
    validate_risk(doc.get('risk', risk_defaults()))
    validate_payout(doc.get('payout', payout_defaults()))
    validate_cycle(doc.get('schedule', cycle_defaults()))
    t = doc['trading']
    if t.get('asset') not in STORED_ASSET_NAMES:
        raise ValueError('يُسمح بأزواج فوركس OTC الموجودة في القائمة فقط')
    valid_number(t['trade_amount'], .01, 100000)
    validate_recovery(doc.get('recovery', recovery_defaults()), t['trade_amount'])
    if t['duration_seconds'] not in DURATIONS:
        raise ValueError('مدة غير مدعومة في قائمة البوت')
    if isinstance(t['martingale_steps'], bool) or t['martingale_steps'] not in range(5):
        raise ValueError('خطوات التدبيل بين 0 و4')
    for key in ('entry_delay_seconds', 'loop_delay_seconds', 'result_timeout_extra',
                'balance_settle_seconds', 'loss_recheck_seconds'):
        valid_number(t[key], 0, 120)
    for key in ('demo_account', 'real_account'):
        valid_url(doc[key]['ws_url'])
    return doc


def patched(doc, fields):
    d = deepcopy(doc)
    if 'trading.asset' in fields and fields['trading.asset'] not in SYMBOL_NAMES:
        raise ValueError('اختر زوجاً من الصفحات الخمس المطابقة لبوت الإشارات')
    allowed = {'account_mode'} | {'schedule.' + k for k in cycle_defaults()} | {'payout.' + k for k in payout_defaults()} | {'recovery.' + k for k in recovery_defaults()} | {'risk.' + k for k in risk_defaults()} | {'trading.' + k for k in defaults()['trading']} | {
        f'{kind}_account.{key}' for kind in ('real', 'demo') for key in ('ssid', 'ws_url', 'uid')}
    for path, value in fields.items():
        if path not in allowed:
            raise ValueError('حقل إعداد غير مسموح')
        parts = path.split('.')
        obj = d
        for part in parts[:-1]:
            obj = obj[part]
        obj[parts[-1]] = value
    return validate_document(d)
