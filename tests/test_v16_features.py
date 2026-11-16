"""Offline end-to-end tests using real Repository, worker, aiogram and Telethon.
All credentials below are synthetic fixtures. No external network or live trades.
"""
import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from pymongo import ReturnDocument
from cloud_settings import defaults, patched
from database import Repository, utcnow
from risk_limits import risk_state
from account_details import account_identity, account_text
from telegram_signal import SignalListener, parse_direction
from cloud_transport import CloudPocket, EventDecoder
from control_bot import build_dispatcher, home, settings_kb, limits_kb, report, AdminOnly
from worker import TradingWorker
from ui_helpers import FakeBot, synchronized_fixture
import config


def get_path(obj, key):
    for part in key.split('.'):
        if not isinstance(obj, dict) or part not in obj:
            return None
        obj = obj[part]
    return obj


def set_path(obj, key, value):
    parts = key.split('.')
    for part in parts[:-1]:
        obj = obj.setdefault(part, {})
    obj[parts[-1]] = deepcopy(value)


def matches(doc, query):
    for key, value in query.items():
        if key == '$or':
            if not any(matches(doc, branch) for branch in value):
                return False
            continue
        stored = get_path(doc, key)
        if isinstance(value, dict):
            for op, target in value.items():
                if op == '$gt' and (stored is None or not stored > target): return False
                if op == '$lte' and (stored is None or not stored <= target): return False
                if op == '$exists' and (stored is not None) != target: return False
        elif stored != value:
            return False
    return True


class MemoryCollection:
    """Mongo-like single-document test double; uses real Repository operations."""
    def __init__(self, doc=None):
        self.doc = deepcopy(doc)
        self.fail_write = False

    async def find_one(self, query):
        return deepcopy(self.doc) if self.doc is not None and matches(self.doc, query) else None

    async def update_one(self, query, update, upsert=False):
        if self.fail_write:
            raise OSError('synthetic database unavailable')
        inserted = False
        if self.doc is None and upsert:
            self.doc = deepcopy(update.get('$setOnInsert', {}))
            inserted = True
        if self.doc is None or not matches(self.doc, query):
            return SimpleNamespace(matched_count=0)
        for key, value in update.get('$set', {}).items():
            set_path(self.doc, key, value)
        for key, value in update.get('$inc', {}).items():
            set_path(self.doc, key, (get_path(self.doc, key) or 0) + value)
        for key in update.get('$unset', {}):
            self.doc.pop(key, None)
        return SimpleNamespace(matched_count=1, upserted_id='current_settings' if inserted else None)

    async def find_one_and_update(self, query, update, **kwargs):
        if not self.doc or not matches(self.doc, query):
            return None
        await self.update_one(query, update)
        return deepcopy(self.doc)


async def real_repo(doc=None):
    collection = MemoryCollection(doc or defaults())
    repo = Repository('', '', collection=collection)
    await repo.initialize()
    await repo.acquire()
    return repo


def auth(uid, demo=1):
    return '42' + json.dumps(['auth', {'uid': uid, 'isDemo': demo, 'session': 'offline-only'}])


class DirectionTests(unittest.TestCase):
    def test_both_photographed_formats(self):
        self.assertEqual(parse_direction('Asset: CAD/CHF (OTC)\nDirection: UP\nAccuracy rate: 55%'), 'UP')
        self.assertEqual(parse_direction('AUD/USD (OTC)\n5 sec\nCurrent Time (UTC+3):\n18:26:07\nMeta Signal: Lower'), 'DOWN')

    def test_all_combinations_case_whitespace(self):
        for label in ('Direction', 'Meta Signal', 'meta  signal'):
            for value, target in [('UP','UP'), ('DOWN','DOWN'), ('Higher','UP'), ('lower','DOWN')]:
                with self.subTest(label=label, value=value):
                    self.assertEqual(parse_direction(f'{label}:\n{value}'), target)

    def test_telegram_unicode_and_markdown(self):
        self.assertEqual(parse_direction('Meta\u00a0Signal: **Hi\u200bgher**'), 'UP')
        self.assertEqual(parse_direction('\u200fMeta Signal: `Lower`'), 'DOWN')

    def test_no_false_positive_or_conflicting_trade(self):
        for text in ('Higher', 'Meta is preparing the signal...', 'Direction: UPWARD',
                     'MyDirection: DOWN', 'Direction: DOWNSTREAM',
                     'Direction: UP\nMeta Signal: Lower', 'Accuracy rate: 68%', ''):
            self.assertIsNone(parse_direction(text), text)


class SignalEventsTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_and_edited_messages_normalized_and_deduplicated(self):
        registrations = []
        def on(builder):
            def decorate(fn):
                registrations.append((builder, fn))
                return fn
            return decorate
        client = SimpleNamespace(get_entity=AsyncMock(return_value=SimpleNamespace(bot=True)), on=on)
        listener = SignalListener(client)
        await listener.init()
        self.assertEqual(len(registrations), 2)
        handler = registrations[0][1]
        e = SimpleNamespace(id=10, raw_text='Meta Signal: Higher')
        await handler(e)
        await handler(e)
        self.assertEqual(listener._pending.qsize(), 1)
        self.assertEqual(await listener.wait_for_signal(.1), 'UP')
        listener.drain_pending()
        await handler(e)  # edit same card in a new request must remain usable
        self.assertEqual(await listener.wait_for_signal(.1), 'UP')
        e.raw_text = 'Meta Signal: Lower'
        await handler(e)
        self.assertEqual(await listener.wait_for_signal(.1), 'DOWN')
        e.raw_text = 'Meta is preparing the signal...'
        await handler(e)
        self.assertTrue(listener._pending.empty())


class IdentityTests(unittest.TestCase):
    def doc(self):
        d = defaults()
        d['demo_account']['ssid'] = auth(111)
        d['real_account']['ssid'] = auth(222, 0)
        return d

    def test_selected_uid_changes_without_exposing_tokens(self):
        d = self.doc()
        self.assertEqual(account_identity(d)['uid'], '111')
        d['account_mode'] = 'REAL'
        self.assertEqual(account_identity(d)['uid'], '222')
        text = account_text(d)
        self.assertIn('REAL', text)
        self.assertIn('222', text)
        self.assertNotIn('offline-only', text)
        self.assertNotIn('session', text)

    def test_raw_ssid_uses_supplied_uid(self):
        d = self.doc()
        d['demo_account'].update(ssid='synthetic-raw', uid=333)
        self.assertEqual(account_identity(d)['uid'], '333')

    def test_identical_uid_for_two_modes_is_not_invented(self):
        d = self.doc()
        d['real_account']['ssid'] = auth(111, 0)
        d['account_mode'] = 'REAL'
        self.assertEqual(account_identity(d)['uid'], '111')
        self.assertIn('REAL', account_text(d))

    def test_old_connection_uid_never_leaks_after_switch(self):
        d = self.doc()
        po = SimpleNamespace(mode='PRACTICE', connected=True,
                             credentials=deepcopy(d['demo_account']), server_uid='999')
        self.assertEqual(account_identity(d, po)['uid'], '999')
        d['account_mode'] = 'REAL'
        self.assertEqual(account_identity(d, po)['uid'], '222')
        d['account_mode'] = 'PRACTICE'
        d['demo_account']['ssid'] = auth(444)
        self.assertEqual(account_identity(d, po)['uid'], '444')

    def test_explicit_server_uid_and_binary_auth(self):
        d = self.doc()
        po = CloudPocket(d['demo_account'], 'PRACTICE')
        po.process(['successauth', {'uid': 111}])
        self.assertEqual(po.server_uid, '111')
        po.connected = True
        self.assertEqual(account_identity(d, po)['source'], 'من استجابة المنصة')
        decoder = EventDecoder()
        self.assertEqual(decoder.feed('451-["profile",{"_placeholder":true,"num":0}]'), [])
        for e in decoder.feed(b'{"uid":111,"balance":123,"isDemo":1}'):
            po.process(e)
        self.assertEqual(po.balance, 123)
        self.assertEqual(po.server_uid, '111')
        po.process(['updateClosedDeals', {'id': 456, 'uid': 888}])
        self.assertEqual(po.server_uid, '111')
        po.process(['updateBalance', {'uid': 222, 'balance': 999, 'isDemo': 0}])
        self.assertEqual(po.server_uid, '111')

    def test_missing_uid_is_not_a_placeholder_number(self):
        d = self.doc(); d['demo_account']['ssid'] = '...'
        self.assertIsNone(account_identity(d)['uid'])
        self.assertIn('غير متاح', account_text(d))


class LimitMathTests(unittest.TestCase):
    def limited(self, net):
        d = defaults()
        d['risk']['mode'] = 'LIMITED'
        d['account_stats']['PRACTICE']['net_pnl'] = net
        return d

    def test_inclusive_profit_and_loss_with_overshoot(self):
        for net, reason in [(49.99, None), (50, 'PROFIT_LIMIT'), (75, 'PROFIT_LIMIT'),
                            (-49.99, None), (-50, 'LOSS_LIMIT'), (-100, 'LOSS_LIMIT')]:
            with self.subTest(net=net):
                self.assertEqual(risk_state(self.limited(net))['reason'], reason)

    def test_traditional_never_halts_on_threshold(self):
        d = self.limited(-9999); d['risk']['mode'] = 'TRADITIONAL'
        self.assertIsNone(risk_state(d)['reason'])

    def test_turnover_is_not_net_profit(self):
        d = self.limited(5)
        d['account_stats']['PRACTICE'].update(total_staked=150, total_returned=155)
        self.assertIsNone(risk_state(d)['reason'])

    def test_stats_independent_of_selected_mode(self):
        d = self.limited(50)
        d['account_mode'] = 'REAL'
        self.assertIsNone(risk_state(d)['reason'])

    def test_validate_keys_and_reject_invalid_limits(self):
        for value in (0, -50, float('nan'), float('inf'), True, 'x', 1000001):
            with self.subTest(value=value):
                with self.assertRaises((ValueError, TypeError)):
                    patched(defaults(), {'risk.profit_limit': value})
        for key in ('risk.enabled', 'runtime.pending', 'risk.mode'):
            with self.assertRaises(ValueError):
                patched(defaults(), {key: 'bad'})
        self.assertEqual(patched(defaults(), {'risk.loss_limit': 12.5})['risk']['loss_limit'], 12.5)

    def test_micro_rounding_matches_saved_accounting(self):
        d = self.limited(.1 + .2)
        d['risk']['profit_limit'] = .3
        self.assertEqual(risk_state(d)['reason'], 'PROFIT_LIMIT')


class PersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_missing_risk_persisted_with_old_stats_untouched(self):
        old = defaults(); del old['risk']
        old['account_stats']['PRACTICE']['net_pnl'] = 25
        repo = await real_repo(old)
        self.assertEqual(repo.collection.doc['risk']['mode'], 'TRADITIONAL')
        self.assertEqual(repo.doc['account_stats']['PRACTICE']['net_pnl'], 25)
        await repo.update({'risk.mode': 'LIMITED', 'risk.profit_limit': 40})
        again = Repository('', '', collection=repo.collection)
        await again.initialize()
        self.assertEqual(again.doc['risk']['profit_limit'], 40)
        self.assertEqual(again.doc['risk']['mode'], 'LIMITED')
        self.assertEqual(again.doc['account_stats']['PRACTICE']['net_pnl'], 25)

    async def test_reset_zeros_net_not_limit_configuration(self):
        repo = await real_repo()
        await repo.update({'risk.mode': 'LIMITED', 'risk.loss_limit': 30})
        repo.doc['account_stats']['PRACTICE']['net_pnl'] = -30
        await repo.reset_stats()
        self.assertEqual(repo.doc['risk']['mode'], 'LIMITED')
        self.assertEqual(repo.doc['risk']['loss_limit'], 30)
        self.assertIsNone(risk_state(repo.doc)['reason'])

    async def test_failed_write_never_activates_settings_in_memory(self):
        repo = await real_repo()
        repo.collection.fail_write = True
        with self.assertRaises(OSError):
            await repo.update({'risk.mode': 'LIMITED'})
        self.assertEqual(repo.doc['risk']['mode'], 'TRADITIONAL')


class WorkerLimitsTests(unittest.IsolatedAsyncioTestCase):
    async def make_worker(self, outcomes, mode='LIMITED', net=0, stake=10, profit=50, loss=50,
                          stop_on_exhaustion=False):
        doc = defaults()
        doc['demo_account']['ssid'] = auth(111)
        doc['risk'].update(mode=mode, profit_limit=profit, loss_limit=loss)
        doc['account_stats']['PRACTICE']['net_pnl'] = net
        doc['trading'].update(trade_amount=stake, martingale_steps=4,
                              entry_delay_seconds=0, loop_delay_seconds=0)
        repo = await real_repo(doc)
        w = TradingWorker(repo)
        w._telegram = AsyncMock()
        w.notify = AsyncMock()
        w.listener = SimpleNamespace(drain_pending=lambda: None,
                                     press_new_signal=AsyncMock(return_value=True),
                                     wait_for_signal=AsyncMock(return_value='UP'))
        w.listener = synchronized_fixture(w.listener)
        po = SimpleNamespace(connected=True, mode='PRACTICE', balance=1000.0,
                             _balance_before=None, select_asset=AsyncMock(),
                             credentials=deepcopy(doc['demo_account']), close=AsyncMock())
        queue = list(outcomes)
        async def place(direction, amount):
            if not queue:
                raise AssertionError('unexpected extra trade past threshold')
            po._balance_before = po.balance
            po.balance -= amount
            return True
        async def result(timeout):
            outcome, delta = queue.pop(0)
            po.balance = po._balance_before + delta
            if stop_on_exhaustion and not queue:
                w.stop_event.set()
            return outcome
        po.place_trade = AsyncMock(side_effect=place)
        po.wait_for_result = AsyncMock(side_effect=result)
        w.po = po
        return w

    async def test_profit_stop_immediate_after_save_no_next_signal(self):
        w = await self.make_worker([('win', 50), ('loss', -10)])
        await w._run()
        self.assertEqual(w.po.place_trade.await_count, 1)
        self.assertEqual(w.listener.press_new_signal.await_count, 1)
        self.assertEqual(w.repo.doc['account_stats']['PRACTICE']['net_pnl'], 50)
        self.assertEqual(w.stage, 'limit_reached')
        self.assertFalse(w.running)
        w.notify.assert_awaited_once()
        self.assertIn('حد الربح', w.notify.call_args.args[0])

    async def test_loss_limit_stops_ladder_not_finish_whole_sequence(self):
        w = await self.make_worker([('loss', -10), ('loss', -20), ('loss', -40), ('win', 75)])
        await w._run()
        amounts = [c.args[1] for c in w.po.place_trade.call_args_list]
        self.assertEqual(amounts, [10, 20, 40])
        self.assertEqual(w.repo.doc['account_stats']['PRACTICE']['net_pnl'], -70)
        self.assertEqual(w.stage, 'limit_reached')
        self.assertIn('حد الخسارة', w.notify.call_args.args[0])

    async def test_traditional_preserves_user_example_turnover_150_net_5(self):
        w = await self.make_worker([('loss', -10), ('loss', -20), ('loss', -40), ('win', 75)],
                                   mode='TRADITIONAL', stop_on_exhaustion=True)
        await w._run()
        s = w.repo.doc['account_stats']['PRACTICE']
        self.assertEqual(s['total_staked'], 150)
        self.assertEqual(s['net_pnl'], 5)
        self.assertEqual(s['total_returned'], 155)
        self.assertEqual(s['sequence_losses'], 0)
        self.assertEqual(w.repo.doc['runtime']['martingale_step'], 0)
        w.notify.assert_not_awaited()

    async def test_draw_repeats_stake_and_win_resets_original_martingale(self):
        w = await self.make_worker([('loss', -10), ('draw', 0), ('win', 18), ('win', 9)],
                                   stop_on_exhaustion=True)
        await w._run()
        self.assertEqual([c.args[1] for c in w.po.place_trade.call_args_list], [10, 20, 20, 10])
        self.assertEqual(w.repo.doc['account_stats']['PRACTICE']['draws'], 1)

    async def test_existing_hit_blocks_start_and_no_network(self):
        w = await self.make_worker([], net=50)
        text = await w.start()
        self.assertIn('حد الربح', text)
        self.assertIsNone(w.task)
        w._telegram.assert_not_awaited()
        w.po.place_trade.assert_not_awaited()

    async def test_enabling_uses_existing_net_not_zero_baseline(self):
        w = await self.make_worker([], mode='TRADITIONAL', net=-60)
        text = await w.change({'risk.mode': 'LIMITED'})
        self.assertIn('حد الخسارة', text)
        self.assertTrue(w.stop_event.is_set())
        self.assertEqual(w.repo.doc['account_stats']['PRACTICE']['net_pnl'], -60)

    async def test_lowering_threshold_mid_signal_cancels_before_entry(self):
        w = await self.make_worker([('win', 5)], net=40)
        entered = asyncio.Event()
        async def wait_signal(timeout):
            entered.set()
            await asyncio.sleep(3600)
        w.listener.wait_for_signal = AsyncMock(side_effect=wait_signal)
        w.running = True
        w.task = asyncio.create_task(w._run())
        await asyncio.wait_for(entered.wait(), 1)
        await w.change({'risk.profit_limit': 30})
        with self.assertRaises(asyncio.CancelledError):
            await w.task
        self.assertEqual(w.stage, 'limit_reached')
        w.po.place_trade.assert_not_awaited()

    async def test_setting_change_does_not_cancel_already_open_trade(self):
        w = await self.make_worker([], net=40)
        gate = asyncio.Event()
        w.task = asyncio.create_task(gate.wait())
        w.in_flight = True
        try:
            await w.change({'risk.profit_limit': 30})
            self.assertFalse(w.task.cancelled())
            self.assertFalse(w.task.done())
            self.assertTrue(w.stop_event.is_set())
        finally:
            gate.set()
            await w.task

    async def test_unknown_trade_is_not_invented_loss_or_doubled(self):
        w = await self.make_worker([(None, -10), ('win', 9)], stop_on_exhaustion=True)
        await w._run()
        s = w.repo.doc['account_stats']['PRACTICE']
        self.assertEqual(s['unconfirmed'], 1)
        self.assertEqual(s['net_pnl'], 9)
        self.assertEqual([c.args[1] for c in w.po.place_trade.call_args_list], [10, 10])

    async def test_threshold_rechecked_after_signal_and_before_send(self):
        w = await self.make_worker([('win', 5)])
        async def signal(timeout):
            w.repo.doc['account_stats']['PRACTICE']['net_pnl'] = 50
            return 'UP'
        w.listener.wait_for_signal = AsyncMock(side_effect=signal)
        await w._run()
        w.po.place_trade.assert_not_awaited()
        self.assertEqual(w.stage, 'limit_reached')


class UiTests(unittest.IsolatedAsyncioTestCase):
    async def ui(self):
        from aiogram.fsm.context import FSMContext
        from aiogram.fsm.storage.memory import MemoryStorage
        from aiogram.fsm.storage.base import StorageKey
        d = defaults()
        d['demo_account']['ssid'] = auth(111)
        d['real_account']['ssid'] = auth(222, 0)
        repo = await real_repo(d)
        w = TradingWorker(repo)
        bot = FakeBot()
        self.bot = bot
        dp = build_dispatcher(repo, w, bot)
        state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        callback = next(h.callback for h in dp.callback_query.handlers if h.callback.__name__ == 'callback')
        return repo, w, dp, state, callback

    async def test_real_callbacks_and_custom_limit_handler_save_to_repository(self):
        from control_bot import Input
        repo, w, dp, state, callback = await self.ui()
        cb = SimpleNamespace(data='limit:profit_limit', answer=AsyncMock(),
                             message=SimpleNamespace(edit_text=AsyncMock()))
        await callback(cb, state)
        self.assertEqual(await state.get_state(), Input.limit.state)
        handler = next(h.callback for h in dp.message.handlers if h.callback.__name__ == 'limit_input')
        msg = SimpleNamespace(text='57.5', answer=AsyncMock())
        await handler(msg, state)
        self.assertEqual(repo.collection.doc['risk']['profit_limit'], 57.5)
        self.assertIsNone(await state.get_state())
        cb.data = 'risk:LIMITED'
        await callback(cb, state)
        self.assertEqual(repo.collection.doc['risk']['mode'], 'LIMITED')
        cb.data = 'risk:TRADITIONAL'
        await callback(cb, state)
        self.assertEqual(repo.collection.doc['risk']['mode'], 'TRADITIONAL')
        self.assertFalse(w.running)
        self.assertIsNone(w.task)

    async def test_invalid_limit_stays_in_input_without_database_change(self):
        repo, w, dp, state, callback = await self.ui()
        cb = SimpleNamespace(data='limit:loss_limit', answer=AsyncMock(),
                             message=SimpleNamespace(edit_text=AsyncMock()))
        await callback(cb, state)
        handler = next(h.callback for h in dp.message.handlers if h.callback.__name__ == 'limit_input')
        for raw in ('-50', '0', 'nan', 'inf', 'wrong'):
            msg = SimpleNamespace(text=raw, answer=AsyncMock())
            await handler(msg, state)
            self.assertIsNotNone(await state.get_state())
            self.assertEqual(repo.collection.doc['risk']['loss_limit'], 50)

    async def test_switch_callback_displays_new_selected_uid(self):
        repo, w, dp, state, callback = await self.ui()
        w.connect_account = AsyncMock()  # no external account connection in test
        cb = SimpleNamespace(data='mode:REAL', answer=AsyncMock(),
                             message=SimpleNamespace(edit_text=AsyncMock()))
        await callback(cb, state)
        self.assertEqual(repo.doc['account_mode'], 'REAL')
        text = self.bot._aboud_panels.active.caption
        self.assertIn('UID: 222', text)
        self.assertIn('REAL', text)
        self.assertNotIn('UID: 111', text)
        cb.data = 'mode:PRACTICE'
        await callback(cb, state)
        self.assertIn('UID: 111', self.bot._aboud_panels.active.caption)

    async def test_menus_report_and_admin_guard_remain(self):
        repo = await real_repo()
        repo.doc['demo_account']['ssid'] = auth(123)
        w = TradingWorker(repo)
        bot = FakeBot()
        self.bot = bot
        dp = build_dispatcher(repo, w, bot)
        self.assertIsNotNone(dp)
        for menu in (home(), settings_kb()):
            self.assertIn('limits', [b.callback_data for row in menu.inline_keyboard for b in row])
        buttons = [b.callback_data for row in limits_kb().inline_keyboard for b in row]
        for item in ('risk:LIMITED', 'risk:TRADITIONAL', 'limit:profit_limit', 'limit:loss_limit'):
            self.assertIn(item, buttons)
        text = report(repo, w)
        self.assertIn('UID: 123', text)
        self.assertIn('تقليدي', text)
        self.assertLess(len(text), 4096)
        self.assertNotIn('offline-only', text)
        handler = AsyncMock()
        outsider = SimpleNamespace(from_user=SimpleNamespace(id=-10), chat=SimpleNamespace(type='private', id=-10))
        await AdminOnly()(handler, outsider, {})
        handler.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
