"""Public identity only: never expose auth/session payloads in Telegram panels."""
import json
from cloud_settings import auth_frame


def positive_uid(value):
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if text.isascii() and text.isdecimal() and 0 < len(text) <= 24 and int(text) > 0:
        return str(int(text))
    return None


def credential_uid(credentials, mode):
    try:
        normalized = auth_frame(credentials.get('ssid', ''), mode, credentials.get('uid', 0))
        return positive_uid(json.loads(normalized[2:])[1].get('uid'))
    except (ValueError, TypeError, KeyError, IndexError):
        return None


def credentials_match(left,right,mode):
    """Compare actual normalized auth + endpoint, not incidental UID cache fields.
    A v20 mirror may store uid=0 while its full auth frame and registry use UID.
    Identity equality alone is NOT enough: the session must also match.
    """
    if not isinstance(left,dict) or not isinstance(right,dict):return False
    try:
        from connection_lifecycle import connection_url
        def auth_key(credentials):
            payload=json.loads(auth_frame(credentials.get('ssid',''),mode,credentials.get('uid',0))[2:])[1]
            payload['uid']=positive_uid(payload['uid'])
            return json.dumps(payload,sort_keys=True,separators=(',',':'))
        from urllib.parse import urlsplit,parse_qsl
        def endpoint(value):
            url=urlsplit(connection_url(value))
            return (url.scheme,url.netloc,url.path,sorted(parse_qsl(url.query)))
        return auth_key(left)==auth_key(right) and endpoint(left['ws_url'])==endpoint(right['ws_url'])
    except (ValueError,KeyError,TypeError,IndexError):return left==right


def account_identity(doc, po=None):
    mode = doc['account_mode']
    key = 'real_account' if mode == 'REAL' else 'demo_account'
    credentials = doc[key]
    uid = credential_uid(credentials, mode)
    # Never display a previous account's live UID after settings changed.
    matches = (po is not None and getattr(po, 'connected', False)
               and getattr(po, 'mode', None) == mode
               and credentials_match(getattr(po,'credentials',None),credentials,mode))
    live_uid = positive_uid(getattr(po, 'server_uid', None)) if matches else None
    return {'mode': mode, 'uid': live_uid or uid,
            'source': 'من استجابة المنصة' if live_uid else 'من بيانات الجلسة المحفوظة' if uid else 'غير متاح',
            'connected': bool(matches)}


def account_text(doc, po=None):
    info = account_identity(doc, po)
    label = 'حقيقي REAL' if info['mode'] == 'REAL' else 'تجريبي DEMO / PRACTICE'
    record=doc.get('accounts',{}).get(doc.get('active_account_id'),{})
    name=record.get('name')
    return (f"👤 الحساب النشط: {label}\n"
            f"UID: {info['uid'] or 'غير متاح'}\n"
            +(f"الاسم: {name}\n" if name else '')+
            f"الاتصال: {'متصل' if info['connected'] else 'غير متصل'}\n")
