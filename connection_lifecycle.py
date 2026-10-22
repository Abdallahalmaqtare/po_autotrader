"""Engine.IO lifecycle, isolated from order submission and accounting.
Retry only connection setup. NEVER replay openOrder. No URLs, sessions or raw
server close reasons are logged; failure codes/stages are safe to show in UI.
"""
import asyncio
import inspect
import json
import math
import ssl
import time
from collections import deque
from contextlib import suppress
from urllib.parse import urlsplit,urlunsplit,parse_qsl,urlencode
import websockets
import config
from diagnostics import SafeIssue
from pocket_option import USER_AGENT
from wire_decoder import EventDecoder
from runtime_support import timestamp_print as print


class OrderNotSent(RuntimeError):
    """Local validation/connection guard failed BEFORE any openOrder send."""


def connection_url(value):
    from cloud_settings import valid_url
    value=valid_url(value)
    parts=urlsplit(value);query=parse_qsl(parts.query,keep_blank_values=True)
    # Engine.IO's sid belongs to a PREVIOUS transport and isn't an auth session.
    query=[(k,v) for k,v in query if k!='sid']
    values=dict(query)
    if values.get('EIO','4') not in ('3','4'):raise ValueError('إصدار Engine.IO يجب أن يكون 3 أو 4')
    if values.get('transport','websocket')!='websocket':raise ValueError('رابط الاتصال يجب أن يستخدم transport=websocket')
    if 'EIO' not in values:query.append(('EIO','4'))
    if 'transport' not in values:query.append(('transport','websocket'))
    return urlunsplit((parts.scheme,parts.netloc,parts.path or '/socket.io/',urlencode(query),''))


class ConnectionLifecycle:
    async def _packets(self):
        if self._packet_buffer:return self._packet_buffer.popleft()
        frame=await self.ws.recv()
        if isinstance(frame,str) and '\x1e' in frame:
            self._packet_buffer.extend(frame.split('\x1e'))
            return self._packet_buffer.popleft()
        return frame

    async def _control(self,frame):
        if isinstance(frame,bytes) and frame[:1] in (b'0',b'1',b'2',b'3',b'4'):
            with suppress(UnicodeDecodeError):frame=frame.decode('utf-8')
        if isinstance(frame,str):
            if frame.startswith('2'):
                self._last_ping=time.monotonic()
                await self.ws.send('3'+frame[1:]);return True
            if frame.startswith('3'):
                self._last_pong=time.monotonic();return True
            if frame=='1' or frame=='41':raise SafeIssue('PO_DISCONNECTED')
        return False

    async def _heartbeat(self):
        try:
            while True:
                await asyncio.sleep(self._ping_interval)
                if self._engine_version=='3':
                    sent=time.monotonic();await self.ws.send('2')
                    await asyncio.sleep(self._ping_timeout)
                    if self._last_pong<sent:raise TimeoutError()
                elif time.monotonic()-self._last_ping>self._ping_interval+self._ping_timeout:
                    raise TimeoutError()
        except asyncio.CancelledError:raise
        except Exception as exc:
            self._connection_failed('PO_HEARTBEAT_TIMEOUT','heartbeat',exc)
            if self.ws:
                with suppress(Exception):await self.ws.close()

    def _connection_failed(self,code,stage,exc=None):
        self.connected=False
        self.connection_state='DISCONNECTED'
        self.last_connection_code=code;self.last_connection_stage=stage
        self.closed_event.set()
        print(f'[WS] code={code}; stage={stage}; type={type(exc).__name__ if exc else "closed"}',flush=True)

    async def connect(self):
        # No retry path contains an order send. A bad token/UID isn't transient.
        self.url_effective=connection_url(self.url)
        self._engine_version=dict(parse_qsl(urlsplit(self.url_effective).query)).get('EIO','4')
        self.last_connection_code='';self.last_connection_stage=''
        for attempt in range(3):
            await self.close()
            self.closed_event.clear();self.connection_state='CONNECTING'
            self.payout_book.clear();self.decoder=EventDecoder();self.balance=None
            self.balance_at=0.0;self.saw_other_account_balance=False;self.server_uid=None
            self._packet_buffer=deque();stage='open'
            try:
                params=inspect.signature(websockets.connect).parameters
                kwargs=dict(ping_interval=None,max_size=2**22,close_timeout=3,
                    open_timeout=min(10,config.CONNECT_TIMEOUT_SECONDS),ssl=ssl.create_default_context())
                key='additional_headers' if 'additional_headers' in params else 'extra_headers'
                kwargs[key]={'User-Agent':USER_AGENT,'Origin':'https://pocketoption.com'}
                if 'proxy' in params:kwargs['proxy']=None
                async with asyncio.timeout(config.CONNECT_TIMEOUT_SECONDS):
                    self.ws=await websockets.connect(self.url_effective,**kwargs)
                    greeting=await self._packets()
                    if isinstance(greeting,bytes):greeting=greeting.decode('utf-8')
                    if not isinstance(greeting,str) or not greeting.startswith('0'):raise SafeIssue('PO_PROTOCOL_FAILED')
                    try:
                        hello=json.loads(greeting[1:])
                        self._ping_interval=float(hello.get('pingInterval',25000))/1000
                        self._ping_timeout=float(hello.get('pingTimeout',20000))/1000
                        if not all(math.isfinite(v) and .001<=v<=300 for v in (self._ping_interval,self._ping_timeout)):raise ValueError()
                    except (ValueError,TypeError,AttributeError):raise SafeIssue('PO_PROTOCOL_FAILED') from None
                    self._last_ping=self._last_pong=time.monotonic()
                    stage='namespace';await self.ws.send('40')
                    while True:
                        msg=await self._packets()
                        if await self._control(msg):continue
                        if isinstance(msg,bytes):
                            with suppress(UnicodeDecodeError):msg=msg.decode('utf-8')
                        if isinstance(msg,str) and msg.startswith('40'):break
                        if isinstance(msg,str) and msg.startswith('44'):raise SafeIssue('PO_AUTH_REJECTED')
                    self._heartbeat_task=asyncio.create_task(self._heartbeat())
                    stage='auth';self.connection_state='AUTHENTICATING'
                    await self.ws.send(self._build_auth_frame())
                    authenticated=False
                    while not authenticated:
                        msg=await self._packets()
                        if await self._control(msg):continue
                        for event in self.decoder.feed(msg):
                            if not isinstance(event,list) or not event:continue
                            if str(event[0]).lower() in ('failauth','errorauth','autherror','unauthorized'):
                                raise SafeIssue('PO_AUTH_REJECTED')
                            self.process(event)
                            if self.saw_other_account_balance:raise SafeIssue('PO_ACCOUNT_MISMATCH')
                            authenticated=authenticated or str(event[0]).lower()=='successauth' or self.balance is not None
                    self.connected=True;self.connection_state='CONNECTED'
                    self.last_connection_code='';self.last_connection_stage='ready'
                    self._recv_task=asyncio.create_task(self._receiver_loop())
                    print(f'[WS] authenticated; EIO={self._engine_version}; account={self.mode}',flush=True)
                    return
            except asyncio.CancelledError:
                await self.close();raise
            except Exception as exc:
                code=exc.code if isinstance(exc,SafeIssue) else 'PO_CONNECT_TIMEOUT' if isinstance(exc,TimeoutError) else 'PO_CONNECT_FAILED'
                self._connection_failed(code,stage,exc)
                await self.close()
                # Validation/auth failures must be repaired, not retried endlessly.
                if isinstance(exc,SafeIssue):raise
                if attempt==2:raise SafeIssue(code) from None
                await asyncio.sleep(.5*(2**attempt))

    async def _receiver_loop(self):
        try:
            while True:
                frame=await self._packets()
                if await self._control(frame):continue
                for event in self.decoder.feed(frame):
                    if event and str(event[0]).lower() in ('failauth','errorauth','autherror','unauthorized'):
                        raise SafeIssue('PO_AUTH_REJECTED')
                    self.process(event)
        except asyncio.CancelledError:raise
        except Exception as exc:
            code=exc.code if isinstance(exc,SafeIssue) else 'PO_DISCONNECTED'
            if not (self.closed_event.is_set() and self.last_connection_code):
                self._connection_failed(code,'receive',exc)
        finally:
            self.connected=False;self.closed_event.set()
            self.connection_state='DISCONNECTED'
            heartbeat=getattr(self,'_heartbeat_task',None)
            if heartbeat and heartbeat is not asyncio.current_task():heartbeat.cancel()
            if self.ws:
                with suppress(Exception):await self.ws.close()

    async def close(self):
        self.connected=False;self.connection_state='DISCONNECTED'
        self.closed_event.set()
        for key in ('_heartbeat_task','_recv_task'):
            task=getattr(self,key,None);setattr(self,key,None)
            if task and task is not asyncio.current_task():
                task.cancel()
                with suppress(asyncio.CancelledError,Exception):await task
        ws,self.ws=self.ws,None
        if ws:
            with suppress(Exception):await ws.close()
