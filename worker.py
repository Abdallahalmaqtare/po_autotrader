"""Single-owner worker: settings at cycle boundaries, durable pending-order marker.
Stopping forbids new entries immediately; pending order is settled if time allows.
Account switches close immediately and preserve unresolved orders for manual review.
"""
import asyncio
from contextlib import suppress
from copy import deepcopy
from uuid import uuid4
import time
from telethon import TelegramClient
from telethon.sessions import StringSession
import config
from cloud_settings import auth_frame
from cloud_transport import CloudPocket
from martingale import MoneyManager
from telegram_signal import SignalListener


class TradingWorker:
    def __init__(self, repo, po_factory=CloudPocket, telegram_factory=TelegramClient):
        self.repo = repo
        self.po_factory = po_factory
        self.telegram_factory = telegram_factory
        self.po = None
        self.tg = None
        self.listener = None
        self.task = None
        self.stop_event = asyncio.Event()
        self.operations = asyncio.Lock()
        self.running = False
        self.in_flight = False
        self.last_error = ''
        self.stage = 'stopped'

    @property
    def pending(self):
        return self.repo.doc['runtime'].get('pending') if self.repo.doc else None

    @property
    def balance(self):
        return self.po.balance if self.po else None

    def request_stop(self):
        self.stop_event.set()
        if self.task and not self.in_flight:
            self.task.cancel()

    async def _stop(self, immediate=False):
        self.stop_event.set()
        if self.task:
            if immediate or not self.in_flight:
                self.task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(self.task), config.SHUTDOWN_GRACE_SECONDS)
            except asyncio.TimeoutError:
                self.task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await self.task
            except asyncio.CancelledError:
                pass
        self.task = None
        self.running = False

    async def stop(self):
        async with self.operations:
            await self._stop()
        return 'تم منع الدخول الجديد وإيقاف الحلقة. الصفقة المفتوحة إن وجدت لا يمكن إلغاؤها من المنصة.'

    async def _telegram(self):
        if self.tg and self.tg.is_connected():
            return
        session = StringSession(config.TELEGRAM_STRING_SESSION)
        self.tg = self.telegram_factory(session, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
        await self.tg.connect()
        if not await self.tg.is_user_authorized():
            raise RuntimeError('telegram_session_not_authorized')
        self.listener = SignalListener(self.tg)
        await self.listener.init()

    async def connect_account(self):
        if self.po:
            await self.po.close()
        mode = self.repo.doc['account_mode']
        key = 'real_account' if mode == 'REAL' else 'demo_account'
        credentials = deepcopy(self.repo.doc[key])
        auth_frame(credentials['ssid'], mode, credentials.get('uid', 0))
        self.po = self.po_factory(credentials, mode)
        await self.po.connect()

    async def start(self):
        async with self.operations:
            if self.task and not self.task.done():
                return 'الحلقة تعمل بالفعل أو جارٍ تشغيلها.'
            if not self.repo.leader:
                raise RuntimeError('lease_not_owned')
            if self.pending:
                raise ValueError('يوجد أمر سابق غير محسوم؛ راجع المنصة ثم استخدم زر مراجعة الأمر المعلّق.')
            mode = self.repo.doc['account_mode']
            key = 'real_account' if mode == 'REAL' else 'demo_account'
            auth_frame(self.repo.doc[key]['ssid'], mode, self.repo.doc[key].get('uid', 0))
            self.stop_event.clear()
            self.running = True  # set before task creation to prevent duplicate starts
            self.stage = 'starting'
            self.task = asyncio.create_task(self._run())
        return 'تم طلب البدء؛ راقب الحالة لتأكيد الاتصال. لا تُعد رسالة البدء تأكيداً لتنفيذ صفقة.'

    async def change(self, fields, reconnect=False):
        """Serialized account changes; ordinary trading changes start next cycle."""
        async with self.operations:
            was_running = self.running
            if reconnect:
                # No future entries on old account. Cancel wait but keep durable pending marker.
                await self._stop(immediate=True)
                if self.po:
                    await self.po.close()
            await self.repo.update(fields)
            if reconnect:
                if not self.pending:
                    await self.repo.reset_step()
                try:
                    await self.connect_account()
                except Exception as e:
                    self.last_error = type(e).__name__
                    self.stage = 'connection_failed'
                    return 'تم الحفظ في MongoDB، لكن الاتصال الجديد لم ينجح؛ راجع الجلسة والرابط ثم حاول البدء.'
                if was_running and not self.pending:
                    self.stop_event.clear()
                    self.running = True
                    self.task = asyncio.create_task(self._run())
                if self.pending:
                    return 'تم الحفظ والاتصال بالحساب الجديد؛ التداول متوقف حتى مراجعة الأمر المعلّق في الحساب السابق.'
                return 'تم الحفظ وإعادة الاتصال ببيانات الحساب المختار.'
        return 'تم الحفظ في MongoDB؛ يُطبّق التغيير من الدورة القادمة دون تعديل الصفقة المفتوحة.'

    async def _pause(self, delay):
        if delay <= 0:
            return
        try:
            await asyncio.wait_for(self.stop_event.wait(), delay)
        except asyncio.TimeoutError:
            pass

    def apply_snapshot(self, mode, trading):
        config.ACCOUNT_TYPE = mode
        config.IS_DEMO = 1 if mode == 'PRACTICE' else 0
        config.ASSET = trading['asset']
        config.DURATION_SECONDS = trading['duration_seconds']
        config.TRADE_AMOUNT = trading['trade_amount']
        config.BALANCE_SETTLE_SECONDS = trading['balance_settle_seconds']
        config.LOSS_RECHECK_SECONDS = trading['loss_recheck_seconds']

    async def _run(self):
        try:
            await self._telegram()
            mm = None
            signature = None
            while not self.stop_event.is_set():
                if not self.repo.leader:
                    raise RuntimeError('lease_lost')
                if self.pending:
                    raise RuntimeError('unresolved_order')
                mode = self.repo.doc['account_mode']
                t = deepcopy(self.repo.doc['trading'])
                self.apply_snapshot(mode, t)
                sig = (mode, t['asset'], t['trade_amount'], t['martingale_steps'])
                if sig != signature:
                    # New sizing/pair/account starts a fresh ladder, not an old oversized order.
                    saved = self.repo.doc['runtime'].get('sizing_signature')
                    if signature is not None or (saved is not None and list(sig) != saved):
                        await self.repo.reset_step()
                    mm = MoneyManager(t['trade_amount'], t['martingale_steps'])
                    mm.step = int(self.repo.doc['runtime'].get('martingale_step', 0))
                    signature = sig
                if not self.po or not self.po.connected or self.po.mode != mode:
                    self.stage = 'connecting'
                    try:
                        await self.connect_account()
                    except (TimeoutError, ConnectionError, OSError):
                        self.stage = 'reconnecting'
                        await self._pause(5)
                        continue
                await self.po.select_asset(t['asset'], t['duration_seconds'])
                # A baseline balance is necessary. Wait up to connection timeout,
                # but never send an order while no account balance is known.
                deadline = time.monotonic() + config.CONNECT_TIMEOUT_SECONDS
                while self.po.balance is None and time.monotonic() < deadline:
                    if self.stop_event.is_set():
                        return
                    await asyncio.sleep(.1)
                if self.po.balance is None:
                    raise RuntimeError('balance_unavailable')
                self.stage = 'waiting_signal'
                self.listener.drain_pending()
                if not await self.listener.press_new_signal():
                    await self._pause(2)
                    continue
                direction = await self.listener.wait_for_signal(config.SIGNAL_TIMEOUT_SECONDS)
                if not direction or self.stop_event.is_set():
                    continue
                await self._pause(t['entry_delay_seconds'])
                if self.stop_event.is_set():
                    break
                # Changes received while waiting invalidate this old signal.
                if t != self.repo.doc['trading'] or mode != self.repo.doc['account_mode']:
                    continue
                if not self.po.connected:
                    continue  # fresh connection needs a fresh signal, not a replay
                order = dict(id=uuid4().hex, mode=mode, amount=mm.current_amount(),
                             asset=t['asset'], direction=direction,
                             created_at=time.time(), step=mm.step, sizing_signature=list(sig))
                if order['amount'] > self.po.balance:
                    raise RuntimeError('insufficient_balance')
                # Persist BEFORE send: redeploy must never automatically repeat an uncertain order.
                await self.repo.reserve(order)
                if self.stop_event.is_set():
                    await self.repo.resolve(order, None, None, None, mm.step, dispatched=False)
                    break
                self.in_flight = True
                self.stage = 'order_pending'
                ok = await asyncio.wait_for(self.po.place_trade(direction, order['amount']),
                                             config.CONNECT_TIMEOUT_SECONDS)
                if not ok:
                    raise RuntimeError('dispatch_uncertain')
                before = self.po._balance_before
                result = await self.po.wait_for_result(t['duration_seconds'] + t['result_timeout_extra'])
                if not self.po.connected or result is None:
                    raise RuntimeError('result_unconfirmed_review_required')
                mm.register_result(result)
                await self.repo.resolve(order, result, before, self.po.balance, mm.step)
                self.in_flight = False
                self.stage = 'delay'
                await self._pause(t['loop_delay_seconds'])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # No exception message: third-party errors can contain account/session values.
            self.last_error = type(e).__name__
            self.stage = 'error_review' if self.pending else 'error'
            print('[WORKER] paused; error type:', self.last_error, flush=True)
        finally:
            self.running = False
            self.in_flight = False
            if self.stage not in ('error', 'error_review'):
                self.stage = 'stopped'

    async def close(self):
        await self.stop()
        if self.po:
            await self.po.close()
        if self.tg:
            await self.tg.disconnect()
