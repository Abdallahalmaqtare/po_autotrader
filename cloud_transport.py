"""Cloud lifecycle adapter. Exact broker results first, bounded guarded balance compatibility second.
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
from collections import deque
from balance_settlement import BalanceEvidence
from ws_connection import control_text,connect_options,safe_host,failure_code,is_retryable


class CloudPocket(PocketOptionWS):
    strict_results = True
    result_clock=staticmethod(time.monotonic)
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
        self.connection_phase='IDLE'
        self.connection_code=''
        self.disconnect_event=asyncio.Event()
        self.connect_lock=asyncio.Lock()
        self.balance_evidence=None
        self.retired=deque(maxlen=20)
        self.on_retired=None
        self.order_expiry_wall=0.0

    def _build_auth_frame(self):
        return auth_frame(self.credentials['ssid'], self.mode, self.credentials.get('uid', 0))

    def process(self, event):
        """Accept balance from ANY event carrying a numeric 'balance' (the proven
        local build tolerated all frames). When an isDemo flag is present it must
        match the selected account; a mismatch is flagged so the worker reports
        PO_ACCOUNT_MISMATCH instead of a generic missing balance."""
        if not isinstance(event, list) or len(event) < 2:
            return
        for retired in list(self.retired):
            was=retired.settlement
            retired.feed(event)
            if retired.settlement and retired.settlement!=was and self.on_retired:
                task=asyncio.create_task(self.on_retired(retired.order,retired.settlement))
                self._evidence_tasks.add(task)
                task.add_done_callback(self._evidence_tasks.discard)
                task.add_done_callback(lambda t:t.exception() if not t.cancelled() else None)
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
        if isinstance(payload.get('data'),dict) and ('balance' in payload['data'] or 'uid' in payload['data']):
            payload={**payload,**payload['data']}
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
                expected_uid=str(self.credentials.get('uid') or '')
                from account_details import credential_uid
                expected_uid=credential_uid(self.credentials,self.mode) or expected_uid
                if expected_uid and str(uid)!=expected_uid:
                    self.saw_other_account_balance=True
                    self.connected=False
                    raise SafeIssue('PO_ACCOUNT_MISMATCH')
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
            if self.balance_evidence and name in ('updateBalance','balance'):self.balance_evidence.feed(value)
            self.balance = value
            self.balance_at = time.monotonic()
            print(f'[PO] Balance received ({self.mode}): {value}', flush=True)

    async def connect(self):
        async with self.connect_lock:
            for attempt in range(1,4):
                await self.close()
                self.connection_code='';self.connection_phase='transport'
                self.payout_book.clear();self.decoder=EventDecoder()
                self.balance=None;self.saw_other_account_balance=False;self.server_uid=None
                print(f'[WS] connect host={safe_host(self.url)} account={self.mode} attempt={attempt}',flush=True)
                try:
                    async with asyncio.timeout(config.CONNECT_TIMEOUT_SECONDS):
                        self.ws=await websockets.connect(self.url,**connect_options())
                        self.connection_phase='engine_open'
                        greeting=control_text(await self.ws.recv())
                        if not greeting.startswith('0'):raise SafeIssue('PO_WS_PROTOCOL')
                        try:opened=json.loads(greeting[1:])
                        except (ValueError,TypeError):raise SafeIssue('PO_WS_PROTOCOL') from None
                        if not isinstance(opened,dict):raise SafeIssue('PO_WS_PROTOCOL')
                        self.connection_phase='namespace'
                        await self.ws.send('40')
                        while True:
                            text=control_text(await self.ws.recv())
                            if text.startswith('2'):await self.ws.send('3'+text[1:])
                            elif text.startswith('40'):break
                            elif text.startswith(('41','44')):raise SafeIssue('PO_WS_PROTOCOL')
                        self.connection_phase='authentication'
                        await self.ws.send(self._build_auth_frame())
                        authenticated=False
                        while not authenticated:
                            frame=await self.ws.recv();text=control_text(frame)
                            if text.startswith('2'):
                                await self.ws.send('3'+text[1:]);continue
                            if text.startswith('41'):raise SafeIssue('PO_AUTH_REJECTED')
                            for event in self.decoder.feed(frame):
                                if not isinstance(event,list) or not event:continue
                                name=str(event[0]).lower().replace('_','')
                                if name in ('failauth','errorauth','autherror','failureauth'):
                                    raise SafeIssue('PO_AUTH_REJECTED')
                                self.process(event)
                                if self.saw_other_account_balance:raise SafeIssue('PO_ACCOUNT_MISMATCH')
                                authenticated=authenticated or name=='successauth' or self.balance is not None
                        self.connected=True;self.connection_phase='READY';self.connection_code=''
                        self.disconnect_event.clear()
                        self._recv_task=asyncio.create_task(self._receiver_loop())
                        print('[WS] authenticated; receiver started',flush=True)
                        return
                except asyncio.CancelledError:
                    await self.close();raise
                except Exception as exc:
                    code=failure_code(exc,self.connection_phase)
                    print(f'[WS] failed phase={self.connection_phase} code={code} error={type(exc).__name__}',flush=True)
                    retry=is_retryable(exc) and attempt<3
                    await self.close();self.connection_phase='FAILED';self.connection_code=code
                    if not retry:raise SafeIssue(code) from None
                    await asyncio.sleep(min(2**(attempt-1),4))

    async def _receiver_loop(self):
        try:
            async for frame in self.ws:
                text=control_text(frame)
                if text.startswith('2'):
                    await self.ws.send('3'+text[1:])
                elif text.startswith('41'):
                    self.connection_code='PO_WS_CLOSED';break
                else:
                    for event in self.decoder.feed(frame):
                        if event and str(event[0]).lower() in ('failauth','errorauth','autherror'):
                            raise SafeIssue('PO_AUTH_REJECTED')
                        self.process(event)
        except asyncio.CancelledError:raise
        except Exception as exc:
            self.connection_code=failure_code(exc,'receiver')
            print(f'[WS] receiver ended code={self.connection_code} error={type(exc).__name__}',flush=True)
        finally:
            self.connected=False;self.disconnect_event.set()
            if self.connection_phase=='READY':self.connection_phase='CLOSED'
            if not self.connection_code:self.connection_code='PO_WS_CLOSED'

    def prepare_order(self,order):
        if self.tracker and self.tracker.order['id']!=order['id']:self.retire_order()
        self.balance_evidence=None
        self.last_settlement=None
        self.tracker=DealTracker(order,self.server_uid or self.expected_uid)

    def retire_order(self):
        if self.tracker and not any(t.order['id']==self.tracker.order['id'] for t in self.retired):self.retired.append(self.tracker)
        self.tracker=None;self.balance_evidence=None;self.last_settlement=None

    async def flush_evidence(self):
        if self._evidence_tasks:
            # Cancelling a trade wait must not cancel an independent late ledger commit.
            await asyncio.shield(asyncio.gather(*list(self._evidence_tasks)))

    async def place_trade(self,direction,amount=None):
        # Never retry an uncertain send. Tracker exists BEFORE yielding to send().
        if self.tracker is None:raise RuntimeError('order_not_prepared')
        order=self.tracker.order
        if direction!=order['direction'] or number(amount)!=number(order['amount']):
            raise RuntimeError('order_snapshot_changed')
        async with self._lock:
            await self.ensure_connected()
            self._balance_before=self.balance
            self.balance_evidence=BalanceEvidence(order,self.balance,self.balance_at,now=self.result_clock)
            frame=['openOrder',dict(asset=order['asset'],amount=order['amount'],
                action='call' if direction=='UP' else 'put',isDemo=1 if self.mode=='PRACTICE' else 0,
                optionType=100,time=order['duration_seconds'],requestId=order['request_id'])]
            self.order_expiry_wall=time.time()+float(order['duration_seconds'])+2
            began=time.perf_counter()
            await self.ws.send('42'+json.dumps(frame,separators=(',',':')))
            print(f"[PO] ORDER SENT id={order['id']} request={order['request_id']} amount={order['amount']} latency_ms={(time.perf_counter()-began)*1000:.1f}",flush=True)
            return True

    async def wait_for_result(self,timeout=8):
        if self.tracker is None:return None
        # A bounded final-settlement window. Never turn the first debit into LOSS.
        budget=max(0,float(timeout))+max(0,float(config.BALANCE_SETTLE_SECONDS))+max(0,float(config.LOSS_RECHECK_SECONDS))
        evidence=self.balance_evidence
        quiet=max(.5,float(config.BALANCE_SETTLE_SECONDS))
        end=time.monotonic()+budget
        if evidence:end=max(end,evidence.expires+quiet)
        while True:
            if self.tracker.conflict:return None
            if self.tracker.ready.is_set():
                await self.flush_evidence()
                self.last_settlement=self.tracker.settlement
                if self.last_settlement:
                    item=self.last_settlement
                    print(f"[RESULT] exact deal={item['broker_id']} result={item['result']} net={item['net_pnl']:+.6f}",flush=True)
                    return item['result']
            # Restore v20 compatibility only with observed debit + mature refund/
            # winning credit. Receipt source stays explicit in the financial log.
            receipt=evidence.evaluate(self.connected,quiet,loss_ready=time.monotonic()>=end,open_ack=bool(self.tracker.broker_id)) if evidence else None
            if receipt:
                self.last_settlement=receipt
                print(f"[RESULT] compatible source={receipt['source']} result={receipt['result']} net={receipt['net_pnl']:+.6f}",flush=True)
                return receipt['result']
            remaining=end-time.monotonic()
            if remaining<=0:
                await self.flush_evidence()
                print('[RESULT] bounded wait ended; no inferred loss, controls remain available',flush=True)
                return None
            try:await asyncio.wait_for(self.tracker.ready.wait(),min(.2,remaining))
            except asyncio.TimeoutError:pass

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
        self.disconnect_event.set()
        if self.connection_phase=='READY':self.connection_phase='CLOSED'
        task, self._recv_task = self._recv_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        if self.ws:
            with suppress(Exception):
                await self.ws.close()
        self.ws = None
