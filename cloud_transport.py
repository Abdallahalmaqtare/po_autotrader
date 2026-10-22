"""Cloud lifecycle adapter. Original order/result algorithm remains in pocket_option.py.
Engine.IO heartbeat + Socket.IO 42 and single/multiple binary JSON attachments.
"""
import asyncio
from runtime_support import timestamp_print as print
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
from account_details import positive_uid
from payout_guard import PayoutBook, PayoutBlocked, allowed, symbol


from wire_decoder import EventDecoder


class CloudPocket(PocketOptionWS):
    def __init__(self, credentials, mode):
        super().__init__(credentials['ssid'])
        self.url = credentials['ws_url']
        self.mode = mode
        self.credentials = credentials.copy()
        self.payout_book = PayoutBook(mode)
        self.entry_check = None
        self.decoder = EventDecoder()
        self.balance_at = 0.0
        self.saw_other_account_balance = False
        self.server_uid = None
        self._payout_log_at = 0.0

    def _build_auth_frame(self):
        return auth_frame(self.credentials['ssid'], self.mode, self.credentials.get('uid', 0))

    def process(self, event):
        """Accept balance from ANY event carrying a numeric 'balance' (the proven
        local build tolerated all frames). When an isDemo flag is present it must
        match the selected account; a mismatch is flagged so the worker reports
        PO_ACCOUNT_MISMATCH instead of a generic missing balance."""
        if not isinstance(event, list) or len(event) < 2:
            return
        revision=self.payout_book.revision
        self.payout_book.feed(event)
        if self.payout_book.revision!=revision and time.monotonic()-self._payout_log_at>10:
            self._payout_log_at=time.monotonic()
            print(f'[PAYOUT] parsed assets={len(self.payout_book.quotes)}; account={self.mode}',flush=True)
        name, payload = event[:2]
        if not isinstance(payload, dict):
            return
        demo = payload.get('isDemo')
        expected = 1 if self.mode == 'PRACTICE' else 0
        if demo is not None:
            if isinstance(demo, bool):
                parsed = int(demo)
            elif isinstance(demo, (int, float)) and demo in (0, 1):
                parsed = int(demo)
            else:
                flags = {'true': 1, 'false': 0, '1': 1, '0': 0}
                parsed = flags.get(str(demo).strip().lower())
            if parsed is None:
                return
            if parsed != expected:
                self.saw_other_account_balance = True
                print('[PO] balance frame arrived for the OTHER account type - '
                      'stored SSID does not match the selected account mode', flush=True)
                return
        if name in ('successauth', 'profile', 'updateProfile', 'updateBalance'):
            uid = positive_uid(payload.get('uid')) or positive_uid(payload.get('user_id'))
            if uid is None and name in ('successauth', 'profile', 'updateProfile'):
                user = payload.get('user')
                if isinstance(user, dict):
                    uid = positive_uid(user.get('id')) or positive_uid(user.get('uid'))
            if uid:
                self.server_uid = uid
        if 'balance' not in payload:
            return
        try:
            value = float(payload['balance'])
        except (TypeError, ValueError):
            return
        if math.isfinite(value) and value >= 0:
            self.balance = value
            self.balance_at = time.monotonic()
            print(f'[PO] Balance received ({self.mode}): {value}', flush=True)

    async def connect(self):
        await self.close()
        self.payout_book.clear()
        self.decoder = EventDecoder()
        self.balance = None
        self.saw_other_account_balance = False
        self.server_uid = None
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
        if self.entry_check is not None and not self.entry_check():
            raise PayoutBlocked('pre_send_guard_rejected')
        if not self.connected:
            # Never replay an old signal through an automatic reconnect.
            raise RuntimeError('disconnected_before_send')

    async def refresh_payouts(self):
        # Known handshake, not a guessed getAssets event. Called BETWEEN orders.
        await self.connect()

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
