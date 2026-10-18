"""Admin-only private Telegram controller. No startup/global worker side effects."""
import math
from contextlib import suppress
from aiogram import Bot, Dispatcher, F, BaseMiddleware
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.exceptions import TelegramBadRequest
import config
from cloud_settings import MODES, DURATIONS, valid_number, valid_url, auth_frame
from forex_assets import FOREX_OTC, SYMBOL_NAMES


class AdminOnly(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, 'from_user', None)
        message = event.message if isinstance(event, CallbackQuery) else event
        chat = getattr(message, 'chat', None)
        if not (user and user.id == config.ADMIN_TELEGRAM_ID and
                chat and chat.type == 'private' and chat.id == config.ADMIN_TELEGRAM_ID):
            if isinstance(event, CallbackQuery):
                with suppress(Exception):
                    await event.answer()
            return  # no state changes, no messages to untrusted users
        return await handler(event, data)


class Input(StatesGroup):
    credential = State()
    url = State()
    number = State()


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data=value) for text, value in row] for row in rows])


def home():
    return kb([[('▶️ بدء التداول | Start', 'start'), ('⏹️ إيقاف | Stop', 'stop')],
               [('⚙️ الإعدادات | Settings', 'settings')],
               [('📊 الإحصائيات | Stats', 'stats'), ('🔄 الحالة | Status', 'status')],
               [('🧪 فحص الاتصال دون تداول', 'diagnose')],
               [('🗑 تصفير السجل | Reset Stats', 'reset_stats')]])


def settings_kb():
    return kb([[('👤 إدارة الحسابات | Accounts', 'accounts')],
               [('💱 أزواج فوركس OTC', 'assets:0')],
               [('💵 المبلغ الأساسي', 'amount'), ('🪜 التدبيل', 'mg')],
               [('⏱ مدة الصفقة', 'duration'), ('⚡ تأخير الدخول', 'number:entry_delay_seconds')],
               [('⏳ الفاصل بين الدورات', 'number:loop_delay_seconds')],
               [('⌛ مهلة النتيجة الإضافية', 'number:result_timeout_extra')],
               [('💰 انتظار الرصيد', 'number:balance_settle_seconds'),
                ('🔍 إعادة فحص الخسارة', 'number:loss_recheck_seconds')],
               [('⬅️ الرئيسية', 'home')]])


def accounts_kb():
    return kb([[('🟢 التحويل للحقيقي | Switch to Real', 'real_confirm')],
               [('🟡 التحويل للتجريبي | Switch to Demo', 'mode:PRACTICE')],
               [('✏️ تحديث بيانات الحقيقي', 'credentials:REAL')],
               [('✏️ تحديث بيانات الديمو', 'credentials:PRACTICE')],
               [('⬅️ الإعدادات', 'settings')]])


def assets_kb(page):
    total = math.ceil(len(FOREX_OTC)/6)
    page = max(0, min(page, total-1))
    rows = [[(name, 'asset:'+symbol)] for name, symbol in FOREX_OTC[page*6:page*6+6]]
    rows += [[('<', f'assets:{page-1}'), (f'{page+1}/{total}', 'noop'), ('>', f'assets:{page+1}')],
             [('⬅️ الإعدادات', 'settings')]]
    return kb(rows)


def report(repo, worker):
    d = repo.doc
    s = d['account_stats'][d['account_mode']]
    t = d['trading']
    settled = s['won_trades'] + s['lost_trades']
    rate = s['won_trades']/settled*100 if settled else 0
    bal = f'${worker.balance:.2f}' if worker.balance is not None else 'غير متاح'
    pending = 1 if worker.pending else 0
    net = s['net_pnl']
    net_s = f"+${net:.2f} 🟢" if net >= 0 else f"-${abs(net):.2f} 🔴"
    return (f"📊 تقرير الأداء — {d['account_mode']}\n"
            f"الحالة: {'شغال 🟢' if worker.running else 'متوقف 🔴'}\n"
            f"الزوج: {t['asset']} | المدة: {t['duration_seconds']}s\n"
            f"الأساس: ${t['trade_amount']:g} | مستويات التدبيل: {t['martingale_steps']}\n"
            '───────────────────────\n'
            '📈 الصفقات والنتائج:\n'
            f"• إجمالي الصفقات المنفذة: {s['total_trades']}\n"
            f"• رابحة (WON): {s['won_trades']}\n"
            f"• خاسرة (LOST): {s['lost_trades']}\n"
            f"• تعادل (DRAW): {s['draws']}\n"
            f"• خاسرة بعد استنفاد التدبيلات (سلسلة كاملة): {s['sequence_losses']}\n"
            f"• غير مؤكدة: {s['unconfirmed']} | معلّقة: {pending}\n"
            f"• نسبة النجاح (Win Rate): {rate:.1f}%\n"
            '───────────────────────\n'
            '💰 التقرير المالي (بمفهوم المنصة):\n'
            f"• العائد (إجمالي مبالغ الدخول): ${s['total_staked']:.2f}\n"
            f"• العوائد المستلمة: ${s['total_returned']:.2f}\n"
            f"• الربح / الخسارة الصافية: {net_s}\n"
            f"• إجمالي المفقود في السلاسل المستنفدة: ${s['lost_amount']:.2f}\n"
            f"• الرصيد الحالي للحساب: {bal}\n"
            'التقرير مستقل لكل حساب ومحفوظ عبر إعادة التشغيل.\n'
            'المبالغ استدلال من الرصيد وليست كشف حساب رسمي؛ الأوامر غير المحسومة لا تُحسب خسائر.')


async def panel(bot, cb, text, markup):
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        # e.g. double-clicked unchanged menu. Do not let a UI no-op stop polling.
        await bot.send_message(config.ADMIN_TELEGRAM_ID, text, reply_markup=markup)


def build_dispatcher(repo, worker, bot):
    dp = Dispatcher()
    async def notify_diagnostic(text):
        await bot.send_message(config.ADMIN_TELEGRAM_ID, '⚠️ تشخيص بدء التداول\n' + text)
    worker.notify = notify_diagnostic
    dp.message.outer_middleware(AdminOnly())
    dp.callback_query.outer_middleware(AdminOnly())

    @dp.message(CommandStart())
    async def start_panel(msg: Message, state: FSMContext):
        await state.clear()
        await msg.answer('🎛 لوحة تحكم التداول — لا يبدأ التداول تلقائياً بعد الإقلاع.', reply_markup=home())

    @dp.message(Command('cancel'))
    async def cancel(msg: Message, state: FSMContext):
        await state.clear()
        await msg.answer('أُلغي الإدخال.', reply_markup=settings_kb())

    @dp.callback_query()
    async def callback(cb: CallbackQuery, state: FSMContext):
        action = cb.data or ''
        if action == 'stop':
            worker.request_stop()
        await cb.answer()
        if action != 'noop':
            await state.clear()
        try:
            if action == 'noop':
                return
            if action == 'home':
                return await panel(bot, cb, '🎛 لوحة التحكم', home())
            if action == 'start':
                text = await worker.start()
                return await panel(bot, cb, text, home())
            if action == 'stop':
                # Mark stop before awaiting anything else.
                worker.request_stop()
                text = await worker.stop()
                return await panel(bot, cb, text, home())
            if action == 'reset_stats':
                return await panel(bot, cb, '🗑 تصفير السجل\nسيتم إيقاف التداول وتصفير كل الإحصائيات '
                                   'والعائد المالي لكلا الحسابين. هل أنت متأكد؟',
                                   kb([[('نعم، صفّر السجل', 'reset_confirm')], [('إلغاء', 'home')]]))
            if action == 'reset_confirm':
                await worker.stop()
                await repo.reset_stats()
                return await panel(bot, cb, '✅ تم تصفير السجل بالكامل. التداول متوقف — اضغط Start للبدء من جديد.',
                                   home())
            if action == 'stats':
                return await panel(bot, cb, report(repo, worker), home())
            if action == 'diagnose':
                text = await worker.diagnose()
                return await panel(bot, cb, text, home())
            if action == 'status':
                text = (f"الحساب: {repo.doc['account_mode']}\nالمرحلة: {worker.stage}\n"
                        f"WebSocket: {'متصل' if worker.po and worker.po.connected else 'غير متصل'}\n"
                        f"MongoDB writer: {'فعال' if repo.leader else 'غير فعال'}\n"
                        f"الزوج: {repo.doc['trading']['asset']}\n"
                        f"آخر كود خطأ: {worker.last_error or 'لا يوجد'}\n"
                        'البيانات السرية لا تظهر في اللوحة.')
                if worker.diagnostic:
                    text += '\n\n' + worker.diagnostic.public_text()
                rows = [[('🔍 مراجعة الأمر المعلّق', 'review')], [('⬅️ الرئيسية', 'home')]] if worker.pending else None
                return await panel(bot, cb, text, kb(rows) if rows else home())
            if action == 'settings':
                return await panel(bot, cb, '⚙️ الإعدادات؛ التغييرات العادية تسري من الدورة القادمة.', settings_kb())
            if action == 'accounts':
                return await panel(bot, cb, f"👤 الحساب الحالي: {repo.doc['account_mode']}\nلا تُعرض قيم SSID.", accounts_kb())
            if action == 'real_confirm':
                return await panel(bot, cb, '⚠️ الحساب الحقيقي يستخدم أموالاً حقيقية. تأكيد التبديل؟',
                                   kb([[('تأكيد REAL', 'mode:REAL')], [('إلغاء', 'accounts')]]))
            if action.startswith('mode:'):
                mode = action.split(':')[1]
                if mode not in MODES:
                    raise ValueError('حساب غير صالح')
                text = await worker.change({'account_mode': mode}, reconnect=True)
                return await panel(bot, cb, text, accounts_kb())
            if action.startswith('credentials:'):
                mode = action.split(':')[1]
                if mode not in MODES:
                    raise ValueError('حساب غير صالح')
                await state.set_state(Input.credential)
                await state.update_data(mode=mode)
                return await panel(bot, cb, f'أرسل إطار auth الكامل أو SSID لـ {mode}.\n'
                                   'سنحاول حذف رسالتك؛ لا تُرسل كلمة مرور حسابك. /cancel للإلغاء', None)
            if action.startswith('assets:'):
                return await panel(bot, cb, '💱 فوركس OTC فقط', assets_kb(int(action.split(':')[1])))
            if action.startswith('asset:'):
                text = await worker.change({'trading.asset': action.split(':')[1]})
                return await panel(bot, cb, text, settings_kb())
            if action == 'amount':
                return await panel(bot, cb, '💵 المبلغ الأساسي', kb([
                    [(f'${v}', f'value:trade_amount:{v}') for v in (1, 2, 5, 10)],
                    [('✍️ مبلغ مخصص', 'number:trade_amount')], [('⬅️', 'settings')]]))
            if action == 'mg':
                return await panel(bot, cb, 'المستويات تشمل الدخول الأساسي: 4 تعني 1، 2، 4، 8 عند أساس 1$.',
                                   kb([[(str(v), f'value:martingale_steps:{v}') for v in range(5)], [('⬅️', 'settings')]]))
            if action == 'duration':
                rows = [[(f'{v}s', f'value:duration_seconds:{v}') for v in DURATIONS[i:i+3]] for i in range(0, len(DURATIONS), 3)]
                return await panel(bot, cb, '⏱ مدة الصفقة', kb(rows + [[('⬅️', 'settings')]]))
            if action.startswith('value:'):
                _, key, raw = action.split(':')
                value = int(raw) if key in ('martingale_steps', 'duration_seconds') else float(raw)
                text = await worker.change({'trading.'+key: value})
                return await panel(bot, cb, text, settings_kb())
            if action.startswith('number:'):
                key = action.split(':')[1]
                if key not in repo.doc['trading'] or key == 'asset':
                    raise ValueError('خيار غير صالح')
                await state.set_state(Input.number)
                await state.update_data(key=key)
                return await panel(bot, cb, f'أرسل قيمة {key} كرقم؛ /cancel للإلغاء.', None)
            if action == 'review':
                return await panel(bot, cb, 'يوجد أمر قد يكون أُرسل قبل توقف الخدمة. راجع الحساب في المنصة أولاً.\n'
                                   'التأكيد التالي يؤرشفه كغير مؤكد ويعيد التدبيل للأساس، ولا يعيد إرسال الأمر ولا يخمّن PnL.',
                                   kb([[('راجعت المنصة، أرشفة كغير مؤكد', 'review_confirm')], [('إلغاء', 'home')]]))
            if action == 'review_confirm':
                await worker.stop()
                await repo.acknowledge_pending()
                return await panel(bot, cb, 'تمت الأرشفة؛ يلزم ضغط Start للبدء من جديد.', home())
        except ValueError as e:
            await bot.send_message(config.ADMIN_TELEGRAM_ID, str(e), reply_markup=settings_kb())
        except Exception as e:
            # Never echo raw exceptions containing Mongo URI/session/WS payloads.
            await bot.send_message(config.ADMIN_TELEGRAM_ID,
                f'تعذّرت العملية ({type(e).__name__}). لم يُؤكّد نجاح التغيير؛ راجع الحالة.', reply_markup=home())

    @dp.message(Input.credential)
    async def credential(msg: Message, state: FSMContext):
        data = await state.get_data()
        raw = msg.text or ''
        with suppress(Exception):
            await msg.delete()
        try:
            mode = data['mode']
            key = 'real_account' if mode == 'REAL' else 'demo_account'
            frame = auth_frame(raw, mode, repo.doc[key].get('uid', 0))
        except (ValueError, KeyError) as e:
            await msg.answer(str(e) if isinstance(e, ValueError) else 'أعد فتح إدارة الحسابات.')
            return
        await state.update_data(frame=frame)
        await state.set_state(Input.url)
        await msg.answer('تم استلام الجلسة دون عرضها. أرسل رابط wss أو /skip لاعتماد الرابط الافتراضي لهذا الحساب.\n/cancel للإلغاء')

    @dp.message(Input.url)
    async def url(msg: Message, state: FSMContext):
        data = await state.get_data()
        try:
            mode = data['mode']
            key = 'real_account' if mode == 'REAL' else 'demo_account'
            value = (config.REAL_URL if mode == 'REAL' else config.DEMO_URL) if msg.text == '/skip' else valid_url(msg.text)
            text = await worker.change({key+'.ssid': data['frame'], key+'.ws_url': value},
                                       reconnect=repo.doc['account_mode'] == mode)
            await state.clear()
            await msg.answer(text, reply_markup=accounts_kb())
        except ValueError as e:
            await msg.answer(str(e))
        except Exception as e:
            await state.clear()
            await msg.answer('لم يُؤكّد حفظ البيانات؛ حاول مجدداً. نوع الخطأ: '+type(e).__name__)

    @dp.message(Input.number)
    async def number_input(msg: Message, state: FSMContext):
        try:
            data = await state.get_data()
            value = valid_number(msg.text or '', .01 if data['key'] == 'trade_amount' else 0,
                                 100000 if data['key'] == 'trade_amount' else 120)
            text = await worker.change({'trading.'+data['key']: value})
            await state.clear()
            await msg.answer(text, reply_markup=settings_kb())
        except (ValueError, TypeError):
            await msg.answer('قيمة غير صالحة؛ أدخل رقماً ضمن النطاق أو /cancel.')
        except Exception as e:
            await msg.answer('تعذّر الحفظ: '+type(e).__name__)
    return dp
