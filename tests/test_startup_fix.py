"""Real Telethon parser + offline lifecycle regressions; zero external API calls."""
import asyncio
import contextlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from telethon.sessions import StringSession
from telethon.crypto import AuthKey
import config
import worker as worker_module
from worker import TradingWorker
from cloud_settings import defaults
from cloud_transport import CloudPocket
from diagnostics import SafeIssue, diagnose_exception
from telegram_bootstrap import session_from_env, signal_username
from control_bot import home, build_dispatcher
from ui_helpers import FakeBot


def valid_session():
    session = StringSession()
    session.set_dc(2, '149.154.167.51', 443)
    session.auth_key = AuthKey(bytes(range(256)))  # synthetic, not a real session
    return session.save()


def repo_fixture():
    d = defaults()
    d['demo_account']['ssid'] = '42["auth",{"session":"offline","uid":123,"isDemo":1,"platform":2}]'
    return SimpleNamespace(doc=d, leader=True)


class ParsingTests(unittest.TestCase):
    def test_reproduce_original_valueerror_from_session_filename(self):
        with self.assertRaises(ValueError):
            StringSession('po_session')

    def test_valid_session_roundtrip(self):
        raw = valid_session()
        self.assertEqual(session_from_env(raw).save(), raw)

    def test_wrapping_quotes_assignment_and_line_breaks(self):
        raw = valid_session()
        variants = [f'  "{raw}"  ', f"'{raw}'", f'TELEGRAM_STRING_SESSION="{raw}"',
                    raw[:70]+'\n'+raw[70:]]
        for value in variants:
            with self.subTest(kind=len(value)):
                self.assertEqual(session_from_env(value).save(), raw)

    def test_reject_wrong_credentials_and_empty_no_secret_echo(self):
        for raw in ('', 'po_session.session', '123456:fake-bot-token', '42["auth",{}]', '111truncated'):
            with self.assertRaises(SafeIssue) as ctx:
                session_from_env(raw)
            self.assertEqual(str(ctx.exception), 'TG_SESSION_INVALID')

    def test_reject_keyless_serialized_session(self):
        s = StringSession()
        s.set_dc(2,'149.154.167.51',443)
        self.assertEqual(s.save(), '')
        with self.assertRaises(SafeIssue):
            session_from_env(s.save())

    def test_normalize_bot_usernames(self):
        for raw in ('@ExampleSignalBot', ' https://t.me/ExampleSignalBot ',
                    't.me/ExampleSignalBot?start=hello', 'SIGNAL_BOT_USERNAME=@ExampleSignalBot'):
            self.assertEqual(signal_username(raw), 'ExampleSignalBot')

    def test_reject_bad_bot_username(self):
        for raw in ('', 'https://evil.test/ExampleSignalBot', 't.me/+private',
                    't.me/ExampleSignalBot/123', 'some bot name', '12345'):
            with self.assertRaises(SafeIssue):
                signal_username(raw)

    def test_phase_classification_of_same_valueerror(self):
        secret = 'do-not-leak-session-here'
        for stage, expected in [('telegram_session_parse','TG_SESSION_INVALID'),
                                ('signal_bot_resolve','SIGNAL_BOT_UNRESOLVED'),
                                ('pocket_credentials','PO_CREDENTIALS_INVALID'),
                                ('order_result','ORDER_UNCERTAIN')]:
            d = diagnose_exception(ValueError(secret),stage)
            self.assertEqual(d.code,expected)
            self.assertNotIn(secret,d.public_text()+d.log_line())

    def test_traceback_location_has_no_exception_text_or_code_line(self):
        try:
            raise ValueError('secret_mongo_uri_and_api_token')
        except ValueError as exc:
            d = diagnose_exception(exc, 'signal_bot_resolve')
        self.assertIn('test_startup_fix.py:',d.location)
        self.assertNotIn('secret_mongo', d.public_text()+d.log_line())

    def test_balance_accepts_boolean_strings_and_ignores_bad_flag(self):
        po=CloudPocket({'ssid':'unused','ws_url':config.DEMO_URL},'PRACTICE')
        po.process(['updateBalance',{'balance':100, 'isDemo':'true'}])
        self.assertEqual(po.balance,100)
        po.process(['updateBalance',{'balance':999, 'isDemo':'not-a-number'}])
        self.assertEqual(po.balance,100)
        real=CloudPocket({'ssid':'unused','ws_url':config.REAL_URL},'REAL')
        real.process(['updateBalance',{'balance':50, 'isDemo':'false'}])
        self.assertEqual(real.balance,50)

    def test_dashboard_includes_connection_only_diagnostics(self):
        values=[b.callback_data for row in home().inline_keyboard for b in row]
        self.assertIn('tools',values)
        from control_bot import tools_kb
        self.assertIn('diagnose',[b.callback_data for row in tools_kb().inline_keyboard for b in row])


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.patch_session=patch.object(config,'TELEGRAM_STRING_SESSION',valid_session())
        self.patch_name=patch.object(config,'SIGNAL_BOT_USERNAME','ExampleSignalBot')
        self.patch_session.start(); self.patch_name.start()
        self.addCleanup(self.patch_session.stop); self.addCleanup(self.patch_name.stop)

    async def test_invalid_session_start_fails_before_task_or_any_order(self):
        factory=AsyncMock()
        w=TradingWorker(repo_fixture(), telegram_factory=factory)
        with patch.object(config,'TELEGRAM_STRING_SESSION','po_session'):
            with self.assertRaisesRegex(ValueError,'TG_SESSION_INVALID'):
                await w.start()
        self.assertIsNone(w.task)
        factory.assert_not_called()
        self.assertEqual(w.last_error,'TG_SESSION_INVALID')
        self.assertFalse(w.running)

    async def test_invalid_pocket_credentials_labeled_before_start(self):
        r=repo_fixture(); r.doc['demo_account']['ssid']='...'
        w=TradingWorker(r)
        with self.assertRaisesRegex(ValueError,'PO_CREDENTIALS_INVALID'):
            await w.start()
        self.assertIsNone(w.task)

    async def test_unknown_signal_bot_records_exact_stage_and_cleans_client(self):
        client=SimpleNamespace(is_connected=lambda:True, connect=AsyncMock(),
                               disconnect=AsyncMock(), is_user_authorized=AsyncMock(return_value=True))
        listener=SimpleNamespace(init=AsyncMock(side_effect=ValueError('cannot find username PRIVATE_SECRET')))
        w=TradingWorker(repo_fixture(),telegram_factory=lambda *a:client)
        with patch.object(worker_module,'SignalListener',return_value=listener):
            with self.assertRaises(ValueError) as ctx:
                await w._telegram()
        d=await w.record_error(ctx.exception)
        self.assertEqual(d.code,'SIGNAL_BOT_UNRESOLVED')
        self.assertEqual(d.stage,'signal_bot_resolve')
        self.assertIsNone(w.tg)
        self.assertIsNone(w.listener)
        client.disconnect.assert_awaited()
        self.assertNotIn('PRIVATE_SECRET',d.public_text())

    async def test_partial_connected_client_is_rebuilt_and_resolved(self):
        old=SimpleNamespace(is_connected=lambda:True,disconnect=AsyncMock())
        new=SimpleNamespace(is_connected=lambda:True,connect=AsyncMock(),disconnect=AsyncMock(),
                            is_user_authorized=AsyncMock(return_value=True))
        listener=SimpleNamespace(init=AsyncMock(),bot_entity=SimpleNamespace(bot=True))
        w=TradingWorker(repo_fixture(),telegram_factory=lambda *a:new)
        w.tg=old; w.listener=None
        with patch.object(worker_module,'SignalListener',return_value=listener):
            await w._telegram()
        old.disconnect.assert_awaited_once()
        new.connect.assert_awaited_once()
        listener.init.assert_awaited_once()
        self.assertIs(w.listener,listener)

    async def test_unauthorized_string_session_is_reported_not_login_prompt(self):
        client=SimpleNamespace(is_connected=lambda:True,connect=AsyncMock(),disconnect=AsyncMock(),
                               is_user_authorized=AsyncMock(return_value=False))
        w=TradingWorker(repo_fixture(),telegram_factory=lambda *a:client)
        with self.assertRaisesRegex(SafeIssue,'TG_SESSION_UNAUTHORIZED'):
            await w._telegram()
        self.assertIsNone(w.tg)

    async def test_non_bot_target_rejected_without_leaving_handler_initialized(self):
        client=SimpleNamespace(is_connected=lambda:True,connect=AsyncMock(),disconnect=AsyncMock(),
                               is_user_authorized=AsyncMock(return_value=True))
        listener=SimpleNamespace(init=AsyncMock(),bot_entity=SimpleNamespace(bot=False))
        w=TradingWorker(repo_fixture(),telegram_factory=lambda *a:client)
        with patch.object(worker_module,'SignalListener',return_value=listener):
            with self.assertRaisesRegex(SafeIssue,'SIGNAL_TARGET_NOT_BOT'):
                await w._telegram()
        self.assertIsNone(w.listener)

    async def test_notifier_receives_allowlisted_message_and_throttles(self):
        w=TradingWorker(repo_fixture()); w.stage='signal_bot_resolve'; w.notify=AsyncMock()
        out=io.StringIO()
        with contextlib.redirect_stdout(out):
            await w.record_error(ValueError('PRIVATE_SESSION'))
            await w.record_error(ValueError('PRIVATE_SESSION'))
        w.notify.assert_awaited_once()
        self.assertNotIn('PRIVATE_SESSION',out.getvalue())
        self.assertIn('SIGNAL_BOT_UNRESOLVED',out.getvalue())

    async def test_connection_diagnostics_do_not_click_signal_or_trade(self):
        po=SimpleNamespace(close=AsyncMock(),connect=AsyncMock(),balance=10, connected=True)
        w=TradingWorker(repo_fixture(),po_factory=lambda *args:po)
        w._telegram=AsyncMock()
        text=await w.diagnose()
        self.assertIn('لم تُطلب إشارة',text)
        self.assertIsNone(w.task)
        self.assertFalse(w.running)
        po.connect.assert_awaited_once()

    async def test_diagnostic_refuses_to_interrupt_running_task(self):
        w=TradingWorker(repo_fixture())
        w.task=asyncio.create_task(asyncio.sleep(60))
        w._telegram=AsyncMock()
        try:
            text=await w.diagnose()
            self.assertIn('أوقف التداول',text)
            w._telegram.assert_not_awaited()
        finally:
            w.task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await w.task

    async def test_runtime_failure_preserves_pending_and_stops(self):
        r=repo_fixture(); r.doc['runtime']['pending']={'id':'existing'}
        w=TradingWorker(r)
        w._telegram=AsyncMock(side_effect=ValueError('SECRET_EXCEPTION'))
        await w._run()
        self.assertEqual(w.pending['id'],'existing')
        self.assertFalse(w.running)
        self.assertEqual(w.stage,'error_review')
        self.assertNotIn('SECRET_EXCEPTION',w.diagnostic.public_text())

    async def test_dispatcher_construction_sets_notifier_no_network(self):
        w=TradingWorker(repo_fixture()); bot=FakeBot()
        dp=build_dispatcher(w.repo,w,bot)
        self.assertIsNotNone(dp)
        await w.notify('safe-test')
        bot.send_photo.assert_awaited_once()
        bot.send_message.assert_not_awaited()


if __name__=='__main__':
    unittest.main()
