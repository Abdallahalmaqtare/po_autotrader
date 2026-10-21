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
from account_details import account_text
from risk_limits import risk_summary
from branded_ui import get_panels
from recovery import recovery_text, short_summary
from compact_stats import compact_report


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
    limit = State()
    recovery_number = State()
    payout_number = State()


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data=value) for text, value in row] for row in rows])


def home():
    return kb([[('▶️ بدء التداول', 'start'), ('⏹ إيقاف التداول', 'stop')],
               [('📊 الإحصائيات', 'stats'), ('🔄 الحالة', 'status')],
               [('⚙️ الإعدادات', 'settings'), ('👤 الحسابات', 'accounts')],
               [('🛟 التعزيز', 'recovery'), ('🛡 الحدود', 'limits')],
               [('🧰 الأدوات والصيانة', 'tools')]])


def stats_kb():
    return kb([[('🔄 تحديث', 'stats'), ('🗑 تصفير', 'reset_stats')],
               [('🏠 الرئيسية', 'home')]])


def tools_kb():
    return kb([[('🧪 فحص الاتصال', 'diagnose')], [('🗑 تصفير السجل', 'reset_stats')],
               [('🏠 الرئيسية', 'home')]])


def settings_kb():
    return kb([[('💱 زوج التداول', 'assets:0'), ('⏱ المدة', 'duration')],
               [('💵 المبلغ الأساسي', 'amount'), ('🪜 مستويات التدبيل', 'mg')],
               [('🛟 التعزيز', 'recovery'), ('🛡 حدود الربح والخسارة', 'limits')],
               [('📉 حماية عائد الزوج', 'payout')],
               [('⚡ التوقيتات المتقدمة', 'timing')],
               [('👤 إدارة الحسابات', 'accounts'), ('🏠 الرئيسية', 'home')]])


def payout_kb():
    return kb([[('◻️ تعطيل', 'payout:mode:OFF')],
               [('⏹ إيقاف عند الانخفاض', 'payout:mode:STOP')],
               [('🔄 تغيير الزوج تلقائياً', 'payout:mode:SWITCH')],
               [('📉 حد الإجراء', 'payout_threshold'), ('🎯 حد الزوج البديل', 'payout_min')],
               [('🔄 تحديث النسبة', 'payout'), ('⬅️ الإعدادات', 'settings')]])


def timing_kb():
    return kb([[('⚡ تأخير الدخول', 'number:entry_delay_seconds'),
                ('⏳ فاصل الدورات', 'number:loop_delay_seconds')],
               [('⌛ مهلة النتيجة', 'number:result_timeout_extra'),
                ('💰 انتظار الرصيد', 'number:balance_settle_seconds')],
               [('🔍 إعادة فحص الخسارة', 'number:loss_recheck_seconds')],
               [('⬅️ الإعدادات', 'settings')]])


def recovery_kb():
    return kb([[('◻️ تقليدي', 'recovery:mode:TRADITIONAL'),
                ('🛟 تفعيل التعزيز', 'recovery:mode:ENABLED')],
               [('💵 زيادة الأساس', 'recovery_increment'), ('🎯 طريقة الرجوع', 'recovery_exit')],
               [('🏆 عدد الانتصارات', 'recovery_wins')],
               [('⬅️ الإعدادات', 'settings'), ('🏠 الرئيسية', 'home')]])


def limits_kb():
    return kb([[('♾ تقليدي — دون حدود', 'risk:TRADITIONAL')],
               [('🛡 تشغيل بحدود', 'risk:LIMITED')],
               [('🎯 حد الربح', 'limit:profit_limit'), ('🛑 حد الخسارة', 'limit:loss_limit')],
               [('⬅️ الإعدادات', 'settings')]])


def limits_text(doc):
    return (risk_summary(doc) + '\n'
            'اختر الحدود وعدّل المبالغ. التفعيل لا يصفّر الأرباح السابقة؛ '
            'تُستخدم نفس أرقام التقرير المحفوظ لهذا الحساب. '
            'إذا كان الحد متحققاً بالفعل يتوقف/يُمنع Start. '
            'لبداية من الصفر استخدم تصفير السجل (يصفّر الحسابين).\n'
            'التوقف يمنع الدخول الجديد بعد حسم الصفقة؛ مبلغ الصفقة الأخيرة قد يتجاوز الحد.')


def accounts_kb():
    return kb([[('🟢 التحويل للحقيقي | Switch to Real', 'real_confirm')],
               [('🟡 التحويل للتجريبي | Switch to Demo', 'mode:PRACTICE')],
               [('✏️ تحديث بيانات الحقيقي', 'credentials:REAL')],
               [('✏️ تحديث بيانات الديمو', 'credentials:PRACTICE')],
               [('⬅️ الإعدادات', 'settings')]])


def assets_kb(page):
    total = math.ceil(len(FOREX_OTC)/6)
    page = max(0, min(page, total-1))
    entries = FOREX_OTC[page*6:page*6+6]
    rows = [[(name, 'asset:'+symbol) for name, symbol in entries[i:i+2]]
            for i in range(0, len(entries), 2)]
    rows += [[('<' if page > 0 else '·', f'assets:{page-1}' if page > 0 else 'noop'),
              (f'{page+1}/{total}', 'noop'),
              ('>' if page < total-1 else '·', f'assets:{page+1}' if page < total-1 else 'noop')],
             [('Back', 'settings')]]
    return kb(rows)


def report(repo, worker):
    return compact_report(repo, worker)


async def panel(bot, cb, text, markup):
    return await get_panels(bot).show(text, markup, view=cb.data or 'home', origin=cb.message)


def build_dispatcher(repo, worker, bot):
    dp = Dispatcher()
    ui = get_panels(bot)
    async def notify_diagnostic(text):
        await ui.notice('🔔 إشعار البوت\n' + text)
    worker.notify = notify_diagnostic
    dp.message.outer_middleware(AdminOnly())
    dp.callback_query.outer_middleware(AdminOnly())

    @dp.message(CommandStart())
    async def start_panel(msg: Message, state: FSMContext):
        await state.clear()
        await ui.show('🎛 لوحة تحكم عبود Trading Controller\nلا يبدأ التداول تلقائياً بعد الإقلاع.', home(), view='home')

    @dp.message(Command('cancel'))
    async def cancel(msg: Message, state: FSMContext):
        await state.clear()
        await ui.show('أُلغي الإدخال.', settings_kb(), view='settings')

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
                worker.stage = 'stopped'
                return await panel(bot, cb, '✅ تم تصفير السجل بالكامل. التداول متوقف — اضغط Start للبدء من جديد.',
                                   home())
            if action == 'stats':
                return await panel(bot, cb, report(repo, worker), stats_kb())
            if action == 'diagnose':
                text = await worker.diagnose()
                return await panel(bot, cb, text, home())
            if action == 'status':
                text = (account_text(repo.doc, worker.po) + risk_summary(repo.doc) +
                        f"المرحلة: {worker.stage}\n"
                        f"WebSocket: {'متصل' if worker.po and worker.po.connected else 'غير متصل'}\n"
                        f"MongoDB writer: {'فعال' if repo.leader else 'غير فعال'}\n"
                        f"الزوج: {repo.doc['trading']['asset']}\n"
                        f"آخر كود خطأ: {worker.last_error or 'لا يوجد'}\n"
                        'البيانات السرية لا تظهر في اللوحة.')
                if worker.diagnostic:
                    text += '\n\n' + worker.diagnostic.public_text()
                rows = [[('🔍 مراجعة الأمر المعلّق', 'review')], [('⬅️ الرئيسية', 'home')]] if worker.pending else None
                return await panel(bot, cb, text, kb(rows) if rows else home())
            if action == 'payout':
                return await panel(bot, cb, worker.payout_status(), payout_kb())
            if action.startswith('payout:mode:'):
                value=action.split(':')[2]
                if value not in ('OFF','STOP','SWITCH'): raise ValueError('وضع حماية غير صالح')
                await worker.change({'payout.mode':value})
                return await panel(bot, cb, worker.payout_status(), payout_kb())
            if action in ('payout_threshold','payout_min'):
                key='threshold' if action=='payout_threshold' else 'switch_min'
                values=(70,80,84,85,90) if key=='threshold' else (85,90,92,95)
                rows=[[(f'{value}%',f'payout:value:{key}:{value}') for value in values[i:i+3]]
                      for i in range(0,len(values),3)]
                rows += [[('✍️ نسبة مخصصة','payout:edit:'+key)],[('⬅️ حماية العائد','payout')]]
                return await panel(bot, cb, 'الإجراء عند النسبة أو أقل' if key=='threshold' else 'أقل عائد للزوج البديل',kb(rows))
            if action.startswith('payout:value:'):
                _,_,key,raw=action.split(':')
                if key not in ('threshold','switch_min'): raise ValueError('خيار نسبة غير صالح')
                await worker.change({'payout.'+key:valid_number(raw,0,100)})
                return await panel(bot, cb, worker.payout_status(), payout_kb())
            if action.startswith('payout:edit:'):
                key=action.split(':')[2]
                if key not in ('threshold','switch_min'): raise ValueError('خيار نسبة غير صالح')
                await state.set_state(Input.payout_number)
                await state.update_data(key=key)
                return await panel(bot,cb,'أرسل النسبة بين 0 و100، مثال 84. /cancel للإلغاء.',None)
            if action == 'tools':
                return await panel(bot, cb, '🧰 الأدوات والصيانة', tools_kb())
            if action == 'timing':
                return await panel(bot, cb, '⚡ التوقيتات المتقدمة', timing_kb())
            if action == 'recovery':
                return await panel(bot, cb, recovery_text(repo.doc), recovery_kb())
            if action.startswith('recovery:mode:'):
                mode = action.split(':')[2]
                if mode not in ('TRADITIONAL', 'ENABLED'):
                    raise ValueError('وضع تعزيز غير صالح')
                await worker.change({'recovery.mode': mode})
                return await panel(bot, cb, recovery_text(repo.doc), recovery_kb())
            if action == 'recovery_increment':
                return await panel(bot, cb, '💵 الزيادة فوق المبلغ الأساسي', kb([
                    [(f'+${v}', f'recovery:value:increment:{v}') for v in (1,2,3)],
                    [(f'+${v}', f'recovery:value:increment:{v}') for v in (4,5)],
                    [('✍️ زيادة مخصصة', 'recovery:edit:increment')], [('⬅️ التعزيز', 'recovery')]]))
            if action == 'recovery_exit':
                return await panel(bot, cb, '🎯 الرجوع للمبلغ الأساسي بعد:', kb([
                    [('💰 تعويض صافي الخسارة', 'recovery:exit:RECOVER_LOSS')],
                    [('🏆 عدد انتصارات محدد', 'recovery:exit:WINS')], [('⬅️ التعزيز', 'recovery')]]))
            if action.startswith('recovery:exit:'):
                value = action.split(':')[2]
                if value not in ('RECOVER_LOSS', 'WINS'):
                    raise ValueError('طريقة رجوع غير صالحة')
                await worker.change({'recovery.exit_mode': value})
                return await panel(bot, cb, recovery_text(repo.doc), recovery_kb())
            if action == 'recovery_wins':
                return await panel(bot, cb, '🏆 عدد الصفقات الرابحة أثناء التعزيز', kb([
                    [(str(v), f'recovery:value:win_target:{v}') for v in (1,2,3)],
                    [(str(v), f'recovery:value:win_target:{v}') for v in (4,5)],
                    [('✍️ عدد مخصص', 'recovery:edit:win_target')], [('⬅️ التعزيز', 'recovery')]]))
            if action.startswith('recovery:value:'):
                _, _, key, raw = action.split(':')
                if key not in ('increment', 'win_target'):
                    raise ValueError('خيار تعزيز غير صالح')
                value = int(raw) if key == 'win_target' else valid_number(raw, .01, 100000)
                await worker.change({'recovery.'+key: value})
                return await panel(bot, cb, recovery_text(repo.doc), recovery_kb())
            if action.startswith('recovery:edit:'):
                key = action.split(':')[2]
                if key not in ('increment', 'win_target'):
                    raise ValueError('خيار تعزيز غير صالح')
                await state.set_state(Input.recovery_number)
                await state.update_data(key=key)
                prompt = 'أرسل عدد الانتصارات (1–1000).' if key == 'win_target' else 'أرسل الزيادة بالدولار فوق المبلغ الأساسي.'
                return await panel(bot, cb, prompt + '\n/cancel للإلغاء', None)
            if action == 'settings':
                return await panel(bot, cb, '⚙️ الإعدادات؛ التغييرات العادية تسري من الدورة القادمة.', settings_kb())
            if action == 'accounts':
                return await panel(bot, cb, account_text(repo.doc, worker.po) + 'لا تُعرض قيم SSID.', accounts_kb())
            if action == 'limits':
                return await panel(bot, cb, limits_text(repo.doc), limits_kb())
            if action.startswith('risk:'):
                selected = action.split(':', 1)[1]
                if selected not in ('TRADITIONAL', 'LIMITED'):
                    raise ValueError('وضع تشغيل غير صالح')
                text = await worker.change({'risk.mode': selected})
                return await panel(bot, cb, text + '\n\n' + limits_text(repo.doc), limits_kb())
            if action.startswith('limit:'):
                key = action.split(':', 1)[1]
                if key not in ('profit_limit', 'loss_limit'):
                    raise ValueError('حد غير صالح')
                await state.set_state(Input.limit)
                await state.update_data(key=key)
                label = 'الربح' if key == 'profit_limit' else 'الخسارة'
                return await panel(bot, cb, f'أرسل حد {label} بالدولار كعدد موجب، مثال 50. /cancel للإلغاء.', None)
            if action == 'real_confirm':
                return await panel(bot, cb, '⚠️ الحساب الحقيقي يستخدم أموالاً حقيقية. تأكيد التبديل؟',
                                   kb([[('تأكيد REAL', 'mode:REAL')], [('إلغاء', 'accounts')]]))
            if action.startswith('mode:'):
                mode = action.split(':')[1]
                if mode not in MODES:
                    raise ValueError('حساب غير صالح')
                text = await worker.change({'account_mode': mode}, reconnect=True)
                return await panel(bot, cb, text + '\n' + account_text(repo.doc, worker.po), accounts_kb())
            if action.startswith('credentials:'):
                mode = action.split(':')[1]
                if mode not in MODES:
                    raise ValueError('حساب غير صالح')
                await state.set_state(Input.credential)
                await state.update_data(mode=mode)
                return await panel(bot, cb, f'أرسل إطار auth الكامل أو SSID لـ {mode}.\n'
                                   'سنحاول حذف رسالتك؛ لا تُرسل كلمة مرور حسابك. /cancel للإلغاء', None)
            if action.startswith('assets:'):
                page = max(0, min(4, int(action.split(':')[1])))
                return await panel(bot, cb, f'Select an asset\nMarket: OTC\n\nChoose an asset:\n💱 الأزواج مطابقة للصور — الصفحة {page+1}/5', assets_kb(page))
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
            await ui.notice(str(e), settings_kb(), view='error')
        except Exception as e:
            # Never echo raw exceptions containing Mongo URI/session/WS payloads.
            await ui.notice(f'تعذّرت العملية ({type(e).__name__}). لم يُؤكّد نجاح التغيير؛ راجع الحالة.', home(), view='error')

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
            await ui.show(str(e) if isinstance(e, ValueError) else 'أعد فتح إدارة الحسابات.', accounts_kb(), view='accounts')
            return
        await state.update_data(frame=frame)
        await state.set_state(Input.url)
        await ui.show('تم استلام الجلسة دون عرضها. أرسل رابط wss أو /skip لاعتماد الرابط الافتراضي لهذا الحساب.\n/cancel للإلغاء', view='accounts')

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
            await ui.show(text + '\n' + account_text(repo.doc, worker.po), accounts_kb(), view='accounts')
        except ValueError as e:
            await ui.show(str(e), view='accounts')
        except Exception as e:
            await state.clear()
            await ui.show('لم يُؤكّد حفظ البيانات؛ حاول مجدداً. نوع الخطأ: '+type(e).__name__, accounts_kb(), view='accounts')

    @dp.message(Input.limit)
    async def limit_input(msg: Message, state: FSMContext):
        try:
            data = await state.get_data()
            key = data.get('key')
            if key not in ('profit_limit', 'loss_limit'):
                raise ValueError('حد غير صالح')
            value = valid_number(msg.text or '', .01, 1000000)
            text = await worker.change({'risk.' + key: value})
            await state.clear()
            await ui.show(text + '\n\n' + limits_text(repo.doc), limits_kb(), view='limits')
        except (ValueError, TypeError):
            await ui.show('أدخل مبلغاً موجباً بين 0.01 و1000000 أو /cancel.', view='limits')
        except Exception as exc:
            await ui.show('لم يُؤكد الحفظ: ' + type(exc).__name__, limits_kb(), view='limits')

    @dp.message(Input.recovery_number)
    async def recovery_input(msg: Message, state: FSMContext):
        try:
            data = await state.get_data()
            key = data.get('key')
            if key not in ('increment', 'win_target'):
                raise ValueError('خيار تعزيز غير صالح')
            value = valid_number(msg.text or '', 1 if key == 'win_target' else .01,
                                 1000 if key == 'win_target' else 100000)
            if key == 'win_target':
                if not value.is_integer():
                    raise ValueError('عدد الانتصارات يجب أن يكون صحيحاً')
                value = int(value)
            await worker.change({'recovery.'+key: value})
            await state.clear()
            await ui.show(recovery_text(repo.doc), recovery_kb(), view='recovery')
        except (ValueError, TypeError):
            await ui.show('قيمة غير صالحة؛ أرسل رقماً ضمن النطاق أو /cancel.', view='recovery')
        except Exception as exc:
            await ui.show('تعذّر الحفظ: '+type(exc).__name__, recovery_kb(), view='recovery')

    @dp.message(Input.payout_number)
    async def payout_input(msg: Message, state: FSMContext):
        try:
            key=(await state.get_data()).get('key')
            if key not in ('threshold','switch_min'): raise ValueError('خيار نسبة غير صالح')
            await worker.change({'payout.'+key:valid_number((msg.text or '').strip().rstrip('%'),0,100)})
            await state.clear()
            await ui.show(worker.payout_status(),payout_kb(),view='payout')
        except (ValueError,TypeError):
            await ui.show('نسبة غير صالحة؛ البديل يجب أن يتجاوز حد الإجراء. /cancel للإلغاء.',view='payout')
        except Exception as exc:
            await ui.show('تعذّر الحفظ: '+type(exc).__name__,payout_kb(),view='payout')

    @dp.message(Input.number)
    async def number_input(msg: Message, state: FSMContext):
        try:
            data = await state.get_data()
            value = valid_number(msg.text or '', .01 if data['key'] == 'trade_amount' else 0,
                                 100000 if data['key'] == 'trade_amount' else 120)
            text = await worker.change({'trading.'+data['key']: value})
            await state.clear()
            await ui.show(text, settings_kb(), view='setup')
        except (ValueError, TypeError):
            await ui.show('قيمة غير صالحة؛ أدخل رقماً ضمن النطاق أو /cancel.', view='setup')
        except Exception as e:
            await ui.show('تعذّر الحفظ: '+type(e).__name__, settings_kb(), view='setup')
    return dp
