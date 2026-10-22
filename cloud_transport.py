"""Cloud lifecycle adapter. Production orders use exact broker-deal settlement.
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
from trade_settlement import DealTracker, number
from uuid import uuid4


from connection_lifecycle import ConnectionLifecycle,OrderNotSent

class CloudPocket(ConnectionLifecycle,PocketOptionWS):
    strict_results = True
    def __init__(self, credentials, mode):
        super().__init__(credentials['ssid'])
        self.url = credentials['ws_url']
        self.mode = mode
        self.credentials = credentials.copy()
        from account_details import credential_uid
        self.expected_uid=credential_uid(credentials,mode)
        self.payout_book = PayoutBook(mode)
        self.entry_check = None
        self.decoder = EventDecoder()
        self.balance_at = 0.0
        self.saw_other_account_balance = False
        self.server_uid = None
        self._payout_log_at = 0.0
        self.tracker = None
        self.last_settlement = None
        self.on_evidence = None
        self._evidence_tasks = set()
        self.server_name = None
        self.closed_event=asyncio.Event()
        self.connection_state="DISCONNECTED"
        self.last_connection_code=""
        self.last_connection_stage="not_started"
        self._heartbeat_task=None

    def _build_auth_frame(self):
        return auth_frame(self.credentials['ssid'], self.mode, self.credentials.get('uid', 0))

    def process(self, event):
        """Accept balance from ANY event carrying a numeric 'balance' (the proven
        local build tolerated all frames). When an isDemo flag is present it must
        match the selected account; a mismatch is flagged so the worker reports
        PO_ACCOUNT_MISMATCH instead of a generic missing balance."""
        if not isinstance(event, list) or len(event) < 2:
            return
        if self.tracker:
            old_id=self.tracker.broker_id
            self.tracker.feed(event)
            if self.tracker.broker_id and self.tracker.broker_id!=old_id and self.on_evidence:
                task=asyncio.create_task(self.on_evidence(self.tracker.order['id'],self.tracker.broker_id))
                self._evidence_tasks.add(task)
                task.add_done_callback(self._evidence_tasks.discard)
                task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
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
                    uid=positive_uid(user.get('uid'))
                    candidate=positive_uid(user.get('id'))
                    if not uid and candidate==self.expected_uid:uid=candidate
            if uid:
                expected_uid=str(self.credentials.get('uid') or '')
                from account_details import credential_uid
                expected_uid=credential_uid(self.credentials,self.mode) or expected_uid
                if expected_uid and str(uid)!=expected_uid:
                    self.saw_other_account_balance=True
                    self.connected=False
                    raise SafeIssue('PO_UID_MISMATCH')
                self.server_uid = uid
            user=payload.get('user') if isinstance(payload.get('user'),dict) else payload
            from account_registry import safe_name
            self.server_name=safe_name(user.get('name') or user.get('username') or user.get('nickname')) or self.server_name
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

    def prepare_order(self,order):
        self.last_settlement=None
        self.tracker=DealTracker(order,self.server_uid or self.expected_uid)

    async def flush_evidence(self):
        if self._evidence_tasks:
            await asyncio.gather(*list(self._evidence_tasks))

    async def place_trade(self,direction,amount=None):
        # Never retry an uncertain send. Tracker exists BEFORE yielding to send().
        if self.tracker is None:raise RuntimeError('order_not_prepared')
        order=self.tracker.order
        if direction!=order['direction'] or number(amount)!=number(order['amount']):
            raise RuntimeError('order_snapshot_changed')
        async with self._lock:
            await self.ensure_connected()
            self._balance_before=self.balance  # diagnostics only; NOT PnL
            frame=['openOrder',dict(asset=order['asset'],amount=order['amount'],
                action='call' if direction=='UP' else 'put',isDemo=1 if self.mode=='PRACTICE' else 0,
                optionType=100,time=order['duration_seconds'],requestId=order['request_id'])]
            began=time.perf_counter()
            await self.ws.send('42'+json.dumps(frame,separators=(',',':')))
            print(f"[PO] ORDER SENT id={order['id']} request={order['request_id']} amount={order['amount']} latency_ms={(time.perf_counter()-began)*1000:.1f}",flush=True)
            return True

    async def wait_for_result(self,timeout=8):
        if self.tracker is None:return None
        budget=max(0,float(timeout))+max(0,float(config.BALANCE_SETTLE_SECONDS))+max(0,float(config.LOSS_RECHECK_SECONDS))
        try:await asyncio.wait_for(self.tracker.ready.wait(),budget)
        except asyncio.TimeoutError:
            print('[RESULT] exact closed deal not received; unresolved, no doubling',flush=True)
            await self.flush_evidence()
            return None
        await self.flush_evidence()
        if self.tracker.conflict:return None
        self.last_settlement=self.tracker.settlement
        if self.last_settlement:
            item=self.last_settlement
            print(f"[RESULT] confirmed broker deal={item['broker_id']} result={item['result']} net={item['net_pnl']:+.6f}",flush=True)
            return item['result']
        return None

    async def ensure_connected(self):
        state=getattr(self.ws,'state',None)
        if not self.connected or self.ws is None or getattr(state,'name','') in ('CLOSING','CLOSED'):
            raise OrderNotSent('disconnected_before_send')
        if self.entry_check is not None and not self.entry_check():
            raise PayoutBlocked('pre_send_guard_rejected')

    async def refresh_payouts(self):
        # Known handshake, not a guessed getAssets event. Called BETWEEN orders.
        await self.connect()

    async def select_asset(self, asset, duration):
        await self.ws.send('42' + json.dumps(['changeSymbol', {'asset': asset, 'period': duration}]))

