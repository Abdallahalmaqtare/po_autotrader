"""v17 offline UI/media/catalog regressions; no Telegram or trading connection."""
import asyncio
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import EditMessageMedia, EditMessageCaption, SendPhoto
from aiogram.types import FSInputFile, InputMediaPhoto
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.base import StorageKey
from branded_ui import BrandedPanels, CARDS, ASSET_DIR, card_for, text_chunks, utf16_size
from forex_assets import FOREX_OTC, SYMBOL_NAMES, STORED_ASSET_NAMES
from cloud_settings import defaults, validate_document, patched
from control_bot import assets_kb, build_dispatcher, AdminOnly
from worker import TradingWorker
from test_v16_features import real_repo, auth
from ui_helpers import FakeBot

EXPECTED_PAGES = [
    ['EUR/USD','GBP/USD','USD/JPY','AUD/USD','CAD/JPY','GBP/AUD'],
    ['EUR/CAD','EUR/JPY','GBP/JPY','USD/CAD','AUD/JPY','EUR/GBP'],
    ['CAD/CHF','CHF/JPY','EUR/CHF','GBP/CHF','NZD/CAD','NZD/CHF'],
    ['CHF/NOK','EUR/HUF','JOD/CNY','NGN/USD','NZD/JPY','SAR/CNY'],
    ['AED/CNY','BHD/CNY','KES/USD','OMR/CNY','QAR/CNY','USD/INR'],
]


class CatalogTests(unittest.TestCase):
    def test_exact_thirty_assets_in_pictured_order(self):
        names = [name+' (OTC)' for page in EXPECTED_PAGES for name in page]
        self.assertEqual([name for name, symbol in FOREX_OTC], names)
        self.assertEqual(len(FOREX_OTC), 30)
        self.assertEqual(len(SYMBOL_NAMES), 30)
        self.assertEqual(len(set(s for _, s in FOREX_OTC)), 30)

    def test_all_five_pages_are_three_rows_two_columns(self):
        for page, expected in enumerate(EXPECTED_PAGES):
            with self.subTest(page=page+1):
                rows = assets_kb(page).inline_keyboard
                self.assertEqual([len(r) for r in rows[:3]], [2, 2, 2])
                self.assertEqual([b.text for r in rows[:3] for b in r], [n+' (OTC)' for n in expected])
                self.assertEqual([b.callback_data for r in rows[:3] for b in r],
                                 ['asset:'+n.replace('/','')+'_otc' for n in expected])
                self.assertEqual(rows[3][1].text, f'{page+1}/5')
                self.assertEqual(rows[4][0].text, 'Back')
                self.assertEqual(rows[4][0].callback_data, 'settings')

    def test_first_last_navigation_disabled_and_out_of_range_clamped(self):
        self.assertEqual(assets_kb(0).inline_keyboard[3][0].callback_data, 'noop')
        self.assertEqual(assets_kb(4).inline_keyboard[3][2].callback_data, 'noop')
        self.assertEqual(assets_kb(1).inline_keyboard[3][0].callback_data, 'assets:0')
        self.assertEqual(assets_kb(3).inline_keyboard[3][2].callback_data, 'assets:4')
        self.assertEqual(assets_kb(-100), assets_kb(0))
        self.assertEqual(assets_kb(999), assets_kb(4))

    def test_new_symbols_all_accepted_by_settings_validation(self):
        for _, symbol in FOREX_OTC:
            d = patched(defaults(), {'trading.asset': symbol})
            self.assertEqual(d['trading']['asset'], symbol)

    def test_old_saved_asset_does_not_break_redeploy_or_other_edits(self):
        d = defaults(); d['trading']['asset'] = 'EURNZD_otc'
        self.assertEqual(validate_document(d)['trading']['asset'], 'EURNZD_otc')
        self.assertEqual(patched(d, {'risk.profit_limit': 80})['trading']['asset'], 'EURNZD_otc')
        with self.assertRaises(ValueError):
            patched(d, {'trading.asset': 'EURNZD_otc'})
        for raw in ('Apple_otc', 'Tesla_otc', 'EURUSD', 'UNKNOWN_otc'):
            with self.assertRaises(ValueError):
                patched(defaults(), {'trading.asset': raw})

    def test_twelve_local_cards_and_safe_filenames(self):
        self.assertEqual(len(CARDS), 12)
        for filename in CARDS.values():
            path = ASSET_DIR/filename
            self.assertGreater(path.stat().st_size, 10000)
            self.assertTrue(path.read_bytes().startswith(b'\xff\xd8\xff'))
            self.assertEqual(path.parent, ASSET_DIR)

    def test_unicode_splitting_reconstructs_entire_report(self):
        full = 'تقرير الأداء 📊\n' * 900
        chunks = text_chunks(full)
        self.assertEqual(''.join(chunks), full)
        self.assertTrue(all(utf16_size(part) <= 3900 for part in chunks))
        self.assertGreater(len(chunks), 2)

    def test_all_interface_routes_have_an_intended_card(self):
        mapping = {'home':'home','start':'start','stop':'stop','settings':'settings',
                   'status':'status','stats':'stats','accounts':'accounts',
                   'real_confirm':'accounts','mode:REAL':'accounts','credentials:PRACTICE':'accounts',
                   'assets:4':'forex','asset:EURUSD_otc':'forex','limits':'limits',
                   'risk:TRADITIONAL':'limits','risk:LIMITED':'limits',
                   'limit:profit_limit':'limits','limit:loss_limit':'limits',
                   'reset_stats':'reset','reset_confirm':'reset','amount':'setup',
                   'mg':'setup','duration':'setup','number:trade_amount':'setup',
                   'value:trade_amount:1':'setup','review':'diagnostic','review_confirm':'diagnostic',
                   'diagnose':'diagnostic','error':'diagnostic'}
        for action, card in mapping.items():
            self.assertEqual(card_for(action), card)
        self.assertEqual(card_for('notice', '🛑 توقف تلقائي: بلوغ حد الربح'), 'limits')
        self.assertEqual(card_for('start', '🛑 توقف تلقائي: بلوغ حد الخسارة'), 'limits')


class MediaTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = FakeBot()
        self.ui = BrandedPanels(self.bot)

    async def test_first_panel_uploads_local_asset_then_edits_media(self):
        await self.ui.show('اللوحة الرئيسية', view='home')
        sent = self.bot.send_photo.call_args
        self.assertIsInstance(sent.args[1], FSInputFile)
        mid = self.ui.active.message_id
        await self.ui.show('الإعدادات', view='settings')
        self.assertEqual(self.ui.active.message_id, mid)
        self.assertEqual(self.bot.send_photo.await_count, 1)
        media = self.bot.edit_message_media.call_args.kwargs['media']
        self.assertIsInstance(media, InputMediaPhoto)
        self.assertIsInstance(media.media, FSInputFile)
        self.assertIsNone(media.parse_mode)

    async def test_repeated_same_view_updates_caption_not_new_photo(self):
        await self.ui.show('حالة 1', view='status')
        await self.ui.show('حالة 2', view='status')
        self.bot.send_photo.assert_awaited_once()
        self.bot.edit_message_caption.assert_awaited_once()
        self.bot.edit_message_media.assert_not_awaited()

    async def test_uploaded_file_ids_reused_without_reupload(self):
        await self.ui.show('رئيسية', view='home')
        await self.ui.show('إعدادات', view='settings')
        await self.ui.show('رئيسية', view='home')
        media = self.bot.edit_message_media.call_args.kwargs['media']
        self.assertIsInstance(media.media, str)
        self.assertTrue(media.media.startswith('cached-photo-'))

    async def test_stale_file_id_retries_from_bundled_file(self):
        await self.ui.show('رئيسية', view='home')
        await self.ui.show('إعدادات', view='settings')
        original = self.bot.edit_message_media.side_effect
        async def fail_cached(**kwargs):
            if isinstance(kwargs['media'].media, str):
                raise TelegramBadRequest(method=EditMessageMedia(media=kwargs['media']), message='wrong file identifier')
            return await original(**kwargs)
        self.bot.edit_message_media.side_effect = fail_cached
        await self.ui.show('رئيسية', view='home')
        self.assertEqual(self.ui.active.card, 'home')
        self.assertIsInstance(self.bot.edit_message_media.call_args.kwargs['media'].media, FSInputFile)

    async def test_message_not_modified_is_a_safe_noop(self):
        await self.ui.show('الحالة', view='status')
        self.bot.edit_message_caption.side_effect = TelegramBadRequest(
            method=EditMessageCaption(), message='Bad Request: message is not modified')
        await self.ui.show('الحالة', view='status')
        self.bot.send_photo.assert_awaited_once()
        self.assertEqual(self.ui.active.card, 'status')

    async def test_long_report_is_not_truncated_or_rejected_as_caption(self):
        text = 'صافي الحساب $50.00 📊\n' * 500
        await self.ui.show(text, view='stats')
        self.assertLessEqual(utf16_size(self.ui.active.caption), 1000)
        delivered = ''.join(call.args[1] for call in self.bot.send_message.call_args_list)
        self.assertEqual(delivered, text)
        self.assertTrue(all(utf16_size(call.args[1]) <= 3900 for call in self.bot.send_message.call_args_list))
        old_details = list(self.ui.active.details)
        await self.ui.show('الإعدادات', view='settings')
        self.assertEqual(self.bot.delete_message.await_count, len(old_details))
        self.assertEqual(self.ui.active.details, [])

    async def test_report_chunks_removed_when_refreshing_same_stats_panel(self):
        await self.ui.show('إحصائية طويلة\n' * 150, view='stats')
        self.assertTrue(self.ui.active.details)
        count = len(self.ui.active.details)
        await self.ui.show('إحصائية محدّثة\n' * 150, view='stats')
        self.assertEqual(self.bot.delete_message.await_count, count)
        self.bot.send_photo.assert_awaited_once()

    async def test_image_failure_falls_back_to_text_without_throwing(self):
        self.bot.send_photo.side_effect = TelegramNetworkError(method=SendPhoto(chat_id=1, photo='x'), message='offline')
        await self.ui.show('إعداد محفوظ', view='settings')
        self.bot.send_message.assert_awaited_once()
        self.assertIn('إعداد محفوظ', self.bot.send_message.call_args.args[1])
        self.assertIsNone(self.ui.active)

    async def test_missing_local_file_also_falls_back_to_text(self):
        with patch('branded_ui.ASSET_DIR', Path('/no/such/brand/assets')):
            await self.ui.show('الحالة', view='status')
        self.bot.send_message.assert_awaited_once()

    async def test_old_text_panel_replaced_after_redeploy(self):
        old = SimpleNamespace(message_id=5, photo=[])
        await self.ui.show('لوحة جديدة', view='home', origin=old)
        self.bot.send_photo.assert_awaited_once()
        self.bot.edit_message_reply_markup.assert_awaited_once()
        self.assertNotEqual(self.ui.active.message_id, 5)

    async def test_old_photo_panel_adopted_after_redeploy(self):
        old = SimpleNamespace(message_id=7, photo=[SimpleNamespace(file_id='old')])
        await self.ui.show('لوحة جديدة', view='home', origin=old)
        self.bot.send_photo.assert_not_awaited()
        self.bot.edit_message_media.assert_awaited_once()
        self.assertEqual(self.ui.active.message_id, 7)

    async def test_notice_does_not_replace_active_navigation_panel(self):
        await self.ui.show('الإعدادات', view='settings')
        active = self.ui.active
        await self.ui.notice('🛑 توقف تلقائي: بلوغ حد الربح')
        self.assertIs(self.ui.active, active)
        path = self.bot.send_photo.call_args.args[1].path
        self.assertEqual(Path(path).name, 'limits.jpg')

    async def test_deleted_active_panel_recreated_safely(self):
        await self.ui.show('الحالة', view='status')
        mid = self.ui.active.message_id
        self.bot.edit_message_caption.side_effect = TelegramBadRequest(
            method=EditMessageCaption(), message='message to edit not found')
        await self.ui.show('حالة جديدة', view='status')
        self.assertEqual(self.bot.send_photo.await_count, 2)
        self.assertNotEqual(self.ui.active.message_id, mid)

    async def test_concurrent_panels_serialized(self):
        await asyncio.gather(self.ui.show('رئيسية', view='home'),
                             self.ui.show('إعدادات', view='settings'))
        self.bot.send_photo.assert_awaited_once()
        self.bot.edit_message_media.assert_awaited_once()


class ControllerScreensTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.repo = await real_repo()
        self.worker = TradingWorker(self.repo)
        self.bot = FakeBot()
        self.dp = build_dispatcher(self.repo, self.worker, self.bot)
        self.state = FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=1, user_id=1))
        self.callback = next(h.callback for h in self.dp.callback_query.handlers if h.callback.__name__ == 'callback')

    async def action(self, value):
        cb = SimpleNamespace(data=value, answer=AsyncMock(), message=SimpleNamespace(message_id=20, photo=[]))
        await self.callback(cb, self.state)
        cb.answer.assert_awaited_once()
        return cb

    async def test_all_five_catalog_pages_navigate_without_sixth(self):
        for page in range(5):
            await self.action(f'assets:{page}')
            self.assertEqual(self.bot._aboud_panels.active.card, 'forex')
            self.assertIn(f'{page+1}/5', self.bot._aboud_panels.active.caption)
        self.assertEqual(self.bot.send_photo.await_count, 1)
        self.assertEqual(self.bot.edit_message_caption.await_count, 4)

    async def test_every_new_pair_can_be_saved_by_callback(self):
        for _, symbol in FOREX_OTC:
            await self.action('asset:'+symbol)
            self.assertEqual(self.repo.collection.doc['trading']['asset'], symbol)
        self.assertEqual(self.bot.send_photo.await_count, 1)

    async def test_main_settings_account_stats_status_and_risk_have_cards(self):
        cases = [('home','home'),('settings','settings'),('accounts','accounts'),
                 ('stats','stats'),('status','status'),('limits','limits'),
                 ('real_confirm','accounts'),('reset_stats','reset'),('amount','setup'),
                 ('mg','setup'),('duration','setup'),('number:trade_amount','setup'),
                 ('limit:profit_limit','limits')]
        for value, card in cases:
            await self.action(value)
            self.assertEqual(self.bot._aboud_panels.active.card, card, value)
        self.assertFalse(self.worker.running)
        self.assertIsNone(self.worker.task)

    async def test_start_stop_requests_use_images_and_do_not_duplicate_actions(self):
        self.worker.start = AsyncMock(return_value='تم طلب البدء')
        self.worker.stop = AsyncMock(return_value='تم الإيقاف')
        self.worker.request_stop = unittest.mock.Mock()
        await self.action('start')
        self.worker.start.assert_awaited_once()
        self.assertEqual(self.bot._aboud_panels.active.card, 'start')
        await self.action('stop')
        self.worker.stop.assert_awaited_once()
        self.assertEqual(self.bot._aboud_panels.active.card, 'stop')

    async def test_reset_confirmation_still_required_and_keeps_limits(self):
        self.repo.doc['account_stats']['PRACTICE']['net_pnl'] = 20
        await self.action('reset_stats')
        self.assertEqual(self.repo.doc['account_stats']['PRACTICE']['net_pnl'], 20)
        await self.action('reset_confirm')
        self.assertEqual(self.repo.doc['account_stats']['PRACTICE']['net_pnl'], 0)
        self.assertEqual(self.repo.doc['risk']['profit_limit'], 50)
        self.assertEqual(self.bot._aboud_panels.active.card, 'reset')

    async def test_unauthorized_users_trigger_no_media_or_database_calls(self):
        handler = AsyncMock()
        event = SimpleNamespace(from_user=SimpleNamespace(id=-1), chat=SimpleNamespace(type='private',id=-1))
        await AdminOnly()(handler, event, {})
        handler.assert_not_awaited()
        self.bot.send_photo.assert_not_awaited()
        self.bot.send_message.assert_not_awaited()

    async def test_start_and_cancel_commands_are_branded_panels(self):
        start = next(h.callback for h in self.dp.message.handlers if h.callback.__name__ == 'start_panel')
        cancel = next(h.callback for h in self.dp.message.handlers if h.callback.__name__ == 'cancel')
        msg = SimpleNamespace(answer=AsyncMock())
        await start(msg, self.state)
        self.assertEqual(self.bot._aboud_panels.active.card, 'home')
        await cancel(msg, self.state)
        self.assertEqual(self.bot._aboud_panels.active.card, 'settings')
        msg.answer.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
