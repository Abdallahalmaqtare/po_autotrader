"""Real screenshot-flow navigator + listener + worker with simulated I/O only."""
import asyncio
from copy import deepcopy
from datetime import datetime,timezone,timedelta
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from payout_guard import PayoutBook,PayoutBlocked,payout_defaults,allowed
from meta_navigation import MetaNavigator,NavigationError,asset_from_text,duration_from_text
from synchronized_signals import SynchronizedSignalListener
from cloud_settings import defaults,patched
from cloud_transport import CloudPocket,EventDecoder
from control_bot import build_dispatcher,payout_kb
from worker import TradingWorker
from database import Repository
from forex_assets import FOREX_OTC
from ui_helpers import FakeBot
from meta_fixture import FakeMetaClient
from test_v16_features import real_repo
import test_v16_features as old_tests
import config


def row(asset,rate,tradable=1,durations=(5,15,60)):
    return [5,asset,'Pair name','currency',2,rate,0,0,0,1,0,0,0,0,tradable,
            [{'time':v} for v in durations]]


def feed(book,asset,rate,**extra):
    book.feed(['updateAssets',[dict(symbol=asset,payout=rate,tradable=True,**extra)]])


class PayoutParsingTests(unittest.TestCase):
    def test_tuple_schema_and_dicts_read_payout_not_accuracy(self):
        book=PayoutBook('PRACTICE')
        book.feed(['updateAssets',[row('EURUSD_otc',92)]])
        self.assertEqual(book.rate('EURUSD_otc',5),92)
        book.feed(['updateAssets',[{'symbol':'GBPUSD_otc','profit':90,'tradable':True}]])
        self.assertEqual(book.rate('GBPUSD_otc',5),90)
        book.feed(['accuracy',{'asset':'EURUSD_otc','profit':99}])
        book.feed(['updateClosedDeals',{'asset':'EURUSD_otc','profit':99}])
        self.assertEqual(book.rate('EURUSD_otc',5),92)

    def test_equality_triggers_policy_and_high_rate_is_allowed(self):
        book=PayoutBook('PRACTICE'); p=payout_defaults(); p['mode']='STOP'
        for value,expected in [(92,True),(84.1,True),(84,False),(83.9,False),(0,False)]:
            feed(book,'EURUSD_otc',value)
            self.assertEqual(allowed(book,'EURUSD_otc',5,p),expected)

    def test_expired_closed_wrong_duration_and_missing_unknown(self):
        book=PayoutBook('PRACTICE')
        book.feed(['updateAssets',[row('EURUSD_otc',92,0),row('GBPUSD_otc',92,1,(60,))]])
        self.assertIsNone(book.rate('EURUSD_otc',5))
        # Tuple[15] is an expiry-profile hint, not verified turbo-duration exclusion.
        self.assertEqual(book.rate('GBPUSD_otc',5),92)
        self.assertEqual(book.rate('GBPUSD_otc',60),92)
        feed(book,'GBPUSD_otc',92,supported_durations=[60])
        self.assertIsNone(book.rate('GBPUSD_otc',5))
        feed(book,'AUDUSD_otc',92)
        book.quotes['AUDUSD_otc'].at-=61
        self.assertIsNone(book.rate('AUDUSD_otc',5))
        self.assertIsNone(book.rate('MISSING_otc',5))

    def test_duration_specific_payout_does_not_use_other_duration(self):
        book=PayoutBook('PRACTICE')
        feed(book,'EURUSD_otc',{'5':92,'60':80})
        self.assertEqual(book.rate('EURUSD_otc',5),92)
        self.assertEqual(book.rate('EURUSD_otc',60),80)
        self.assertIsNone(book.rate('EURUSD_otc',15))

    def test_wrong_account_and_nonfinite_invalid_values_not_accepted(self):
        book=PayoutBook('REAL')
        feed(book,'EURUSD_otc',92,isDemo=True)
        self.assertIsNone(book.rate('EURUSD_otc',5))
        feed(book,'EURUSD_otc',92,isDemo=0)
        self.assertEqual(book.rate('EURUSD_otc',5),92)
        for value in (None,float('nan'),float('inf'),-1,101,True,'wrong'):
            feed(book,'EURUSD_otc',value)
            self.assertIsNone(book.rate('EURUSD_otc',5))

    def test_candidates_are_whitelisted_and_ranked_highest_first(self):
        book=PayoutBook('PRACTICE'); p=payout_defaults();p['mode']='SWITCH'
        for asset,value in [('EURUSD_otc',70),('GBPUSD_otc',92),('AUDUSD_otc',90),
                            ('CADJPY_otc',85),('Apple_otc',99),('USDMXN_otc',99)]:
            feed(book,asset,value)
        self.assertEqual(book.candidates(5,p),[('GBPUSD_otc',92),('AUDUSD_otc',90),('CADJPY_otc',85)])
        p['switch_min']=92
        self.assertEqual(book.candidates(5,p),[('GBPUSD_otc',92)])

    def test_socketio_binary_and_raw_table_decode(self):
        decoder=EventDecoder(); records=[row('EURUSD_otc',92)]
        self.assertEqual(decoder.feed('451-["updateAssets",{"_placeholder":true,"num":0}]'),[])
        event=decoder.feed(json.dumps(records).encode())[0]
        book=PayoutBook('PRACTICE');book.feed(event)
        self.assertEqual(book.rate('EURUSD_otc',5),92)
        book.clear()
        book.feed(decoder.feed(json.dumps(records).encode())[0])
        self.assertEqual(book.rate('EURUSD_otc',5),92)
        book.clear();book.feed(decoder.feed(json.dumps(records))[0])
        self.assertEqual(book.rate('EURUSD_otc',5),92)

    def test_policy_validation_and_default_off(self):
        self.assertEqual(defaults()['payout']['mode'],'OFF')
        for key,value in [('mode','bad'),('threshold',float('nan')),('threshold',101),
                          ('threshold',True),('switch_min',-1),('max_age_seconds',0)]:
            with self.assertRaises((ValueError,TypeError)):
                patched(defaults(),{'payout.'+key:value})
        with self.assertRaises(ValueError):
            patched(defaults(),{'payout.mode':'SWITCH','payout.switch_min':84})


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client=FakeMetaClient()
        self.listener=SynchronizedSignalListener(self.client)
        await self.listener.init()

    async def test_full_menu_trade_otc_pair_duration_confirm_first_signal(self):
        direction=await self.listener.next_direction('EURUSD_otc',5)
        self.assertEqual(direction,'UP')
        self.assertEqual(self.client.clicks,['Trade','OTC','EUR/USD (OTC)','S5','Confirm'])
        self.assertNotIn('New signal',self.client.clicks)
        self.assertTrue(self.listener.verifies('EURUSD_otc',5))

    async def test_second_request_presses_new_signal_once(self):
        await self.listener.next_direction('EURUSD_otc',5)
        self.client.clicks.clear()
        self.assertEqual(await self.listener.next_direction('EURUSD_otc',5),'UP')
        self.assertEqual(self.client.clicks,['New signal'])

    async def test_back_navigation_switches_pair_and_duration_without_old_signal(self):
        await self.listener.next_direction('EURUSD_otc',5)
        self.client.clicks.clear()
        await self.listener.synchronize('GBPUSD_otc',15)
        self.assertEqual(await self.listener.next_direction('GBPUSD_otc',15),'UP')
        self.assertEqual(self.client.clicks,['Back','Back','Back','GBP/USD (OTC)','S15','Confirm'])
        self.assertTrue(self.listener.verifies('GBPUSD_otc',15))
        self.assertFalse(self.listener.verifies('EURUSD_otc',5))

    async def test_fifth_page_asset_and_return_to_first_page(self):
        await self.listener.next_direction('USDINR_otc',5)
        self.assertEqual(self.client.clicks.count('>'),4)
        self.client.clicks.clear()
        await self.listener.next_direction('EURUSD_otc',5)
        self.assertEqual(self.client.clicks.count('<'),4)
        self.assertEqual(self.client.asset,'EURUSD_otc')

    async def test_all_thirty_assets_discovered_through_actual_pages(self):
        available=await self.listener.available_assets()
        self.assertEqual(available,{sym for _,sym in FOREX_OTC})
        self.assertEqual(self.client.clicks.count('>'),4)

    async def test_missing_duration_never_confirms_or_trades(self):
        self.client.omit_duration=5
        with self.assertRaisesRegex(NavigationError,'DURATION'):
            await self.listener.next_direction('EURUSD_otc',5)
        self.assertNotIn('Confirm',self.client.clicks)

    async def test_unavailable_pair_never_clicks_some_other_asset(self):
        with self.assertRaisesRegex(NavigationError,'ASSET_UNAVAILABLE'):
            await self.listener.next_direction('USDMXN_otc',5)
        self.assertNotIn('Confirm',self.client.clicks)

    async def test_wrong_pair_duration_and_old_message_are_ignored(self):
        self.listener.expected=('EURUSD_otc',5)
        self.listener.arm()
        msg=self.client.current
        msg.raw_text='Trading signal\nGBP/USD (OTC)\n5 sec\nMeta Signal: Higher'
        await self.client.emit()
        self.assertTrue(self.listener._pending.empty())
        msg.raw_text='Trading signal\nEUR/USD (OTC)\n15 sec\nMeta Signal: Higher'
        await self.client.emit()
        self.assertTrue(self.listener._pending.empty())
        msg.raw_text='Trading signal\nEUR/USD (OTC)\n5 sec\nMeta Signal: Higher'
        msg.date=datetime.now(timezone.utc)-timedelta(seconds=30)
        await self.client.emit()
        self.assertTrue(self.listener._pending.empty())
        msg.date=datetime.now(timezone.utc)
        await self.client.emit()
        self.assertFalse(self.listener._pending.empty())

    async def test_stop_cancels_navigation_before_any_button(self):
        stop=asyncio.Event();stop.set()
        with self.assertRaises(asyncio.CancelledError):
            await self.listener.next_direction('EURUSD_otc',5,stop)
        self.assertEqual(self.client.clicks,[])

    async def test_expired_first_signal_is_not_reused_after_stopped_time(self):
        await self.listener.synchronize('EURUSD_otc',5)
        self.listener.armed_at-=30
        self.client.clicks.clear()
        self.assertEqual(await self.listener.next_direction('EURUSD_otc',5),'UP')
        self.assertEqual(self.client.clicks,['New signal'])

    async def test_first_card_is_deduplicated(self):
        await self.listener.synchronize('EURUSD_otc',5)
        await self.client.emit()
        self.assertEqual(self.listener._pending.qsize(),1)

    async def test_metadata_extraction_matches_screenshot_formats(self):
        self.assertEqual(asset_from_text('Asset: EUR/USD (OTC)'), 'EURUSD_otc')
        self.assertEqual(duration_from_text('Trade duration: 5 sec'),5)
        self.assertEqual(duration_from_text('EUR/USD (OTC)\n1 min'),60)
        self.assertIsNone(asset_from_text('EUR/USD (OTC) and GBP/USD (OTC)'))


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def worker(self,outcomes,mode='STOP',rate=92):
        helper=old_tests.WorkerLimitsTests()
        w=await helper.make_worker(outcomes,mode='TRADITIONAL',stake=1,stop_on_exhaustion=True)
        await w.repo.update({'payout.mode':mode,'trading.asset':'EURUSD_otc'})
        self.client=FakeMetaClient()
        listener=SynchronizedSignalListener(self.client);await listener.init()
        w.listener=listener
        w.po.payout_book=PayoutBook('PRACTICE');feed(w.po.payout_book,'EURUSD_otc',rate)
        self.trade_pairs=[]
        previous=w.po.place_trade.side_effect
        async def checked(direction,amount):
            self.assertTrue(w.listener.verifies(config.ASSET,config.DURATION_SECONDS))
            self.assertEqual(asset_from_text(w.listener.last_text),config.ASSET)
            self.trade_pairs.append(config.ASSET)
            return await previous(direction,amount)
        w.po.place_trade.side_effect=checked
        return w

    async def test_high_payout_executes_first_confirm_then_new_signal(self):
        w=await self.worker([('win',.9),('win',.9)])
        await w._run()
        self.assertEqual(w.po.place_trade.await_count,2)
        self.assertEqual(self.client.clicks.count('Confirm'),1)
        self.assertEqual(self.client.clicks.count('New signal'),1)

    async def test_threshold_equality_stops_without_request_or_order(self):
        w=await self.worker([('win',.9)],rate=84)
        await w._run()
        w.po.place_trade.assert_not_awaited()
        self.assertEqual(w.stage,'payout_stopped')
        self.assertIn('84%',w.payout_note)
        self.assertEqual(self.client.clicks,[])
        w.notify.assert_awaited_once()

    async def test_switch_only_to_common_highest_live_pair_and_consume_first_signal(self):
        w=await self.worker([('win',.9)],mode='SWITCH',rate=70)
        feed(w.po.payout_book,'GBPUSD_otc',92)
        feed(w.po.payout_book,'AUDUSD_otc',95)
        self.client.pairs=[pair for pair in FOREX_OTC if pair[1]!='AUDUSD_otc']
        await w._run()
        self.assertEqual(w.repo.doc['trading']['asset'],'GBPUSD_otc')
        self.assertEqual(self.client.asset,'GBPUSD_otc')
        self.assertEqual(self.trade_pairs,['GBPUSD_otc'])
        self.assertEqual(self.client.clicks.count('Confirm'),1)
        self.assertEqual(self.client.clicks.count('New signal'),0)

    async def test_drop_after_confirm_blocks_old_signal_before_order(self):
        w=await self.worker([('win',.9)],rate=92)
        original=self.client.emit
        async def drop(*args,**kwargs):
            feed(w.po.payout_book,'EURUSD_otc',70)
            await original(*args,**kwargs)
        self.client.emit=drop
        await w._run()
        w.po.place_trade.assert_not_awaited()
        self.assertEqual(w.stage,'payout_stopped')
        self.assertEqual(w.repo.doc['account_stats']['PRACTICE']['total_trades'],0)

    async def test_expired_quote_refreshes_between_orders_then_requests_new_signal(self):
        w=await self.worker([('win',.9)])
        w.po.payout_book.quotes['EURUSD_otc'].at-=61
        async def refresh():
            feed(w.po.payout_book,'EURUSD_otc',92)
        w.po.refresh_payouts=AsyncMock(side_effect=refresh)
        await w._run()
        w.po.refresh_payouts.assert_awaited_once()
        self.assertEqual(w.po.place_trade.await_count,1)
        self.assertEqual(self.client.clicks.count('Confirm'),1)

    async def test_policy_off_does_not_require_payout_but_still_matches_pair(self):
        w=await self.worker([('win',.9)],mode='OFF')
        w.po.payout_book.clear()
        await w._run()
        self.assertEqual(self.trade_pairs,['EURUSD_otc'])

    async def test_no_alternative_waits_without_sending_any_order(self):
        w=await self.worker([('win',.9)],mode='SWITCH',rate=70)
        async def stop_wait(delay): w.stop_event.set()
        w._pause=stop_wait
        await w._run()
        w.po.place_trade.assert_not_awaited()
        self.assertIn('بديل',w.payout_note)

    async def test_manual_pair_and_duration_change_synchronizes_while_off_without_trade(self):
        w=await self.worker([('win',.9)])
        text=await w.change({'trading.asset':'GBPUSD_otc','trading.duration_seconds':15})
        self.assertIn('تمت مزامنة',text)
        self.assertEqual(self.client.asset,'GBPUSD_otc')
        self.assertEqual(self.client.duration,15)
        w.po.place_trade.assert_not_awaited()
        self.assertFalse(w.running)
        self.assertIsNone(w.task)

    async def test_reselecting_same_controller_pair_still_syncs_meta_menu(self):
        w=await self.worker([('win',.9)])
        self.assertEqual(w.repo.doc['trading']['asset'],'EURUSD_otc')
        text=await w.change({'trading.asset':'EURUSD_otc'})
        self.assertIn('تمت مزامنة',text)
        self.assertEqual(self.client.clicks,['Trade','OTC','EUR/USD (OTC)','S5','Confirm'])
        w.po.place_trade.assert_not_awaited()

    async def test_manual_change_interrupts_wait_and_only_new_pair_executes(self):
        w=await self.worker([('win',.9)],mode='OFF')
        entered=asyncio.Event()
        original=self.client.emit
        async def delayed(*args,**kwargs):
            if self.client.asset=='EURUSD_otc' and not entered.is_set():
                entered.set()
                await asyncio.Event().wait()
            await original(*args,**kwargs)
        self.client.emit=delayed
        w.running=True
        w.task=asyncio.create_task(w._run())
        await asyncio.wait_for(entered.wait(),1)
        await w.change({'trading.asset':'GBPUSD_otc'})
        await asyncio.wait_for(w.task,2)
        self.assertEqual(self.trade_pairs,['GBPUSD_otc'])
        self.assertEqual(self.client.asset,'GBPUSD_otc')
        self.assertEqual(w.po.place_trade.await_count,1)

    async def test_no_mid_trade_navigation_when_user_changes_pair(self):
        w=await self.worker([('win',.9)])
        w.running=True;w.in_flight=True
        await w.change({'trading.asset':'GBPUSD_otc'})
        self.assertEqual(self.client.clicks,[])
        self.assertTrue(w.selection_changed.is_set())

    async def test_presend_gate_refusal_is_not_counted_as_unknown_trade(self):
        w=await self.worker([('win',.9)])
        async def block(*args):
            feed(w.po.payout_book,'EURUSD_otc',70)
            raise PayoutBlocked()
        w.po.place_trade.side_effect=block
        await w._run()
        stats=w.repo.doc['account_stats']['PRACTICE']
        self.assertEqual(stats['total_trades'],0)
        self.assertEqual(stats['unconfirmed'],0)
        self.assertIsNone(w.pending)
        self.assertEqual(w.stage,'payout_stopped')

    async def test_live_cloud_presend_hook_runs_inside_original_send_path(self):
        po=CloudPocket({'ssid':'unused','ws_url':config.DEMO_URL},'PRACTICE')
        po.connected=True
        po.ws=SimpleNamespace(send=AsyncMock())
        po.entry_check=lambda:False
        with self.assertRaises(PayoutBlocked):
            await po.place_trade('UP',1)
        po.ws.send.assert_not_awaited()

    async def test_source_failure_does_not_replace_pair_or_open_order(self):
        w=await self.worker([('win',.9)],mode='SWITCH',rate=70)
        feed(w.po.payout_book,'GBPUSD_otc',92)
        w.listener.available_assets=AsyncMock(side_effect=NavigationError('META_PAGE_LIMIT'))
        async def stop_wait(delay):w.stop_event.set()
        w._pause=stop_wait
        await w._run()
        self.assertEqual(w.repo.doc['trading']['asset'],'EURUSD_otc')
        w.po.place_trade.assert_not_awaited()


class PolicyUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_upgrade_persists_disabled_policy_and_ui_updates_all_fields(self):
        old=defaults();del old['payout']
        repo=await real_repo(old)
        self.assertEqual(repo.collection.doc['payout']['mode'],'OFF')
        w=TradingWorker(repo);bot=FakeBot();dp=build_dispatcher(repo,w,bot)
        state=FSMContext(storage=MemoryStorage(),key=StorageKey(bot_id=1,chat_id=1,user_id=1))
        callback=next(h.callback for h in dp.callback_query.handlers if h.callback.__name__=='callback')
        async def action(data):
            await callback(SimpleNamespace(data=data,answer=AsyncMock(),message=SimpleNamespace(message_id=5,photo=[])),state)
        for data in ('payout','payout_threshold','payout_min','payout:value:threshold:84',
                     'payout:value:switch_min:92','payout:mode:SWITCH'):
            await action(data)
        self.assertEqual(repo.doc['payout']['switch_min'],92)
        self.assertEqual(repo.doc['payout']['mode'],'SWITCH')
        self.assertEqual(bot._aboud_panels.active.card,'limits')
        await action('payout:edit:threshold')
        handler=next(h.callback for h in dp.message.handlers if h.callback.__name__=='payout_input')
        await handler(SimpleNamespace(text='83%'),state)
        self.assertEqual(repo.collection.doc['payout']['threshold'],83)
        self.assertFalse(w.running)
        reloaded=Repository('','',collection=repo.collection);await reloaded.initialize()
        self.assertEqual(reloaded.doc['payout']['switch_min'],92)


if __name__=='__main__':
    unittest.main()
