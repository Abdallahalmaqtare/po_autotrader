"""Confirm startup hotfix does not change sizing, timing config or pending safety."""
import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from cloud_settings import defaults
from martingale import MoneyManager
from worker import TradingWorker
from ui_helpers import synchronized_fixture


class TradingRegression(unittest.IsolatedAsyncioTestCase):
    def worker(self, result):
        doc=defaults()
        doc['trading'].update(entry_delay_seconds=0,loop_delay_seconds=0, martingale_steps=4)
        doc['runtime']['martingale_step']=0
        repo=SimpleNamespace(doc=doc,leader=True)
        async def reserve(order):
            doc['runtime']['pending']=order
        async def resolve(order, outcome, before, after, step, **kwargs):
            doc['runtime'].update(pending=None,martingale_step=step)
        repo.reserve=AsyncMock(side_effect=reserve)
        repo.resolve=AsyncMock(side_effect=resolve)
        w=TradingWorker(repo)
        w._telegram=AsyncMock()
        w.listener=SimpleNamespace(drain_pending=lambda:None,
            press_new_signal=AsyncMock(return_value=True),
            wait_for_signal=AsyncMock(return_value='UP'))
        w.listener=synchronized_fixture(w.listener)
        w.po=SimpleNamespace(connected=True,mode='PRACTICE',balance=100,
                             _balance_before=100,select_asset=AsyncMock(),
                             place_trade=AsyncMock(return_value=True))
        async def finish(timeout):
            w.stop_event.set()
            w.po.balance=100.48 if result=='win' else 99
            return result
        w.po.wait_for_result=AsyncMock(side_effect=finish)
        return w

    async def test_successful_cycle_still_sends_once_and_saves_win(self):
        w=self.worker('win')
        await w._run()
        w.po.place_trade.assert_awaited_once_with('UP',1.0)
        self.assertEqual(w.repo.resolve.call_args.args[1],'win')
        self.assertEqual(w.repo.doc['runtime']['martingale_step'],0)

    async def test_loss_preserves_original_ladder_step(self):
        w=self.worker('loss')
        await w._run()
        self.assertEqual(w.repo.doc['runtime']['martingale_step'],1)

    async def test_send_timeout_archived_without_doubling_or_replay_v14_behavior(self):
        # The uploaded v15 deliberately auto-archives uncertainty (v14 behavior).
        # The old test incorrectly still expected the pre-v14 manual-review pause.
        w=self.worker('win')
        w.po.place_trade=AsyncMock(side_effect=TimeoutError)
        async def archive(order, outcome, before, after, step, **kwargs):
            w.repo.doc['runtime'].update(pending=None, martingale_step=step)
            w.stop_event.set()  # bound this offline continuous-loop scenario
        w.repo.resolve=AsyncMock(side_effect=archive)
        await w._run()
        w.po.place_trade.assert_awaited_once()
        self.assertIsNone(w.pending)
        self.assertEqual(w.repo.doc['runtime']['martingale_step'],0)
        self.assertEqual(w.repo.resolve.call_args.args[1],None)
        self.assertEqual(w.stage,'stopped')

    async def test_stop_while_waiting_prevents_send(self):
        w=self.worker('win')
        async def signal(timeout):
            w.request_stop()
            return 'UP'
        w.listener.wait_for_signal=signal
        await w._run()
        w.po.place_trade.assert_not_awaited()
        self.assertIsNone(w.pending)

    def test_untouched_order_sizing_and_logging_core_hashes_match(self):
        root=Path(__file__).resolve().parents[1]
        for name,digest in json.loads((root/'original_core_hashes.json').read_text()).items():
            if name == 'telegram_signal.py':
                continue  # intentionally extended in v16; both formats covered separately
            self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(),digest)

    def test_four_levels_and_draw_are_unchanged(self):
        mm=MoneyManager(1,4)
        mm.register_result('loss'); self.assertEqual(mm.current_amount(),2)
        mm.register_result('loss'); self.assertEqual(mm.current_amount(),4)
        mm.register_result('draw'); self.assertEqual(mm.current_amount(),4)
        mm.register_result('win'); self.assertEqual(mm.current_amount(),1)


if __name__=='__main__':
    unittest.main()
