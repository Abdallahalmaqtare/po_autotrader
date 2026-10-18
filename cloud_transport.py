"""Cloud lifecycle adapter. Original order/result algorithm remains in pocket_option.py.
Engine.IO heartbeat + Socket.IO 42 and single/multiple binary JSON attachments.
"""
import asyncio
import inspect
import json
import ssl
import time
import math
from contextlib import suppress
import websockets
import config
from pocket_option import PocketOptionWS, USER_AGENT
from cloud_settings import auth_frame
from diagnostics import SafeIssue


class EventDecoder:
    def __init__(self):
        self.pending = None

    def feed(self, frame):
        if isinstance(frame, bytes):
            if not self.pending:
                return []
            try:
                value = json.loads(frame.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                self.pending = None
                return []
            event, expected, attachments = self.pending
            attachments.append(value)
            if len(attachments) < expected:
                return []
            self.pending = None
            def restore(obj):
                if isinstance(obj, dict) and obj.get('_placeholder') is True:
                    return attachments[obj['num']]
                if isinstance(obj, list):
                    return [restore(v) for v in obj]
                if isinstance(obj, dict):
                    return {k: restore(v) for k, v in obj.items()}
                return obj
            try:
                return [restore(event)]
            except (IndexError, KeyError, TypeError):
                return []
        if frame.startswith('45') and '-' in frame:
            try:
                n, data = frame[2:].split('-', 1)
                n = int(n)
                obj = json.loads(data)
                if not 1 <= n <= 10:
                    return []
                self.pending = (obj, n, [])
            except (ValueError, TypeError):
                self.pending = None
            return []
        if frame.startswith('42'):
            try:
                return [json.loads(frame[2:])]
            except ValueError:
                return []
        return []


class CloudPocket(PocketOptionWS):
    def __init__(self, credentials, mode):
        super().__init__(credentials['ssid'])
        self.url = credentials['ws_url']
        self.mode = mode
        self.credentials = credentials.copy()
        self.decoder = EventDecoder()
        self.balance_at = 0.0

    def _build_auth_frame(self):
        return auth_frame(self.credentials['ssid'], self.mode, self.credentials.get('uid', 0))

    def process(self, event):
        if not isinstance(event, list) or len(event) < 2:
            return
        name, payload = event[:2]
        if name == 'updateBalance' and isinstance(payload, dict):
            demo = payload.get('isDemo')
            if demo is not None:
                flags = {'true': 1, 'false': 0, '1': 1, '0': 0}
                parsed = int(demo) if isinstance(demo, (bool, int, float)) and demo in (0, 1) else flags.get(str(demo).strip().lower())
                if parsed is None or parsed != (1 if self.mode == 'PRACTICE' else 0):
                    return
            try:
                value = float(payload['balance'])
            except (KeyError, TypeError, ValueError):
                return
            if math.isfinite(value) and value >= 0:
                self.balance = value
                self.balance_at = time.monotonic()
        # Result frames remain decoded, but the inherited sizing decision
        # deliberately uses the existing balance-comparison + recheck algorithm.

    async def connect(self):
        await self.close()
        self.decoder = EventDecoder()
        self.balance = None
        headers = {'User-Agent': USER_AGENT, 'Origin': 'https://pocketoption.com'}
        kwargs = dict(ping_interval=None, max_size=2**22, close_timeout=3,
                      open_timeout=10, ssl=ssl.create_default_context())
        key = 'additional_headers' if 'additional_headers' in inspect.signature(websockets.connect).parameters else 'extra_headers'
        kwargs[key] = headers
        try:
            async with asyncio.timeout(config.CONNECT_TIMEOUT_SECONDS):
                self.ws = await websockets.connect(self.url, **kwargs)
                greeting = await self.ws.recv()
                if not isinstance(greeting, str) or not greeting.startswith('0'):
                    raise RuntimeError('protocol_open_failed')
                await self.ws.send('40')
                while True:
                    msg = await self.ws.recv()
                    if msg == '2':
                        await self.ws.send('3')
                    elif isinstance(msg, str) and msg.startswith('40'):
                        break
                await self.ws.send(self._build_auth_frame())
                authenticated = False
                while not authenticated:
                    msg = await self.ws.recv()
                    if msg == '2':
                        await self.ws.send('3')
                        continue
                    for event in self.decoder.feed(msg):
                        if not isinstance(event, list) or not event:
                            continue
                        if event[0] in ('failauth', 'errorauth', 'authError'):
                            raise SafeIssue('PO_AUTH_REJECTED')
                        self.process(event)
                        authenticated = event[0] == 'successauth' or self.balance is not None
                self.connected = True
                self._recv_task = asyncio.create_task(self._receiver_loop())
        except BaseException:
            await self.close()
            raise

    async def _receiver_loop(self):
        try:
            async for frame in self.ws:
                if frame == '2':
                    await self.ws.send('3')
                else:
                    for event in self.decoder.feed(frame):
                        self.process(event)
        except Exception:
            pass  # do not log server payloads/session strings
        finally:
            self.connected = False

    async def ensure_connected(self):
        if not self.connected:
            # Never replay an old signal through an automatic reconnect.
            raise RuntimeError('disconnected_before_send')

    async def select_asset(self, asset, duration):
        await self.ws.send('42' + json.dumps(['changeSymbol', {'asset': asset, 'period': duration}]))

    async def close(self):
        self.connected = False
        task, self._recv_task = self._recv_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        if self.ws:
            with suppress(Exception):
                await self.ws.close()
        self.ws = None
