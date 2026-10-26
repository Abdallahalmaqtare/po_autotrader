"""Local branded Telegram panels, independently layered above the trading worker.
Pictures ship in the deployment: no remote URL, API generation, Pillow or download
is needed at runtime. Telegram file_ids are cached for this Bot instance only.
"""
import asyncio
from runtime_support import timestamp_print as print
from dataclasses import dataclass, field
from pathlib import Path
from contextlib import suppress
from aiogram.types import FSInputFile, InputMediaPhoto
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
import config

BRAND = 'عبود | ABOUD TRADING CONTROLLER'
ASSET_DIR = Path(__file__).resolve().parent / 'assets' / 'brand'
CARDS = {
    'home': 'home.jpg', 'settings': 'settings.jpg', 'status': 'status.jpg',
    'stats': 'stats.jpg', 'start': 'start.jpg', 'stop': 'stop.jpg',
    'forex': 'forex.jpg', 'accounts': 'accounts.jpg', 'limits': 'limits.jpg',
    'reset': 'reset.jpg', 'setup': 'setup.jpg', 'diagnostic': 'diagnostic.jpg',
}
TITLES = {
    'home': '🎛 لوحة التحكم', 'settings': '⚙️ إعدادات التداول',
    'status': '🔄 الحالة الحالية', 'stats': '📊 الإحصائيات والتقرير المالي',
    'start': '▶️ طلب بدء التداول', 'stop': '⏹ إيقاف التداول',
    'forex': '💱 أزواج فوركس OTC', 'accounts': '👤 إدارة الحسابات',
    'limits': '🛡 وضع التشغيل وحدود الربح والخسارة',
    'reset': '🗑 تصفير السجل', 'setup': '⚙️ تخصيص إعدادات الصفقة',
    'diagnostic': '🧪 فحص الاتصال والإشعارات',
}


def utf16_size(text):
    return len(text.encode('utf-16-le')) // 2


def text_chunks(text, max_units=3900):
    """Split on Unicode characters (never half an emoji) below Telegram's limit."""
    parts, chars, size = [], [], 0
    for char in text:
        width = 2 if ord(char) > 0xffff else 1
        if size + width > max_units:
            parts.append(''.join(chars)); chars = []; size = 0
        chars.append(char); size += width
    if chars or not parts:
        parts.append(''.join(chars))
    return parts


def card_for(action, text=''):
    action = action or 'home'
    if action.startswith('cycle'):
        return 'setup'
    if action.startswith('payout'):
        return 'limits'
    if action.startswith('recovery'):
        return 'setup'
    if action == 'timing':
        return 'setup'
    if action == 'tools':
        return 'diagnostic'
    if action in ('limits',) or action.startswith(('risk:', 'limit:')):
        return 'limits'
    if action.startswith('reset'):
        return 'reset'
    if action in ('accounts', 'real_confirm', 'credential', 'url') or action.startswith(('mode:', 'credentials:')):
        return 'accounts'
    if action.startswith(('assets:', 'asset:')):
        return 'forex'
    if action in ('amount', 'mg', 'duration', 'number') or action.startswith(('number:', 'value:')):
        return 'setup'
    if action in ('diagnose', 'review', 'review_confirm', 'error', 'notice'):
        return 'limits' if 'توقف تلقائي' in text else 'diagnostic'
    if action == 'start' and ('توقف تلقائي' in text or 'الحد' in text and 'ممنوع' in text):
        return 'limits'
    return action if action in CARDS else 'home'


@dataclass
class ActivePanel:
    message_id: int
    card: str
    caption: str
    details: list = field(default_factory=list)


class BrandedPanels:
    def __init__(self, bot):
        self.bot = bot
        self.chat_id = config.ADMIN_TELEGRAM_ID
        self.files = {}
        self.active = None
        self.lock = asyncio.Lock()

    def image(self, card, fresh=False):
        path = ASSET_DIR / CARDS[card]
        if not path.is_file():
            raise FileNotFoundError(CARDS[card])
        return FSInputFile(path) if fresh or card not in self.files else self.files[card]

    def remember(self, message, card):
        photos = getattr(message, 'photo', None)
        if photos:
            self.files[card] = photos[-1].file_id

    async def _photo(self, card, caption, markup):
        for fresh in (False, True):
            try:
                result = await self.bot.send_photo(self.chat_id, self.image(card, fresh),
                    caption=caption, reply_markup=markup, parse_mode=None)
                self.remember(result, card)
                return result
            except TelegramBadRequest:
                if fresh or card not in self.files:
                    raise
                self.files.pop(card, None)  # a stale cached file_id must not break UI
        raise RuntimeError('photo_not_sent')

    async def _edit(self, current, card, caption, markup):
        if current.card == card:
            try:
                return await self.bot.edit_message_caption(chat_id=self.chat_id,
                    message_id=current.message_id, caption=caption,
                    reply_markup=markup, parse_mode=None)
            except TelegramBadRequest as exc:
                if 'message is not modified' in exc.message.lower():
                    return None  # normal repeat-click; never create a duplicate panel
                raise
        for fresh in (False, True):
            try:
                media = InputMediaPhoto(media=self.image(card, fresh), caption=caption, parse_mode=None)
                result = await self.bot.edit_message_media(chat_id=self.chat_id,
                    message_id=current.message_id, media=media, reply_markup=markup)
                self.remember(result, card)
                return result
            except TelegramBadRequest as exc:
                if 'message is not modified' in exc.message.lower():
                    return None
                if fresh or card not in self.files:
                    raise
                self.files.pop(card, None)
        raise RuntimeError('photo_not_edited')

    async def _remove_details(self, current):
        if current:
            for mid in current.details:
                with suppress(TelegramAPIError):
                    await self.bot.delete_message(self.chat_id, mid)
            current.details.clear()

    async def _remove_keyboard(self, message_id):
        with suppress(TelegramAPIError):
            await self.bot.edit_message_reply_markup(chat_id=self.chat_id,
                message_id=message_id, reply_markup=None)

    async def _text_fallback(self, text, markup):
        chunks = text_chunks(BRAND + '\n\n' + text)
        result = None
        for index, part in enumerate(chunks):
            result = await self.bot.send_message(self.chat_id, part, parse_mode=None,
                reply_markup=markup if index == len(chunks)-1 else None)
        return result

    async def show(self, text, markup=None, view='home', origin=None):
        """Edit one active photo panel. Long reports are complete follow-up text.
        A photo upload failure never rolls back a successful trading setting.
        """
        card = card_for(view, text)
        full = BRAND + '\n\n' + text
        long = utf16_size(full) > 1000
        caption = BRAND + '\n\n' + TITLES[card] + '\nالتفاصيل الكاملة في الرسالة أدناه ↓' if long else full
        async with self.lock:
            previous = self.active
            try:
                if previous:
                    try:
                        await self._edit(previous, card, caption, markup)
                        active = ActivePanel(previous.message_id, card, caption)
                    except TelegramBadRequest:
                        result = await self._photo(card, caption, markup)
                        await self._remove_keyboard(previous.message_id)
                        active = ActivePanel(result.message_id, card, caption)
                else:
                    # After redeploy we may receive an old photo callback. Adopt it;
                    # text-only panels from old releases are replaced with a photo.
                    photo = getattr(origin, 'photo', None) if origin is not None else None
                    if photo and getattr(origin, 'message_id', None):
                        old = ActivePanel(origin.message_id, '__old__', '')
                        try:
                            await self._edit(old, card, caption, markup)
                            active = ActivePanel(origin.message_id, card, caption)
                        except TelegramBadRequest:
                            result = await self._photo(card, caption, markup)
                            await self._remove_keyboard(origin.message_id)
                            active = ActivePanel(result.message_id, card, caption)
                    else:
                        result = await self._photo(card, caption, markup)
                        active = ActivePanel(result.message_id, card, caption)
                        if origin is not None and getattr(origin, 'message_id', None):
                            await self._remove_keyboard(origin.message_id)
                await self._remove_details(previous)
                self.active = active
                if long:
                    for chunk in text_chunks(text):
                        message = await self.bot.send_message(self.chat_id, chunk, parse_mode=None)
                        active.details.append(message.message_id)
                return active
            except (TelegramAPIError, OSError) as exc:
                # Log only a safe exception class, not a token, URI or payload.
                print(f'[UI] photo panel unavailable ({type(exc).__name__}); using text', flush=True)
                if self.active:
                    await self._remove_details(self.active)
                    with suppress(TelegramAPIError):
                        await self._remove_keyboard(self.active.message_id)
                self.active = None
                try:
                    return await self._text_fallback(text, markup)
                except TelegramAPIError as fallback:
                    print(f'[UI] Telegram delivery unavailable ({type(fallback).__name__})', flush=True)
                    return None

    async def notice(self, text, markup=None, view='notice'):
        """Independent notification; leave the current navigation panel intact."""
        card = card_for(view, text)
        full = BRAND + '\n\n' + text
        caption = full if utf16_size(full) <= 1000 else BRAND + '\n\n' + TITLES[card]
        try:
            result = await self._photo(card, caption, markup)
            if caption != full:
                for chunk in text_chunks(text):
                    await self.bot.send_message(self.chat_id, chunk, parse_mode=None)
            return result
        except (TelegramAPIError, OSError) as exc:
            print(f'[UI] notice photo unavailable ({type(exc).__name__}); using text', flush=True)
            with suppress(TelegramAPIError):
                return await self._text_fallback(text, markup)


def get_panels(bot):
    ui = getattr(bot, '_aboud_panels', None)
    if ui is None:
        ui = BrandedPanels(bot)
        bot._aboud_panels = ui
    return ui
