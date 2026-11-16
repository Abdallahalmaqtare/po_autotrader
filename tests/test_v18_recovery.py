"""Recovery financial state, persisted worker flows, compact owner UI; no network."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from recovery import (recovery_defaults, state_defaults, after_result, sizing,
                      selected_state, recovery_text)
from cloud_settings import defaults, patched
from worker import TradingWorker
from database import Repository
from control_bot import (build_dispatcher, report, home, settings_kb, recovery_kb,
                         stats_kb, tools_kb, timing_kb)
from branded_ui import utf16_size, BRAND, card_for
from test_v16_features import real_repo
import test_v16_features as previous_tests
from ui_helpers import FakeBot

LOSSES = [('loss',-1),('loss',-2),('loss',-4),('loss',-8)]


class RecoveryPureTests(unittest.TestCase):
    def setUp(self):
        self.cfg = recovery_defaults(); self.cfg['mode']='ENABLED'

    def order(self, state, step=0, base=1, effective=1):
        return dict(sizing_signature=['PRACTICE','EURUSD_otc',effective,4,state['epoch']],
                    recovery_context=dict(epoch=state['epoch'],enabled=True,base_amount=base))

    def test_default_is_traditional_and_base_unchanged(self):
        d=defaults()
        self.assertEqual(d['recovery']['mode'],'TRADITIONAL')
        self.assertEqual(sizing(d)[0],d['trading']['trade_amount'])

    def test_last_loss_creates_phase_only_after_all_four_losses(self):
        state=state_defaults()
        for index,delta in enumerate((-1,-2,-4,-8)):
            state=after_result(self.cfg,state,self.order(state), 'loss',100,100+delta,int(index==3))
            self.assertEqual(state['active'],index==3)
        self.assertEqual(state['target_loss'],15)
        self.assertEqual(state['original_base'],1)
        self.assertEqual(state['phase_pnl'],0)
        self.assertEqual(state['wins'],0)

    def test_draw_is_not_added_to_loss_debt(self):
        state=state_defaults()
        for outcome,delta,full in [('loss',-1,0),('draw',0,0),('loss',-2,0),
                                    ('draw',0,0),('loss',-4,0),('loss',-8,1)]:
            state=after_result(self.cfg,state,self.order(state),outcome,100,100+delta,full)
        self.assertEqual(state['target_loss'],15)

    def test_unknown_initial_sequence_never_guesses_loss_amount(self):
        state=state_defaults()
        for result,delta,full in [(None,0,0),('loss',-1,0),('loss',-2,0),('loss',-4,0),('loss',-8,1)]:
            state=after_result(self.cfg,state,self.order(state),result,100,100+delta,full)
        self.assertFalse(state['active'])

    def test_complete_phase_includes_losses_not_gross_winnings(self):
        state=state_defaults(); state.update(active=True,original_base=1,target_loss=15)
        state=after_result(self.cfg,state,self.order(state,effective=2),'loss',100,90,0)
        state=after_result(self.cfg,state,self.order(state,effective=2),'win',100,115,0)
        self.assertTrue(state['active'])
        self.assertEqual(state['phase_pnl'],5)
        state=after_result(self.cfg,state,self.order(state,effective=2),'win',100,110,0)
        self.assertFalse(state['active'])
        self.assertEqual(state['last_event'],'RECOVERED')

    def test_unknown_phase_result_blocks_false_recovered_claim(self):
        state=state_defaults(); state.update(active=True,original_base=1,target_loss=15)
        state=after_result(self.cfg,state,self.order(state,effective=2),None,100,98,0)
        state=after_result(self.cfg,state,self.order(state,effective=2),'win',100,120,0)
        self.assertTrue(state['active'])
        self.assertEqual(state['uncertain'],1)

    def test_stale_epoch_cannot_restore_canceled_phase(self):
        state=state_defaults(epoch=3)
        old=self.order(state_defaults(epoch=2))
        state=after_result(self.cfg,state,old,'loss',100,85,1)
        self.assertFalse(state['active'])
        self.assertEqual(state['epoch'],3)

    def test_sizing_adds_increment_once_not_multiplied_by_losses(self):
        d=defaults(); d['recovery'].update(mode='ENABLED',increment=3)
        d['runtime']['recovery']['PRACTICE'].update(active=True,original_base=1,target_loss=200)
        self.assertEqual(sizing(d)[0],4)
        d['runtime']['recovery']['PRACTICE']['phase_pnl']=-1000
        self.assertEqual(sizing(d)[0],4)

    def test_invalid_recovery_options_rejected(self):
        for key,value in [('increment',0),('increment',-1),('increment',True),
                          ('increment',float('nan')),('increment',float('inf')),
                          ('win_target',0),('win_target',1.5),('win_target',True),
                          ('win_target',1001),('mode','bad'),('exit_mode','bad')]:
            with self.subTest(key=key,value=value):
                with self.assertRaises((ValueError,TypeError)):
                    patched(defaults(),{'recovery.'+key:value})
        with self.assertRaises(ValueError):
            patched(defaults(),{'trading.trade_amount':100000,'recovery.mode':'ENABLED'})


class RecoveryWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def worker(self,outcomes,exit_mode='RECOVER_LOSS',target=3,increment=1,stake=1,
                     risk='TRADITIONAL',loss_limit=1000):
        builder=previous_tests.WorkerLimitsTests()
        w=await builder.make_worker(outcomes,mode=risk,stake=stake,loss=loss_limit,
                                    profit=1000,stop_on_exhaustion=True)
        await w.repo.update({'recovery.mode':'ENABLED','recovery.increment':increment,
                             'recovery.exit_mode':exit_mode,'recovery.win_target':target})
        w.states=[]
        original=w.repo.resolve
        async def observe(*args,**kwargs):
            await original(*args,**kwargs)
            w.states.append(deepcopy(w.repo.doc))
        w.repo.resolve=observe
        return w

    def amounts(self,w):
        return [call.args[1] for call in w.po.place_trade.call_args_list]

    async def test_user_example_four_losses_boost_two_until_net_fifteen(self):
        w=await self.worker(LOSSES+[('win',1.8)]*9+[('win',.9)])
        await w._run()
        self.assertEqual(self.amounts(w),[1,2,4,8]+[2]*9+[1])
        self.assertEqual(w.states[3]['runtime']['recovery']['PRACTICE']['target_loss'],15)
        self.assertTrue(w.states[11]['runtime']['recovery']['PRACTICE']['active'])
        self.assertFalse(w.states[12]['runtime']['recovery']['PRACTICE']['active'])
        self.assertAlmostEqual(w.repo.doc['account_stats']['PRACTICE']['net_pnl'],2.1)
        self.assertEqual(w.repo.doc['account_stats']['PRACTICE']['sequence_losses'],1)

    async def test_boosted_ladder_remains_two_four_eight_sixteen_no_third_base(self):
        second=[('loss',-2),('loss',-4),('loss',-8),('loss',-16)]
        w=await self.worker(LOSSES+second+[('win',1.8)])
        await w._run()
        self.assertEqual(self.amounts(w),[1,2,4,8,2,4,8,16,2])
        state=selected_state(w.repo.doc)
        self.assertTrue(state['active'])
        self.assertEqual(state['target_loss'],15)
        self.assertAlmostEqual(state['phase_pnl'],-28.2)

    async def test_win_count_is_cumulative_not_consecutive_and_draw_does_not_count(self):
        w=await self.worker(LOSSES+[('win',1.8),('loss',-2),('draw',0),('win',3.6),
                                      ('win',1.8),('win',.9)],exit_mode='WINS',target=3)
        await w._run()
        self.assertEqual(self.amounts(w),[1,2,4,8,2,2,4,4,2,1])
        self.assertFalse(selected_state(w.repo.doc)['active'])
        self.assertEqual(w.states[8]['runtime']['recovery']['PRACTICE']['last_event'],'WIN_TARGET')

    async def test_increment_is_additive_even_for_larger_original_base(self):
        w=await self.worker([('loss',-3),('loss',-6),('loss',-12),('loss',-24),('win',4.5)],
                            increment=2,stake=3)
        await w._run()
        self.assertEqual(self.amounts(w),[3,6,12,24,5])

    async def test_draw_repeats_boosted_amount_and_same_step(self):
        w=await self.worker(LOSSES+[('loss',-2),('draw',0),('win',3.6)])
        await w._run()
        self.assertEqual(self.amounts(w),[1,2,4,8,2,4,4])
        self.assertEqual(selected_state(w.repo.doc)['wins'],1)

    async def test_risk_stop_has_priority_over_boost_activation(self):
        w=await self.worker(LOSSES+[('win',1.8)],risk='LIMITED',loss_limit=15)
        await w._run()
        self.assertEqual(self.amounts(w),[1,2,4,8])
        self.assertEqual(w.stage,'limit_reached')
        self.assertTrue(selected_state(w.repo.doc)['active'])
        w.notify.assert_awaited_once()

    async def test_traditional_does_not_boost_after_exhausted_ladder(self):
        w=await self.worker(LOSSES+[('win',.9)])
        await w.repo.update({'recovery.mode':'TRADITIONAL'})
        await w._run()
        self.assertEqual(self.amounts(w),[1,2,4,8,1])
        self.assertFalse(selected_state(w.repo.doc)['active'])

    async def test_unknown_during_boost_never_counts_win_or_doubles(self):
        w=await self.worker(LOSSES+[(None,-2),('win',1.8)],exit_mode='WINS',target=2)
        await w._run()
        self.assertEqual(self.amounts(w),[1,2,4,8,2,2])
        self.assertEqual(selected_state(w.repo.doc)['wins'],1)
        self.assertEqual(selected_state(w.repo.doc)['uncertain'],1)

    async def test_persistence_resume_restores_boost_amount_and_progress(self):
        w=await self.worker(LOSSES+[('win',1.8)])
        await w._run()
        repo=Repository('','',collection=w.repo.collection)
        await repo.initialize()
        self.assertTrue(selected_state(repo.doc)['active'])
        self.assertAlmostEqual(selected_state(repo.doc)['phase_pnl'],1.8)
        self.assertEqual(sizing(repo.doc)[0],2)

    async def test_actual_worker_restart_restores_mid_boost_step(self):
        first=await self.worker(LOSSES+[('loss',-2)],exit_mode='WINS',target=3)
        await first._run()
        self.assertEqual(first.repo.doc['runtime']['martingale_step'],1)
        second=await self.worker([('win',3.6),('win',1.8),('win',1.8),('win',.9)],
                                 exit_mode='WINS',target=3)
        second.repo=first.repo
        await second._run()
        self.assertEqual(self.amounts(second),[4,2,2,1])
        self.assertFalse(selected_state(second.repo.doc)['active'])

    async def test_restart_before_last_original_loss_preserves_fifteen_debt(self):
        first=await self.worker(LOSSES[:3])
        await first._run()
        self.assertFalse(selected_state(first.repo.doc)['active'])
        second=await self.worker([('loss',-8),('win',1.8)])
        second.repo=first.repo
        await second._run()
        self.assertEqual(self.amounts(second),[8,2])
        self.assertEqual(selected_state(second.repo.doc)['target_loss'],15)

    async def test_atomic_result_cannot_apply_twice(self):
        w=await self.worker(LOSSES)
        await w._run()
        before=deepcopy(w.repo.doc)
        order=deepcopy(w.repo.collection.doc.get('runtime',{}))
        fake={'id':'already-closed','mode':'PRACTICE','amount':8,
              'sizing_signature':['PRACTICE','EURUSD_otc',1,4],
              'recovery_context':{'epoch':0,'enabled':True,'base_amount':1}}
        with self.assertRaises(RuntimeError):
            await w.repo.resolve(fake,'loss',100,92,0,seq_lost=1)
        self.assertEqual(w.repo.doc,before)

    async def test_failed_result_write_does_not_activate_phase_in_memory(self):
        repo=await real_repo()
        await repo.update({'recovery.mode':'ENABLED'})
        state=selected_state(repo.doc)
        state.update(sequence_pnl=-7,sequence_signature=['PRACTICE','EURUSD_otc',1,4,state['epoch']])
        repo.doc['runtime']['recovery']['PRACTICE']=state
        order={'id':'test-last-loss','mode':'PRACTICE','amount':8,
               'sizing_signature':deepcopy(state['sequence_signature']),
               'recovery_context':{'epoch':state['epoch'],'enabled':True,'base_amount':1}}
        await repo.reserve(order)
        previous=deepcopy(repo.doc)
        repo.collection.fail_write=True
        with self.assertRaises(OSError):
            await repo.resolve(order,'loss',100,92,0,seq_lost=1)
        self.assertEqual(repo.doc,previous)

    async def test_new_settings_cancel_phase_without_changing_financial_stats(self):
        w=await self.worker(LOSSES+[('win',1.8)])
        await w._run()
        stats=deepcopy(w.repo.doc['account_stats'])
        epoch=selected_state(w.repo.doc)['epoch']
        await w.repo.update({'recovery.increment':3})
        self.assertFalse(selected_state(w.repo.doc)['active'])
        self.assertGreater(selected_state(w.repo.doc)['epoch'],epoch)
        self.assertEqual(w.repo.doc['account_stats'],stats)
        self.assertEqual(sizing(w.repo.doc)[0],1)

    async def test_same_setting_button_does_not_cancel_active_phase(self):
        w=await self.worker(LOSSES)
        await w._run()
        old=deepcopy(selected_state(w.repo.doc))
        await w.repo.update({'recovery.increment':1.0})
        self.assertEqual(selected_state(w.repo.doc),old)

    async def test_reset_clears_phase_keeps_recovery_preferences(self):
        w=await self.worker(LOSSES)
        await w._run()
        await w.repo.reset_stats()
        self.assertFalse(selected_state(w.repo.doc)['active'])
        self.assertEqual(w.repo.doc['recovery']['mode'],'ENABLED')
        self.assertEqual(w.repo.doc['account_stats']['PRACTICE']['total_trades'],0)

    async def test_modes_have_independent_recovery_state(self):
        w=await self.worker(LOSSES)
        await w._run()
        self.assertTrue(selected_state(w.repo.doc,'PRACTICE')['active'])
        await w.repo.update({'account_mode':'REAL'})
        self.assertFalse(selected_state(w.repo.doc)['active'])
        self.assertEqual(sizing(w.repo.doc)[0],1)
        await w.repo.update({'account_mode':'PRACTICE'})
        self.assertEqual(sizing(w.repo.doc)[0],2)

    async def test_upgrading_old_document_adds_disabled_defaults_persistently(self):
        old=defaults(); del old['recovery']; del old['runtime']['recovery']
        old['account_stats']['PRACTICE']['net_pnl']=-15
        repo=await real_repo(old)
        self.assertEqual(repo.collection.doc['recovery']['mode'],'TRADITIONAL')
        self.assertIn('REAL',repo.collection.doc['runtime']['recovery'])
        self.assertEqual(repo.doc['account_stats']['PRACTICE']['net_pnl'],-15)


class CompactDashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo=await real_repo()
        self.worker=TradingWorker(self.repo)
        self.bot=FakeBot()
        self.dp=build_dispatcher(self.repo,self.worker,self.bot)
        self.state=FSMContext(storage=MemoryStorage(),key=StorageKey(bot_id=1,chat_id=1,user_id=1))
        self.callback=next(h.callback for h in self.dp.callback_query.handlers if h.callback.__name__=='callback')

    async def action(self,action):
        cb=SimpleNamespace(data=action,answer=AsyncMock(),message=SimpleNamespace(message_id=22,photo=[]))
        await self.callback(cb,self.state)
        cb.answer.assert_awaited_once()

    async def test_stats_fit_in_single_caption_no_instructions(self):
        text=report(self.repo,self.worker)
        self.assertLessEqual(utf16_size(BRAND+'\n\n'+text),1000)
        for unwanted in ('الإجراء','اضغط','استخدم','لا تُحسب','تقرير مستقل','منذ آخر','اختر'):
            self.assertNotIn(unwanted,text)
        for key in ('الصفقات','رابحة','خاسرة','تعادل','سلاسل مستنفدة','مبالغ الدخول','العوائد المستلمة','صافي'):
            self.assertIn(key,text)

    async def test_repeated_statistics_only_edit_same_caption_no_new_messages(self):
        for _ in range(10):
            await self.action('stats')
        self.bot.send_photo.assert_awaited_once()
        self.assertEqual(self.bot.edit_message_caption.await_count,9)
        self.bot.send_message.assert_not_awaited()
        self.assertEqual(self.bot._aboud_panels.active.details,[])

    async def test_stats_changed_values_stay_in_same_photo_caption(self):
        await self.action('stats')
        self.repo.doc['account_stats']['PRACTICE']['net_pnl']=5.5
        await self.action('stats')
        self.assertIn('$+5.50',self.bot._aboud_panels.active.caption)
        self.bot.send_photo.assert_awaited_once()
        self.bot.send_message.assert_not_awaited()

    async def test_active_recovery_report_remains_one_caption_with_short_note(self):
        await self.repo.update({'recovery.mode':'ENABLED'})
        self.repo.doc['runtime']['recovery']['PRACTICE'].update(
            active=True,original_base=1,target_loss=15,phase_pnl=-30,uncertain=1)
        self.repo.doc['account_stats']['PRACTICE']['unconfirmed']=1
        text=report(self.repo,self.worker)
        self.assertLessEqual(utf16_size(BRAND+'\n\n'+text),1000)
        self.assertLessEqual(len([line for line in text.splitlines() if '⚠️' in line]),1)
        await self.action('stats')
        self.bot.send_message.assert_not_awaited()

    async def test_control_layout_is_balanced_and_advanced_settings_are_grouped(self):
        self.assertEqual([len(row) for row in home().inline_keyboard],[2,2,2,2,1])
        home_values=[b.callback_data for row in home().inline_keyboard for b in row]
        self.assertIn('recovery',home_values)
        self.assertIn('tools',home_values)
        self.assertNotIn('reset_stats',home_values)
        settings=[b.callback_data for row in settings_kb().inline_keyboard for b in row]
        self.assertIn('timing',settings)
        self.assertNotIn('number:loss_recheck_seconds',settings)
        self.assertIn('number:loss_recheck_seconds',[b.callback_data for row in timing_kb().inline_keyboard for b in row])

    async def test_recovery_modes_increment_exit_and_wins_save_to_mongo(self):
        for action in ('recovery','recovery_increment','recovery_exit','recovery_wins'):
            await self.action(action)
            self.assertEqual(self.bot._aboud_panels.active.card,'setup')
        await self.action('recovery:mode:ENABLED')
        await self.action('recovery:value:increment:3')
        await self.action('recovery:exit:WINS')
        await self.action('recovery:value:win_target:4')
        self.assertEqual(self.repo.collection.doc['recovery'],
                         dict(mode='ENABLED',increment=3,exit_mode='WINS',win_target=4))
        self.assertFalse(self.worker.running)
        self.assertIsNone(self.worker.task)

    async def test_custom_recovery_number_rejects_fractional_wins(self):
        await self.action('recovery:edit:win_target')
        handler=next(h.callback for h in self.dp.message.handlers if h.callback.__name__=='recovery_input')
        await handler(SimpleNamespace(text='2.5'),self.state)
        self.assertEqual(self.repo.doc['recovery']['win_target'],3)
        self.assertIsNotNone(await self.state.get_state())
        await handler(SimpleNamespace(text='12'),self.state)
        self.assertEqual(self.repo.doc['recovery']['win_target'],12)
        self.assertIsNone(await self.state.get_state())

    async def test_custom_increment_and_limits_are_independent(self):
        await self.action('recovery:edit:increment')
        handler=next(h.callback for h in self.dp.message.handlers if h.callback.__name__=='recovery_input')
        await handler(SimpleNamespace(text='1.5'),self.state)
        self.assertEqual(self.repo.doc['recovery']['increment'],1.5)
        self.assertEqual(self.repo.doc['risk']['mode'],'TRADITIONAL')
        self.assertEqual(self.repo.doc['recovery']['mode'],'TRADITIONAL')


if __name__=='__main__':
    unittest.main()
