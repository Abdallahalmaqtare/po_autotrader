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
from order_tracking import OrderTracker, CLOSE_EVENTS
from logger import log


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
        self.saw_other_account_balance = False
        self.orders = OrderTracker()
        self.last_outcome = None
        self.result_source = None
        self.result_net = None
        self.sent_at = 0.0
        self.due_at = 0.0
        self._request_counter = 0

    def _build_auth_frame(self):
        return auth_frame(self.credentials['ssid'], self.mode, self.credentials.get('uid', 0))

    def process(self, event):
        """Accept balance from ANY event carrying a numeric 'balance' (the proven
        local build tolerated all frames). When an isDemo flag is present it must
        match the selected account; a mismatch is flagged so the worker reports
        PO_ACCOUNT_MISMATCH instead of a generic missing balance."""
        self.orders.feed(event)
        if not isinstance(event, list) or len(event) < 2:
            return
        name, payload = event[:2]
        if name in CLOSE_EVENTS:
            print('[PO] close event received; matched_current_order=' + str(self.orders.last_result is not None), flush=True)
        if not isinstance(payload, dict) or 'balance' not in payload:
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
        self.decoder = EventDecoder()
        self.balance = None
        self.saw_other_account_balance = False
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

    async def place_trade(self, direction, amount=None):
        """Same openOrder fields as before; track request ID BEFORE yielding send()."""
        if direction not in ('UP', 'DOWN'):
            raise ValueError('Invalid trade direction')
        amount = config.TRADE_AMOUNT if amount is None else float(amount)
        async with self._lock:
            await self.ensure_connected()
            self._request_counter = max(self._request_counter + 1, int(time.time()*1000) % 2000000000)
            request = self._request_counter
            payload = {'asset': config.ASSET, 'amount': amount,
                       'action': 'call' if direction == 'UP' else 'put',
                       'isDemo': 1 if self.mode == 'PRACTICE' else 0,
                       'optionType': 100, 'time': config.DURATION_SECONDS,
                       'requestId': request}
            self._balance_before = self.balance
            self.orders.begin(request, amount, config.ASSET, self.mode)
            self.last_outcome, self.result_source, self.result_net = None, None, None
            self.sent_at = time.monotonic()
            self.due_at = self.sent_at + config.DURATION_SECONDS
            try:
                await self.ws.send('42' + json.dumps(['openOrder', payload], separators=(',', ':')))
                log(f'[PO] ORDER SENT -> {config.ASSET} | {direction} | {amount:g}$ | '
                    f'{config.DURATION_SECONDS}s | request_id={request}')
                return True
            except Exception:
                self.connected = False
                # Send may already have reached server: never resend it here.
                return False

    def _take_close(self, outcome):
        self.last_outcome = outcome
        self.result_source = outcome.source
        self.result_net = outcome.net_profit
        log(f'[PO] RESULT {outcome.result.upper()} | source=matched_close | request_id={outcome.request_id}')
        return outcome.result

    async def wait_for_result(self, timeout=8):
        """Never turn an unchanged opening debit into LOSS. Loss requires exact close ID.
        Wait/reconnect within bounded grace; unresolved -> caller restarts at base.
        """
        settle = max(0.0,float(config.BALANCE_SETTLE_SECONDS))
        grace = max(0.0,float(config.LOSS_RECHECK_SECONDS))
        deadline = max(self.sent_at+float(timeout), self.due_at) + settle + grace
        attempted_reconnect = False
        while time.monotonic() < deadline:
            if self.orders.last_result is not None:
                return self._take_close(self.orders.last_result)
            if not self.connected and not attempted_reconnect:
                attempted_reconnect = True
                try:
                    await asyncio.wait_for(self.connect(), max(.001, min(config.CONNECT_TIMEOUT_SECONDS, deadline-time.monotonic())))
                except Exception:
                    pass
            outcome = await self.orders.wait(min(.15,max(.001,deadline-time.monotonic())))
            if outcome is not None:
                return self._take_close(outcome)
        # A positive isolated balance delta may reset sizing to base, but cannot
        # trigger a LOSS/martingale. Do not attribute money as verified deal PnL.
        if (not attempted_reconnect and self.connected and self._balance_before is not None and
                self.balance is not None and self.balance > self._balance_before + 1e-6):
            self.result_source, self.result_net = 'balance_gain_unverified', None
            log('[PO] Balance gain observed -> reset base; financial attribution unverified')
            return 'win'
        self.result_source, self.result_net = 'unconfirmed', None
        log('[PO] No matched close -> UNCONFIRMED; opening debit will NOT trigger doubling')
        return None
