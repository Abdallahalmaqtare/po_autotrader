"""Transport-only helpers. Never leak URL queries, auth payloads or exception text.
websockets 15 auto-proxies unless explicitly disabled; older releases did not.
"""
import inspect
import json
import ssl
from urllib.parse import urlparse
import websockets
import config
from pocket_option import USER_AGENT


def control_text(frame):
    if isinstance(frame,bytes):
        try:return frame.decode('utf-8-sig')
        except UnicodeDecodeError:return ''
    return frame if isinstance(frame,str) else ''


def connect_options():
    parameters=inspect.signature(websockets.connect).parameters
    modern='additional_headers' in parameters or int(websockets.__version__.split('.')[0])>=14
    options=dict(ping_interval=None,max_size=2**22,close_timeout=3,
                 open_timeout=min(15,config.CONNECT_TIMEOUT_SECONDS),ssl=ssl.create_default_context())
    headers={'Cache-Control':'no-cache','Pragma':'no-cache'}
    if modern:
        options.update(origin='https://pocketoption.com',user_agent_header=USER_AGENT,additional_headers=headers)
    else:
        options['extra_headers']={**headers,'Origin':'https://pocketoption.com','User-Agent':USER_AGENT}
    if 'proxy' in parameters or int(websockets.__version__.split('.')[0])>=15:
        options['proxy']=True if getattr(config,'PO_USE_SYSTEM_PROXY',False) else None
    return options


def safe_host(url):return urlparse(url).hostname or 'unknown'


def failure_code(exc,stage):
    from diagnostics import SafeIssue
    if isinstance(exc,SafeIssue):return exc.code
    response=getattr(exc,'response',None)
    status=getattr(response,'status_code',None) or getattr(exc,'status_code',None)
    if status:return 'PO_WS_HTTP_'+str(status)
    if isinstance(exc,ssl.SSLError):return 'PO_WS_TLS'
    if isinstance(exc,TimeoutError):
        return {'transport':'PO_WS_TIMEOUT','engine_open':'PO_ENGINE_OPEN_TIMEOUT',
                'namespace':'PO_NAMESPACE_TIMEOUT','authentication':'PO_AUTH_NO_ACK'}.get(stage,'PO_WS_TIMEOUT')
    if isinstance(exc,(ConnectionError,OSError)):return 'PO_WS_NETWORK'
    return 'PO_WS_PROTOCOL'


def is_retryable(exc):
    from diagnostics import SafeIssue
    if isinstance(exc,(SafeIssue,ssl.SSLError)):return False
    status=getattr(getattr(exc,'response',None),'status_code',None) or getattr(exc,'status_code',None)
    if status:return status in (429,500,502,503,504)
    return isinstance(exc,(OSError,TimeoutError,EOFError,ConnectionError,websockets.exceptions.ConnectionClosed))


def connection_text(po):
    if po is None:return 'الاتصال بالمنصة: لم تتم محاولة الاتصال بعد؛ استخدم فحص WebSocket.'
    phase=getattr(po,'connection_phase','READY' if getattr(po,'connected',False) else 'CLOSED')
    code=getattr(po,'connection_code','')
    balance='وصل' if getattr(po,'balance',None) is not None else 'لم يصل بعد'
    lines=[f"WebSocket: {'متصل' if getattr(po,'connected',False) else 'غير متصل'} | المرحلة: {phase}",
           'رصيد الحساب: '+balance]
    if code:lines.append('تشخيص الاتصال: '+code)
    return '\n'.join(lines)
