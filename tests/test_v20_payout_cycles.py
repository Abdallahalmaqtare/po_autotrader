"""Offline decoder/refresh diagnostics and virtual-clock run/rest integration."""
import asyncio
from copy import deepcopy
import gzip
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
import zlib
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from wire_decoder import EventDecoder
from payout_guard import PayoutBook,quote_detail,policy_text
from work_cycle import cycle_defaults,cycle_state,cycle_key,transition
from cloud_settings import defaults,patched
from worker import TradingWorker
from database import Repository
from control_bot import build_dispatcher,payout_kb,cycle_kb
from test_v16_features import real_repo
from test_v19_payout_sync import row,feed
import test_v16_features as previous
from ui_helpers import FakeBot


class DecoderAndPayoutTests(unittest.TestCase):
    def test_raw_engine_binary_prefix_variants_decode(self):
        rows=[row('BHDCNY_otc',65,1,(60,))]
        raw=json.dumps(rows).encode()
        for frame in (raw,b'\x04'+raw,b'4'+raw,gzip.compress(raw),zlib.compress(raw)):
            with self.subTest(prefix=frame[:3]):
                decoder=EventDecoder();book=PayoutBook('PRACTICE')
                for event in decoder.feed(frame):book.feed(event)
                self.assertEqual(book.rate('BHDCNY_otc',5),65)

    def test_namespace_ack_binary_and_string_attachment(self):
        for attachment in (json.dumps([row('BHDCNY_otc',65)]),
                           b'\x04'+json.dumps([row('BHDCNY_otc',65)]).encode()):
            decoder=EventDecoder()
            self.assertEqual(decoder.feed('451-/market,123["updateAssets",{"_placeholder":true,"num":0}]'),[])
            book=PayoutBook('PRACTICE')
            for event in decoder.feed(attachment):book.feed(event)
            self.assertEqual(book.rate('BHDCNY_otc',5),65)
            self.assertEqual(decoder.stats['attachments'],1)

    def test_nested_data_envelope_and_symbol_keyed_table(self):
        book=PayoutBook('PRACTICE')
        book.feed(['updateAssets',{'data':{'assets':{'BHDCNY_otc':{'payout':65,'tradable':True}}}}])
        self.assertEqual(book.rate('BHDCNY_otc',5),65)
        self.assertGreater(book.events_seen,0)
        self.assertEqual(book.rows_seen,1)

    def test_array_expiry_profile_does_not_hide_existing_five_second_payout(self):
        book=PayoutBook('PRACTICE')
        book.feed(['updateAssets',[row('BHDCNY_otc',65,1,(60,180,300))]])
        self.assertEqual(book.rate('BHDCNY_otc',5),65)
        self.assertEqual(book.quotes['BHDCNY_otc'].hint_durations,{60,180,300})
        feed(book,'GBPUSD_otc',{'60':92})
        self.assertIsNone(book.rate('GBPUSD_otc',5))

    def test_quote_reasons_distinguish_connection_table_asset_age_and_closed(self):
        book=PayoutBook('PRACTICE')
        self.assertEqual(quote_detail(None,'BHDCNY_otc',5,60)[0],'DISCONNECTED')
        self.assertEqual(quote_detail(book,'BHDCNY_otc',5,60)[0],'NO_TABLE')
        feed(book,'EURUSD_otc',92)
        self.assertEqual(quote_detail(book,'BHDCNY_otc',5,60)[0],'ASSET_MISSING')
        feed(book,'BHDCNY_otc',65)
        self.assertEqual(quote_detail(book,'BHDCNY_otc',5,60)[0],'KNOWN')
        book.quotes['BHDCNY_otc'].at-=61
        self.assertEqual(quote_detail(book,'BHDCNY_otc',5,60)[0],'STALE')
        book.feed(['updateAssets',[row('BHDCNY_otc',65,0)]])
        self.assertEqual(quote_detail(book,'BHDCNY_otc',5,60)[0],'CLOSED')

    def test_malformed_packet_rejected_without_exposing_payload(self):
        decoder=EventDecoder()
        decoder.feed('451-["updateAssets",{"_placeholder":true,"num":999}]')
        self.assertEqual(decoder.feed(b'\x04[]'),[])
        self.assertGreater(decoder.stats['rejected'],0)
        self.assertEqual(decoder.feed(b'not-json-PRIVATE_TOKEN'),[])
        self.assertNotIn('PRIVATE',str(decoder.stats))

    def test_off_default_and_invalid_cycle_settings(self):
        self.assertEqual(defaults()['schedule']['mode'],'OFF')
        for key,value in [('mode','bad'),('run_minutes',0),('run_minutes',float('nan')),
                          ('rest_minutes',True),('rest_minutes',-1),('finish','bad')]:
            with self.assertRaises((ValueError,TypeError)):
                patched(defaults(),{'schedule.'+key:value})


class PureClockTests(unittest.TestCase):
    def cfg(self,mode='REPEAT',finish='LADDER'):
        return dict(cycle_defaults(),mode=mode,finish=finish)

    def test_ten_minutes_run_ten_rest_then_new_run(self):
        cfg=self.cfg()
        action,state=transition(cfg,cycle_state(),0,now=1000)
        self.assertEqual(action,'ALLOW');self.assertEqual(state['run_until'],1600)
        action,state=transition(cfg,state,0,now=1600)
        self.assertEqual(action,'REST');self.assertEqual(state['rest_until'],2200)
        self.assertEqual(transition(cfg,state,0,now=2199)[0],'REST')
        action,state=transition(cfg,state,0,now=2200)
        self.assertEqual(action,'ALLOW');self.assertEqual(state['run_until'],2800)

    def test_second_third_fourth_step_must_complete_ladder_before_rest(self):
        cfg=self.cfg();_,state=transition(cfg,cycle_state(),0,now=0)
        for step in (1,2,3):
            action,state=transition(cfg,state,step,now=601)
            self.assertEqual(action,'DRAIN')
        action,state=transition(cfg,state,0,now=620)
        self.assertEqual(action,'REST');self.assertEqual(state['rest_until'],1220)

    def test_order_only_pauses_between_trades_preserving_next_step(self):
        cfg=self.cfg(finish='ORDER');_,state=transition(cfg,cycle_state(),0,now=0)
        action,state=transition(cfg,state,2,now=600)
        self.assertEqual(action,'REST')
        action,state=transition(cfg,state,2,now=1200)
        self.assertEqual(action,'ALLOW')

    def test_open_pending_never_enters_rest_until_settled(self):
        cfg=self.cfg();_,state=transition(cfg,cycle_state(),0,now=0)
        self.assertEqual(transition(cfg,state,0,pending=True,now=600)[0],'DRAIN')

    def test_one_shot_is_done_not_auto_resumed(self):
        cfg=self.cfg(mode='STOP');_,state=transition(cfg,cycle_state(),0,now=0)
        action,state=transition(cfg,state,0,now=600)
        self.assertEqual(action,'STOP');self.assertEqual(state['phase'],'DONE')
        self.assertEqual(transition(cfg,state,0,now=100000)[0],'STOP')

    def test_unknown_outcome_does_not_force_endless_ladder_extension(self):
        cfg=self.cfg();_,state=transition(cfg,cycle_state(),0,now=0)
        action,state=transition(cfg,state,2,force_rest=True,now=601)
        self.assertEqual(action,'REST')

    def test_off_never_limits_even_existing_rest(self):
        cfg=self.cfg(mode='OFF')
        action,state=transition(cfg,dict(cycle_state(),phase='REST',rest_until=99999),3,now=0)
        self.assertEqual(action,'ALLOW')
        self.assertEqual(state['phase'],'IDLE')


class RefreshAndPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_refresh_while_off_connects_and_does_not_trade(self):
        repo=await real_repo();w=TradingWorker(repo)
        repo.doc['trading']['asset']='BHDCNY_otc'
        book=PayoutBook('PRACTICE')
        po=SimpleNamespace(connected=True,mode='PRACTICE',payout_book=book,
                           select_asset=AsyncMock(),place_trade=AsyncMock())
        async def refresh():feed(book,'BHDCNY_otc',65)
        po.refresh_payouts=AsyncMock(side_effect=refresh)
        w.po=po
        text=await w.refresh_payout_ui()
        po.refresh_payouts.assert_awaited_once()
        po.select_asset.assert_awaited_once_with('BHDCNY_otc',5)
        po.place_trade.assert_not_awaited()
        self.assertIn('65%',text)
        self.assertFalse(w.running)
        self.assertIsNone(w.task)

    async def test_running_refresh_queues_without_reconnecting_open_trade(self):
        repo=await real_repo();w=TradingWorker(repo)
        w.running=True;w.in_flight=True
        w.po=SimpleNamespace(connected=True,mode='PRACTICE',payout_book=PayoutBook('PRACTICE'),
                             refresh_payouts=AsyncMock())
        await w.refresh_payout_ui()
        self.assertTrue(w.payout_refresh_requested)
        w.po.refresh_payouts.assert_not_awaited()

    async def test_schedule_migration_persists_and_config_reset_does_not_touch_finances(self):
        old=defaults();del old['schedule'];del old['runtime']['cycle']
        old['account_stats']['PRACTICE']['net_pnl']=12
        repo=await real_repo(old)
        self.assertEqual(repo.collection.doc['schedule']['mode'],'OFF')
        await repo.update({'schedule.mode':'REPEAT','schedule.run_minutes':10,'schedule.rest_minutes':5})
        cfg=deepcopy(repo.doc['schedule']);_,state=transition(cfg,cycle_state(),0,now=100)
        await repo.save_cycle(state,cfg)
        again=Repository('','',collection=repo.collection);await again.initialize()
        self.assertEqual(again.doc['runtime']['cycle']['run_until'],700)
        await repo.update({'schedule.rest_minutes':15})
        self.assertEqual(repo.doc['runtime']['cycle']['phase'],'IDLE')
        self.assertEqual(repo.doc['account_stats']['PRACTICE']['net_pnl'],12)

    async def test_stale_clock_save_cannot_override_new_ui_settings(self):
        repo=await real_repo();cfg=deepcopy(repo.doc['schedule'])
        await repo.update({'schedule.mode':'REPEAT'})
        self.assertFalse(await repo.save_cycle(dict(cycle_state(),phase='REST'),cfg))
        self.assertEqual(repo.doc['runtime']['cycle']['phase'],'IDLE')


class WorkerClockTests(unittest.IsolatedAsyncioTestCase):
    async def worker(self,outcomes,mode='REPEAT',finish='LADDER'):
        helper=previous.WorkerLimitsTests()
        w=await helper.make_worker(outcomes,mode='TRADITIONAL',stake=1,stop_on_exhaustion=True)
        await w.repo.update({'schedule.mode':mode,'schedule.run_minutes':10,
                             'schedule.rest_minutes':10,'schedule.finish':finish})
        self.time=0.0;w.clock=lambda:self.time
        self.rest_seen=[]
        original_pause=w._pause
        async def pause(delay):
            if w.stage=='scheduled_rest':
                self.rest_seen.append((self.time,w.repo.doc['runtime']['martingale_step'],w.po.place_trade.await_count))
                self.time+=max(delay,1)
                await asyncio.sleep(0)
            else:await original_pause(delay)
        w._pause=pause
        return w

    async def test_timer_finishes_second_third_fourth_ladder_then_rests_and_resumes(self):
        w=await self.worker([('loss',-1),('loss',-2),('loss',-4),('loss',-8),('win',.9)])
        original=w.po.wait_for_result.side_effect
        async def result(timeout):
            outcome=await original(timeout)
            if w.po.place_trade.await_count==1:self.time=601
            return outcome
        w.po.wait_for_result.side_effect=result
        await w._run()
        self.assertTrue(self.rest_seen)
        self.assertEqual(self.rest_seen[0][1:],(0,4))
        self.assertEqual([c.args[1] for c in w.po.place_trade.call_args_list],[1,2,4,8,1])
        self.assertGreaterEqual(self.time,1201)
        self.assertEqual(w.repo.doc['runtime']['cycle']['phase'],'RUN')

    async def test_order_only_rests_then_continues_same_second_step(self):
        w=await self.worker([('loss',-1),('win',1.8)],finish='ORDER')
        original=w.po.wait_for_result.side_effect
        async def result(timeout):
            outcome=await original(timeout)
            if w.po.place_trade.await_count==1:self.time=601
            return outcome
        w.po.wait_for_result.side_effect=result
        await w._run()
        self.assertEqual(self.rest_seen[0][1:],(1,1))
        self.assertEqual([c.args[1] for c in w.po.place_trade.call_args_list],[1,2])

    async def test_profit_two_hundred_stops_before_rest_and_never_auto_resumes(self):
        w=await self.worker([('win',200),('win',.9)])
        await w.repo.update({'risk.mode':'LIMITED','risk.profit_limit':200,'risk.loss_limit':999})
        original=w.po.wait_for_result.side_effect
        async def result(timeout):
            out=await original(timeout);self.time=601;return out
        w.po.wait_for_result.side_effect=result
        await w._run()
        self.assertEqual(w.po.place_trade.await_count,1)
        self.assertEqual(w.stage,'limit_reached')
        self.assertFalse(self.rest_seen)
        self.assertTrue(w.stop_event.is_set())

    async def test_money_loss_limit_does_not_finish_remaining_ladder(self):
        w=await self.worker([('loss',-1),('loss',-2),('win',3.6)])
        await w.repo.update({'risk.mode':'LIMITED','risk.profit_limit':200,'risk.loss_limit':3})
        await w._run()
        self.assertEqual(w.po.place_trade.await_count,2)
        self.assertEqual(w.stage,'limit_reached')

    async def test_one_shot_ends_after_time_and_does_not_rest(self):
        w=await self.worker([('win',.9),('win',.9)],mode='STOP')
        original=w.po.wait_for_result.side_effect
        async def result(timeout):
            out=await original(timeout);self.time=601;return out
        w.po.wait_for_result.side_effect=result
        await w._run()
        self.assertEqual(w.po.place_trade.await_count,1)
        self.assertEqual(w.stage,'time_limit_reached')
        self.assertEqual(w.repo.doc['runtime']['cycle']['phase'],'DONE')

    async def test_manual_stop_during_rest_cancels_automatic_resume(self):
        w=await self.worker([('win',.9),('win',.9)])
        original=w.po.wait_for_result.side_effect
        async def result(timeout):
            out=await original(timeout);self.time=601;return out
        w.po.wait_for_result.side_effect=result
        async def stop_pause(delay):
            if w.stage=='scheduled_rest':w.request_stop()
        w._pause=stop_pause
        await w._run()
        self.assertEqual(w.po.place_trade.await_count,1)
        self.assertTrue(w.stop_event.is_set())


class TimerUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_modes_durations_finish_rule_and_selected_payout_buttons(self):
        repo=await real_repo();w=TradingWorker(repo);bot=FakeBot()
        dp=build_dispatcher(repo,w,bot)
        state=FSMContext(storage=MemoryStorage(),key=StorageKey(bot_id=1,chat_id=1,user_id=1))
        callback=next(h.callback for h in dp.callback_query.handlers if h.callback.__name__=='callback')
        async def action(value):
            await callback(SimpleNamespace(data=value,answer=AsyncMock(),message=SimpleNamespace(message_id=4,photo=[])),state)
        for value in ('cycle','cycle_run','cycle_rest','cycle:value:run_minutes:10',
                      'cycle:value:rest_minutes:5','cycle:finish:ORDER','cycle:mode:REPEAT'):
            await action(value)
        self.assertEqual(repo.doc['schedule']['mode'],'REPEAT')
        self.assertEqual(repo.doc['schedule']['finish'],'ORDER')
        self.assertEqual(repo.doc['schedule']['rest_minutes'],5)
        self.assertFalse(w.running)
        await action('cycle:edit:run_minutes')
        handler=next(h.callback for h in dp.message.handlers if h.callback.__name__=='cycle_input')
        await handler(SimpleNamespace(text='12'),state)
        self.assertEqual(repo.collection.doc['schedule']['run_minutes'],12)
        marked=[b.text for row in payout_kb(repo.doc).inline_keyboard for b in row if b.callback_data=='payout:mode:OFF']
        self.assertIn('✅',marked[0])


class TelegramResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_disconnect_retries_safe_startup_with_backoff(self):
        from runtime_support import telegram_startup
        from aiogram.exceptions import TelegramNetworkError
        from aiogram.methods import GetMe
        operation=AsyncMock(side_effect=[TelegramNetworkError(method=GetMe(),message='disconnect'),
                                        TimeoutError(),True])
        waiter=AsyncMock()
        result=await telegram_startup(operation,asyncio.Event(),'test',waiter)
        self.assertTrue(result)
        self.assertEqual(operation.await_count,3)
        self.assertEqual([x.args[1] for x in waiter.await_args_list],[1,2])

    async def test_shutdown_aborts_reconnection_and_does_not_call_again(self):
        from runtime_support import telegram_startup
        shutdown=asyncio.Event()
        async def stop_wait(event,seconds):event.set()
        operation=AsyncMock(side_effect=TimeoutError())
        self.assertIsNone(await telegram_startup(operation,shutdown,'test',stop_wait))
        self.assertEqual(operation.await_count,1)

    async def test_bad_token_is_not_treated_as_temporary_disconnect(self):
        from runtime_support import telegram_startup
        from aiogram.exceptions import TelegramUnauthorizedError
        from aiogram.methods import GetMe
        operation=AsyncMock(side_effect=TelegramUnauthorizedError(method=GetMe(),message='unauthorized'))
        with self.assertRaises(TelegramUnauthorizedError):
            await telegram_startup(operation,asyncio.Event(),'test',AsyncMock())
        self.assertEqual(operation.await_count,1)

    async def test_last_moment_profit_limit_rejects_entry(self):
        from payout_guard import payout_defaults
        repo=await real_repo();w=TradingWorker(repo)
        await repo.update({'risk.mode':'LIMITED','risk.profit_limit':200})
        repo.doc['account_stats']['PRACTICE']['net_pnl']=200
        w.listener=SimpleNamespace(verifies=lambda *a,**k:True)
        self.assertFalse(w.entry_eligible(repo.doc['trading'],repo.doc['payout'],'PRACTICE'))


if __name__=='__main__':unittest.main()
