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
import config
from cloud_settings import auth_frame
from cloud_transport import CloudPocket
from martingale import MoneyManager
from telegram_signal import SignalListener
from telegram_bootstrap import session_from_env, signal_username
from diagnostics import SafeIssue, diagnose_exception


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
        self.diagnostic = None
        self.notify = None
        self._last_notice = None
        self._notice_at = 0.0

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

    async def record_error(self, exc, send_notification=True):
        diagnostic = diagnose_exception(exc, self.stage)
        self.diagnostic = diagnostic
        self.last_error = diagnostic.code
        print(diagnostic.log_line(), flush=True)
        key = (diagnostic.code, diagnostic.stage)
        now = time.monotonic()
        if send_notification and self.notify and (key != self._last_notice or now - self._notice_at >= 30):
            self._last_notice, self._notice_at = key, now
            try:
                await asyncio.wait_for(self.notify(diagnostic.public_text()), timeout=5)
            except Exception:
                pass  # Telegram retry outages must not hide the original issue
        return diagnostic

    async def _telegram(self):
        # A live socket is NOT sufficient: v11 could leave an uninitialized
        # client after get_entity failed, then skip initialization on retry.
        if self.tg and self.tg.is_connected() and self.listener is not None:
            return
        if self.tg:
            with suppress(Exception):
                await self.tg.disconnect()
        self.tg, self.listener = None, None
        self.stage = 'telegram_session_parse'
        session = session_from_env(config.TELEGRAM_STRING_SESSION)
        config.SIGNAL_BOT_USERNAME = signal_username(config.SIGNAL_BOT_USERNAME)
        try:
            self.stage = 'telegram_client_create'
            self.tg = self.telegram_factory(session, config.TELEGRAM_API_ID, config.TELEGRAM_API_HASH)
            self.stage = 'telegram_connect'
            async with asyncio.timeout(config.CONNECT_TIMEOUT_SECONDS):
                await self.tg.connect()
                self.stage = 'telegram_authorize'
                if not await self.tg.is_user_authorized():
                    raise SafeIssue('TG_SESSION_UNAUTHORIZED')
                self.stage = 'signal_bot_resolve'
                listener = SignalListener(self.tg)
                await listener.init()
                if not getattr(listener.bot_entity, 'bot', False):
                    raise SafeIssue('SIGNAL_TARGET_NOT_BOT')
                self.listener = listener
        except BaseException:
            if self.tg:
                with suppress(Exception):
                    await self.tg.disconnect()
            self.tg, self.listener = None, None
            raise

    async def diagnose(self):
        """Connection-only diagnostics; never presses New signal or sends an order."""
        async with self.operations:
            if self.task and not self.task.done():
                return 'أوقف التداول أولاً لإجراء فحص اتصال مستقل؛ لن تتم مقاطعة صفقة جارية.'
            try:
                self.stage = 'telegram_session_parse'
                session_from_env(config.TELEGRAM_STRING_SESSION)
                self.stage = 'signal_bot_resolve'
                signal_username(config.SIGNAL_BOT_USERNAME)
                await self._telegram()
                self.stage = 'pocket_credentials'
                await self.connect_account()
                self.diagnostic = None
                self.last_error = ''
                self.stage = 'diagnostics_ok'
                return ('نجح اتصال حساب تيليجرام وحل اسم بوت الإشارات ومصادقة المنصة. '
                        'لم تُطلب إشارة ولم يُرسل أي أمر تداول. النجاح لا يضمن صلاحية كل صفقة لاحقة.')
            except Exception as exc:
                diagnostic = await self.record_error(exc, send_notification=False)
                self.stage = 'error_review' if self.pending else 'error'
                return diagnostic.public_text()

    async def connect_account(self):
        if self.po:
            await self.po.close()
        mode = self.repo.doc['account_mode']
        key = 'real_account' if mode == 'REAL' else 'demo_account'
        credentials = deepcopy(self.repo.doc[key])
        self.stage = 'pocket_credentials'
        try:
            auth_frame(credentials['ssid'], mode, credentials.get('uid', 0))
        except (ValueError, TypeError, KeyError):
            raise SafeIssue('PO_CREDENTIALS_INVALID') from None
        self.stage = 'pocket_connect'
        self.po = self.po_factory(credentials, mode)
        await self.po.connect()

    async def start(self):
        async with self.operations:
            if self.task and not self.task.done():
                return 'الحلقة تعمل بالفعل أو جارٍ تشغيلها.'
            if not self.repo.leader:
                raise RuntimeError('lease_not_owned')
            if self.pending:
                await self.repo.acknowledge_pending()
                print('[WORKER] previous uncertain order auto-archived as UNCONFIRMED '
                      '(never resent); starting fresh', flush=True)
            mode = self.repo.doc['account_mode']
            key = 'real_account' if mode == 'REAL' else 'demo_account'
            try:
                self.stage = 'pocket_credentials'
                auth_frame(self.repo.doc[key]['ssid'], mode, self.repo.doc[key].get('uid', 0))
                self.stage = 'telegram_session_parse'
                session_from_env(config.TELEGRAM_STRING_SESSION)
                self.stage = 'signal_bot_resolve'
                signal_username(config.SIGNAL_BOT_USERNAME)
            except Exception as exc:
                diagnostic = await self.record_error(exc, send_notification=False)
                self.stage = 'error'
                raise ValueError(diagnostic.public_text()) from None
            self.diagnostic = None
            self.last_error = ''
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
                    diagnostic = await self.record_error(e, send_notification=False)
                    self.stage = 'connection_failed'
                    return 'تم الحفظ في MongoDB لكن لم ينجح الاتصال الجديد.\n' + diagnostic.public_text()
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
            chain_stake = 0.0
            while not self.stop_event.is_set():
                if not self.repo.leader:
                    raise RuntimeError('lease_lost')
                if self.pending:
                    await self.repo.acknowledge_pending()
                    print('[WORKER] stale pending marker auto-archived; continuing', flush=True)
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
                    chain_stake = 0.0
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
                    if getattr(self.po, 'saw_other_account_balance', False):
                        raise SafeIssue('PO_ACCOUNT_MISMATCH')
                    raise SafeIssue('PO_BALANCE_MISSING')
                self.stage = 'waiting_signal'
                self.listener.drain_pending()
                self.stage = 'signal_button'
                if not await self.listener.press_new_signal():
                    await self._pause(2)
                    continue
                self.stage = 'signal_wait'
                direction = await self.listener.wait_for_signal(config.SIGNAL_TIMEOUT_SECONDS)
                if not direction or self.stop_event.is_set():
                    continue
                self.stage = 'entry_delay'
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
                    raise SafeIssue('INSUFFICIENT_BALANCE')
                # Persist BEFORE send: redeploy must never automatically repeat an uncertain order.
                self.stage = 'order_reserve'
                await self.repo.reserve(order)
                if self.stop_event.is_set():
                    await self.repo.resolve(order, None, None, None, mm.step, dispatched=False)
                    break
                self.in_flight = True
                self.stage = 'order_send'
                before = None
                result = None
                try:
                    ok = await asyncio.wait_for(self.po.place_trade(direction, order['amount']),
                                                 config.CONNECT_TIMEOUT_SECONDS)
                    before = self.po._balance_before
                    if ok:
                        self.stage = 'order_result'
                        result = await self.po.wait_for_result(
                            t['duration_seconds'] + t['result_timeout_extra'])
                except Exception as send_err:
                    print(f'[WORKER] send/result error ({type(send_err).__name__}) - continuing', flush=True)
                    ok = False
                chain_stake += order['amount']
                prev_step = mm.step
                if result is None:
                    # Uncertain outcome: NEVER stop and never force a manual review.
                    # Keep the SAME martingale step (no doubling on an unverified trade),
                    # archive the order as unconfirmed, reconnect automatically and go on.
                    print('[WORKER] UNCONFIRMED -> amount kept at current step; '
                          'auto-reconnecting and continuing without review', flush=True)
                    self.stage = 'result_save'
                    await self.repo.resolve(order, None, before, self.po.balance, mm.step)
                    self.in_flight = False
                    self.stage = 'delay'
                    await self._pause(t['loop_delay_seconds'])
                    continue
                mm.register_result(result)
                seq_lost = 0
                seq_lost_amount = 0.0
                if result == 'win':
                    chain_stake = 0.0                     # sequence closed with a win
                elif result == 'loss':
                    if prev_step >= mm.max_doubles - 1:
                        # ALL doubling steps lost -> ONE full sequence loss (user rule)
                        seq_lost = 1
                        seq_lost_amount = chain_stake
                        print(f'[WORKER] FULL SEQUENCE LOST ({mm.max_doubles} steps) '
                              f'-> counted as ONE lost trade, lost stake ${chain_stake:g}', flush=True)
                        chain_stake = 0.0
                # draw: chain_stake kept (same step repeats with same amount)
                self.stage = 'result_save'
                await self.repo.resolve(order, result, before, self.po.balance, mm.step,
                                        seq_lost=seq_lost, seq_lost_amount=seq_lost_amount)
                self.in_flight = False
                self.stage = 'delay'
                await self._pause(t['loop_delay_seconds'])
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self.record_error(e)
            self.stage = 'error_review' if self.pending else 'error'
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
