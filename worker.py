"""Single-owner worker: settings at cycle boundaries, durable pending-order marker.
Stopping forbids new entries immediately; pending order is settled if time allows.
Account switches close immediately and preserve unresolved orders for manual review.
"""
import asyncio
from runtime_support import timestamp_print as print
from contextlib import suppress
from copy import deepcopy
from uuid import uuid4
import time
from telethon import TelegramClient
import config
from cloud_settings import auth_frame
from account_details import credentials_match
from connection_lifecycle import OrderNotSent
from cloud_transport import CloudPocket
from martingale import MoneyManager
from synchronized_signals import SynchronizedSignalListener as SignalListener
from meta_navigation import NavigationError
from payout_guard import PayoutBlocked
from payout_workflow import PayoutWorkflow
from work_cycle import CycleWorkflow, cycle_state, transition
from telegram_bootstrap import session_from_env, signal_username
from diagnostics import SafeIssue, diagnose_exception
from risk_limits import risk_state, limit_stop_text
from recovery import sizing, selected_state


class TradingWorker(PayoutWorkflow, CycleWorkflow):
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
        self._last_limit_notice = None
        self.selection_changed = asyncio.Event()
        self.payout_note = ''
        self.payout_refresh_at = 0.0
        self.payout_refresh_requested=False
        self.cycle_note=''
        self.cycle_force_rest=False

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
            if self.pending and self.pending.get('strict_result'):
                return 'هناك صفقة غير محسومة؛ استخدم تحقق من الصفقة المعلّقة دون قطع الاتصال.'
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

    async def add_account(self,credentials,mode,name=''):
        from account_registry import make_record,account_key
        record=make_record(credentials,mode,name)
        key=account_key(record['uid'],mode)
        async with self.operations:
            if self.in_flight or self.pending:raise ValueError('انتظر حسم الصفقة المعلّقة قبل تعديل الحسابات')
            if key in self.repo.doc.get('accounts',{}):
                from account_registry import DuplicateAccount
                raise DuplicateAccount(key)
            if key==self.repo.doc.get('active_account_id') and self.running:
                raise ValueError('أوقف التداول أولاً لتحديث جلسة الحساب النشط')
            result=await self.repo.save_account(credentials,mode,name)
            if key==self.repo.doc.get('active_account_id') and self.po:
                await self.po.close()
            return result

    async def edit_account(self,key,credentials=None,name=None):
        async with self.operations:
            if self.in_flight or self.pending:raise ValueError('انتظر حسم الصفقة المعلّقة قبل التعديل')
            active=key==self.repo.doc.get('active_account_id')
            if active and self.running:raise ValueError('أوقف التداول أولاً قبل تعديل الحساب النشط')
            if credentials is not None:
                from connection_lifecycle import connection_url
                credentials=deepcopy(credentials);credentials['ws_url']=connection_url(credentials['ws_url'])
            await self.repo.edit_account(key,credentials,name)
            if active and credentials is not None:
                if self.po:await self.po.close()
                self.po=None;self.stage='credentials_updated';self.last_error='';self.diagnostic=None
            return 'تم حفظ التعديل دون تغيير UID أو الإحصائيات.'

    async def delete_account(self,key):
        async with self.operations:
            if self.in_flight or self.pending:raise ValueError('انتظر حسم الصفقة المفتوحة قبل الحذف')
            active=key==self.repo.doc.get('active_account_id')
            if active:
                await self._stop(immediate=True)
                if self.po:await self.po.close()
                self.po=None
            await self.repo.delete_account(key)
            if active:self.stage='no_account_selected';self.last_error='';self.diagnostic=None
            return 'تم حذف الحساب وبيانات اتصاله من القائمة. بقي سجل نتائجه مؤرشفاً دون أسرار؛ لم يُختر حساب آخر تلقائياً.'

    async def reconnect_account(self,key=None):
        async with self.operations:
            if key is not None and key!=self.repo.doc.get('active_account_id'):
                return 'اختر هذا الحساب أولاً؛ اختبار الاتصال لا يغير الحساب النشط تلقائياً.'
            if self.in_flight or self.pending:return 'توجد صفقة معلّقة؛ استخدم التحقق من نتيجتها دون إعادة إرسالها.'
            if self.running:return 'التداول يعمل؛ أوقفه أولاً لإجراء اختبار اتصال مستقل.'
            try:
                await self.connect_account()
                deadline=time.monotonic()+config.CONNECT_TIMEOUT_SECONDS
                while self.po.connected and self.po.balance is None and time.monotonic()<deadline:
                    await asyncio.sleep(.1)
                if not self.po.connected:raise SafeIssue(getattr(self.po,'last_connection_code','') or 'PO_DISCONNECTED')
                if self.po.balance is None:raise SafeIssue('PO_BALANCE_MISSING')
                self.diagnostic=None;self.last_error='';self.stage='account_ready'
                return '✅ WebSocket متصل، والمصادقة تمت ووصل الرصيد. التداول متوقف حتى تضغط بدء.'
            except Exception as exc:
                diagnostic=await self.record_error(exc,send_notification=False)
                self.stage='connection_error'
                return diagnostic.public_text()

    async def select_account(self,key):
        async with self.operations:
            if self.in_flight or self.pending:raise ValueError('انتظر حسم الصفقة المفتوحة قبل تبديل الحساب')
            if key not in self.repo.doc.get('accounts',{}):raise ValueError('الحساب غير موجود؛ حدّث القائمة')
            if key==self.repo.doc.get('active_account_id'):
                matches=self.po and self.po.connected and getattr(self.po,'mode',None)==self.repo.doc['account_mode'] and credentials_match(self.po.credentials,self.repo.doc['accounts'][key]['credentials'],self.repo.doc['account_mode'])
                if matches:return 'هذا هو الحساب النشط والمتصل بالفعل؛ لم يتغير التداول أو السجل.'
                if self.running:raise ValueError('أوقف التداول أولاً لإعادة اتصال الحساب النشط')
                try:
                    await self.connect_account();self.stage='account_ready'
                    return 'تمت إعادة اتصال الحساب النشط. التداول متوقف حتى تضغط بدء.'
                except Exception as exc:
                    diagnostic=await self.record_error(exc,send_notification=False)
                    return diagnostic.public_text()
            await self._stop(immediate=True)
            if self.po:await self.po.close()
            await self.repo.select_account(key)
            self.stop_event.set();self.stage='account_selected'
            # Selection is not a Start command and never restarts REAL trading.
            try:
                await self.connect_account()
                return 'تم اختيار الحساب والاتصال به. التداول متوقف؛ اضغط بدء عند الاستعداد.'
            except Exception as exc:
                diagnostic=await self.record_error(exc,send_notification=False)
                return 'تم اختيار الحساب؛ تعذّر الاتصال. التداول متوقف.\n'+diagnostic.public_text()

    async def _save_pending_receipt(self,order,receipt):
        if not receipt:return False
        current=self.repo.doc['runtime'].get('pending')
        if not current or current['id']!=order['id']:return False
        order=deepcopy(order);order['settlement']=deepcopy(receipt)
        mm=MoneyManager(order.get('effective_base',order['amount']/(2**order['step'])),order.get('martingale_steps',4))
        mm.step=order['step'];result=receipt['result'];mm.register_result(result)
        seq_lost=int(result=='loss' and order['step']>=mm.max_doubles-1)
        loss_amount=float(order.get('sequence_loss_before',0))+abs(receipt['net_pnl']) if seq_lost else 0.0
        await self.repo.resolve(order,result,None,None,mm.step,seq_lost=seq_lost,seq_lost_amount=loss_amount)
        self.stage='settlement_confirmed'
        await self.enforce_limits()
        if self.notify:
            with suppress(Exception):
                await asyncio.wait_for(self.notify(f"✅ حُسمت الصفقة المتأخرة: {result} | الصافي ${receipt['net_pnl']:+.2f}\nالتداول متوقف؛ لم تُكرر الصفقة."),5)
        return True

    async def _watch_pending_settlement(self,order,po):
        try:
            # Keep observing the SAME live socket; never resubmit or navigate.
            await asyncio.wait_for(po.tracker.ready.wait(),600)
            await po.flush_evidence()
            if po.tracker.conflict:return
            async with self.operations:
                if self.po is po:await self._save_pending_receipt(order,po.tracker.settlement)
        except (TimeoutError,asyncio.CancelledError):pass
        except Exception as exc:
            print(f'[RESULT] late settlement not committed ({type(exc).__name__})',flush=True)

    async def reconcile_pending(self):
        async with self.operations:
            order=deepcopy(self.pending)
            if not order:return 'لا توجد صفقة معلّقة.'
            if not order.get('strict_result'):return 'الأمر من إصدار قديم بلا معرّف منصة موثوق؛ راجعه على المنصة.'
            if self.running or self.in_flight:return 'جارٍ انتظار نتيجة الصفقة الحالية؛ لم تُعد إرسالها.'
            if self.po and getattr(self.po,'tracker',None) and self.po.tracker.order['id']==order['id']:
                if await self._save_pending_receipt(order,self.po.tracker.settlement):return 'حُفظت نتيجة الصفقة المؤكدة مرة واحدة.'
                return 'لم تصل نتيجة إغلاق مؤكدة بعد؛ لا خسارة ولا تدبيل.'
            await self.connect_account(pending_order=order)
            result=await self.po.wait_for_result(min(30,config.CONNECT_TIMEOUT_SECONDS))
            if result and await self._save_pending_receipt(order,self.po.last_settlement):return 'تمت مطابقة الصفقة وحفظ نتيجتها.'
            return 'لم تصل نتيجة قابلة للمطابقة؛ التداول متوقف دون احتساب خسارة.'

    async def connect_account(self,pending_order=None):
        if self.pending and self.pending.get('strict_result') and pending_order is None:
            raise ValueError('لا نعيد الاتصال قبل حسم الصفقة المعلّقة')
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
        if getattr(self.po,'strict_results',False) is True:
            self.po.on_evidence=self.repo.record_broker_id
            if pending_order:self.po.prepare_order(pending_order)
        await self.po.connect()

    async def enforce_limits(self, notify=True):
        state = risk_state(self.repo.doc)
        if not state['reason']:
            self._last_limit_notice = None
            return False
        self.stop_event.set()
        self.stage = 'limit_reached'
        # If a setting is lowered mid-wait, cancel the waiting task immediately.
        # Never cancel an already-open trade's result collection.
        if self.task and self.task is not asyncio.current_task() and not self.in_flight:
            self.task.cancel()
        key = (state['account_mode'], state['reason'], state['net_pnl'],
               state['profit_limit'], state['loss_limit'])
        if notify and key != self._last_limit_notice:
            self._last_limit_notice = key
            print(f"[RISK] {state['reason']} account={state['account_mode']} "
                  f"net={state['net_pnl']:.6f}; new entries disabled", flush=True)
            if self.notify:
                with suppress(Exception):
                    await asyncio.wait_for(self.notify(limit_stop_text(self.repo.doc)), timeout=5)
        return True

    async def start(self):
        async with self.operations:
            if self.task and not self.task.done():
                return 'الحلقة تعمل بالفعل أو جارٍ تشغيلها.'
            if not self.repo.leader:
                raise RuntimeError('lease_not_owned')
            if await self.enforce_limits(notify=False):
                return limit_stop_text(self.repo.doc)
            if self.pending and self.pending.get('strict_result'):
                return 'توجد صفقة غير محسومة. استخدم تحقق من الصفقة المعلّقة؛ لن تُعاد أو تُحسب خسارة.'
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
            if self.repo.doc['runtime']['cycle']['phase']=='DONE':
                await self.repo.save_cycle(cycle_state(),deepcopy(self.repo.doc['schedule']))
            self.stop_event.clear()
            self.running = True  # set before task creation to prevent duplicate starts
            self.stage = 'starting'
            self.task = asyncio.create_task(self._run())
        return 'تم طلب البدء؛ راقب الحالة لتأكيد الاتصال. لا تُعد رسالة البدء تأكيداً لتنفيذ صفقة.'

    async def change(self, fields, reconnect=False):
        """Serialized account changes; ordinary trading changes start next cycle."""
        async with self.operations:
            if reconnect and (self.in_flight or (self.pending and self.pending.get('strict_result'))):
                raise ValueError('لا يمكن تغيير بيانات الحساب قبل حسم الصفقة المفتوحة')
            was_running = self.running
            selection_requested = any(key in fields for key in ('trading.asset','trading.duration_seconds'))
            if reconnect:
                # No future entries on old account. Cancel wait but keep durable pending marker.
                await self._stop(immediate=True)
                if self.po:
                    await self.po.close()
            await self.repo.update(fields)
            if selection_requested or any(key.startswith(('payout.','schedule.')) for key in fields):
                self.selection_changed.set()
            if await self.enforce_limits():
                return 'تم الحفظ.\n' + limit_stop_text(self.repo.doc)
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
            if selection_requested and not self.running:
                try:
                    return await self.synchronize_selection()
                except Exception as exc:
                    diagnostic=await self.record_error(exc,send_notification=False)
                    return 'تم حفظ الإعداد، لكن لم تكتمل المزامنة.\n'+diagnostic.public_text()
            if selection_requested:
                return 'تم الحفظ؛ ستتم مزامنة MetaTrading قبل الدخول التالي، وبعد حسم الصفقة المفتوحة إن وجدت.'
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
                if await self.enforce_limits():
                    break
                if not await self.cycle_gate(int(self.repo.doc['runtime'].get('martingale_step',0))):
                    continue
                if self.pending and self.pending.get('strict_result'):
                    self.stage='result_unconfirmed';self.stop_event.set();break
                if self.pending:
                    await self.repo.acknowledge_pending()
                    print('[WORKER] stale pending marker auto-archived; continuing', flush=True)
                mode = self.repo.doc['account_mode']
                self.selection_changed.clear()
                t = deepcopy(self.repo.doc['trading'])
                payout_policy = deepcopy(self.repo.doc['payout'])
                recovery_policy = deepcopy(self.repo.doc['recovery'])
                effective_base, recovery_context = sizing(self.repo.doc, mode)
                self.apply_snapshot(mode, t)
                sig = (mode, t['asset'], effective_base, t['martingale_steps'])
                if self.repo.doc.get('active_account_id'):
                    sig += (self.repo.doc['active_account_id'],)
                if recovery_context['enabled']:
                    sig += (recovery_context['epoch'],)
                if sig != signature:
                    # New sizing/pair/account starts a fresh ladder, not an old oversized order.
                    saved = self.repo.doc['runtime'].get('sizing_signature')
                    if signature is not None or (saved is not None and list(sig) != saved):
                        await self.repo.reset_step()
                    mm = MoneyManager(effective_base, t['martingale_steps'])
                    mm.step = int(self.repo.doc['runtime'].get('martingale_step', 0))
                    signature = sig
                    chain_stake = float(self.repo.doc['runtime'].get('sequence_loss_amount',0))
                if not self.po or not self.po.connected or self.po.mode != mode:
                    self.stage = 'connecting'
                    try:
                        await self.connect_account()
                    except SafeIssue as exc:
                        if exc.code not in ('PO_CONNECT_TIMEOUT','PO_DISCONNECTED','PO_HEARTBEAT_TIMEOUT','PO_CONNECT_FAILED'):raise
                        await self.record_error(exc,send_notification=False)
                        self.stage='reconnecting';await self._pause(5);continue
                    except (TimeoutError, ConnectionError, OSError):
                        self.stage = 'reconnecting'
                        await self._pause(5)
                        continue
                try:
                    await self.po.select_asset(t['asset'], t['duration_seconds'])
                except Exception as exc:
                    if self.po.connected:raise
                    self.stage='reconnecting';await self._pause(1);continue
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
                if not await self.guard_payout(t,payout_policy,mode):
                    continue
                self.stage = 'meta_signal'
                signal_task=asyncio.create_task(self.listener.next_direction(t['asset'],t['duration_seconds'],self.stop_event))
                changed_task=asyncio.create_task(self.selection_changed.wait())
                payout_task=asyncio.create_task(self.wait_payout_change(t,payout_policy))
                cycle_task=asyncio.create_task(self.wait_cycle_boundary(mm.step))
                closed=getattr(self.po,'closed_event',None)
                connection_task=asyncio.create_task(closed.wait() if isinstance(closed,asyncio.Event) else asyncio.Event().wait())
                try:
                    done,_=await asyncio.wait((signal_task,changed_task,payout_task,cycle_task,connection_task),return_when=asyncio.FIRST_COMPLETED)
                    if connection_task in done:
                        disarm=getattr(self.listener,"disarm",None)
                        if callable(disarm):disarm()
                        self.stage="reconnecting"
                        continue
                    if changed_task in done or payout_task in done or cycle_task in done:
                        continue
                    try:
                        direction=await signal_task
                    except NavigationError as exc:
                        print(f'[META] request invalidated: {exc}',flush=True)
                        self.stage='meta_retry'
                        await self._pause(2)
                        continue
                finally:
                    for task in (signal_task,changed_task,payout_task,cycle_task,connection_task):
                        if not task.done(): task.cancel()
                        with suppress(asyncio.CancelledError,Exception):
                            await task
                if not direction or self.stop_event.is_set():
                    continue
                self.stage = 'entry_delay'
                await self._pause(t['entry_delay_seconds'])
                if self.stop_event.is_set():
                    break
                # Changes received while waiting invalidate this old signal.
                if (t != self.repo.doc['trading'] or mode != self.repo.doc['account_mode']
                        or payout_policy != self.repo.doc['payout']
                        or recovery_policy != self.repo.doc['recovery']
                        or recovery_context['epoch'] != selected_state(self.repo.doc, mode)['epoch']):
                    continue
                if not self.po.connected:
                    continue  # fresh connection needs a fresh signal, not a replay
                if await self.enforce_limits():
                    break
                if not await self.cycle_gate(mm.step):
                    continue
                if not await self.guard_payout(t,payout_policy,mode,wait=False):
                    continue
                if not self.listener.verifies(t['asset'],t['duration_seconds'],max_age=max(10,t['entry_delay_seconds']+2)):
                    continue
                order = dict(id=uuid4().hex, mode=mode, amount=mm.current_amount(),
                             asset=t['asset'], direction=direction,
                             created_at=time.time(), step=mm.step, sizing_signature=list(sig),
                             recovery_context=deepcopy(recovery_context),
                             account_id=self.repo.doc.get('active_account_id'),duration_seconds=t['duration_seconds'],
                             strict_result=getattr(self.po,'strict_results',False) is True,
                             request_id=uuid4().hex,martingale_steps=t['martingale_steps'],effective_base=effective_base,
                             sequence_loss_before=self.repo.doc['runtime'].get('sequence_loss_amount',0.0))
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
                    self.po.entry_check=lambda: self.entry_eligible(t,payout_policy,mode)
                    if order['strict_result']:self.po.prepare_order(order)
                    ok = await asyncio.wait_for(self.po.place_trade(direction, order['amount']),
                                                 config.CONNECT_TIMEOUT_SECONDS)
                    before = self.po._balance_before
                    if ok:
                        self.stage = 'order_result'
                        result = await self.po.wait_for_result(
                            t['duration_seconds'] + t['result_timeout_extra'])
                except OrderNotSent:
                    await self.repo.resolve(order,None,None,None,mm.step,dispatched=False)
                    self.in_flight=False;self.stage='reconnecting'
                    disarm=getattr(self.listener,'disarm',None)
                    if callable(disarm):disarm()
                    continue
                except PayoutBlocked:
                    await self.repo.resolve(order,None,None,None,mm.step,dispatched=False)
                    self.in_flight=False
                    self.stage='payout_recheck'
                    continue
                except Exception as send_err:
                    print(f'[WORKER] send/result error ({type(send_err).__name__}) - continuing', flush=True)
                    ok = False
                receipt=getattr(self.po,'last_settlement',None) if order['strict_result'] else None
                if order['strict_result'] and (not receipt or receipt.get('order_id')!=order['id']):result=None
                if receipt:order['settlement']=deepcopy(receipt)
                if result=='loss':chain_stake += abs(receipt['net_pnl']) if receipt else order['amount']
                prev_step = mm.step
                if result is None and order['strict_result']:
                    await self.po.flush_evidence()
                    await self.repo.mark_uncertain(order)
                    self.in_flight=False;self.stop_event.set();self.stage='result_unconfirmed'
                    print('[RESULT] unresolved order retained; no loss, no recovery debt, no new entries',flush=True)
                    if getattr(self.po,'tracker',None):
                        self.settlement_watch_task=asyncio.create_task(self._watch_pending_settlement(deepcopy(self.pending),self.po))
                    if self.notify:
                        with suppress(Exception):await asyncio.wait_for(self.notify('⏸ نتيجة الصفقة لم تتأكد بعد. التداول متوقف دون خسارة أو تدبيل؛ ننتظر نفس معرّف الصفقة.'),5)
                    break
                if result is None:
                    # Uncertain outcome: NEVER stop and never force a manual review.
                    # Keep the SAME martingale step (no doubling on an unverified trade),
                    # archive the order as unconfirmed, reconnect automatically and go on.
                    print('[WORKER] UNCONFIRMED -> amount kept at current step; '
                          'auto-reconnecting and continuing without review', flush=True)
                    self.stage = 'result_save'
                    await self.repo.resolve(order, None, before, self.po.balance, mm.step)
                    self.in_flight = False
                    self.cycle_force_rest=True
                    if await self.enforce_limits():
                        break
                    if not await self.cycle_gate(mm.step):
                        continue
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
                if await self.enforce_limits():
                    break
                if not await self.cycle_gate(mm.step):
                    continue
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
            if self.stage not in ('error', 'error_review', 'limit_reached', 'payout_stopped', 'time_limit_reached','result_unconfirmed','settlement_confirmed'):
                self.stage = 'stopped'

    async def close(self):
        watch=getattr(self,'settlement_watch_task',None)
        if watch and not watch.done():
            watch.cancel()
            with suppress(asyncio.CancelledError):await watch
        await self.stop()
        if self.po:
            await self.po.close()
        if self.tg:
            await self.tg.disconnect()
