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
from work_cycle import cycle_text
from account_registry import account_label,registry_text,credential_key,safe_name
from account_details import positive_uid


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
    account_uid = State()
    account_name = State()
    url = State()
    number = State()
    limit = State()
    recovery_number = State()
    payout_number = State()
    cycle_number = State()


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=text, callback_data=value) for text, value in row] for row in rows])


def home():
    return kb([[('▶️ بدء التداول', 'start'), ('⏹ إيقاف التداول', 'stop')],
               [('📊 الإحصائيات', 'stats'), ('🔄 الحالة', 'status')],
               [('⚙️ الإعدادات', 'settings'), ('👤 إدارة الحسابات', 'accounts')],
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
               [('📉 حماية عائد الزوج', 'payout'), ('⏱ التشغيل والراحة', 'cycle')],
               [('⚡ التوقيتات المتقدمة', 'timing')],
               [('👤 إدارة الحسابات', 'accounts'), ('🏠 الرئيسية', 'home')]])


def payout_kb(doc=None):
    selected=doc['payout']['mode'] if doc else None
    mark=lambda key: '✅ ' if selected==key else ''
    threshold=f" {doc['payout']['threshold']:g}%" if doc else ''
    minimum=f" {doc['payout']['switch_min']:g}%" if doc else ''
    return kb([[(mark('OFF')+'تعطيل الحماية', 'payout:mode:OFF')],
               [(mark('STOP')+'إيقاف عند الحد', 'payout:mode:STOP'),
                (mark('SWITCH')+'تبديل الزوج', 'payout:mode:SWITCH')],
               [('📉 حد الإجراء ≤'+threshold, 'payout_threshold'), ('🎯 عائد البديل ≥'+minimum, 'payout_min')],
               [('📡 جلب العائد من المنصة', 'payout_refresh')],
               [('🧪 تشخيص القراءة', 'payout_diagnostic')],
               [('⬅️ الإعدادات', 'settings')]])


def cycle_kb():
    return kb([[('◻️ بدون وقت', 'cycle:mode:OFF')],
               [('⏹ توقف بعد المدة', 'cycle:mode:STOP'), ('🔁 تشغيل وراحة', 'cycle:mode:REPEAT')],
               [('▶️ مدة التشغيل', 'cycle_run'), ('☕ مدة الراحة', 'cycle_rest')],
               [('🪜 أكمل السلسلة', 'cycle:finish:LADDER'), ('📍 الصفقة فقط', 'cycle:finish:ORDER')],
               [('🛡 حدود الربح والخسارة', 'limits')],
               [('🔄 الحالة', 'cycle'), ('⬅️ الإعدادات', 'settings')]])


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
               [('⏱ وقت التشغيل والراحة', 'cycle')],
               [('⬅️ الإعدادات', 'settings')]])


def limits_text(doc):
    p=doc['risk'];s=doc['account_stats'][doc['account_mode']]
    return (f"🛡 حدود الربح والخسارة\nالوضع: {'بحدود' if p['mode']=='LIMITED' else 'تقليدي'}\n"
            f"🎯 حد الربح: +${p['profit_limit']:g}\n🛑 حد الخسارة: -${p['loss_limit']:g}\n"
            f"الصافي الحالي: ${s['net_pnl']:+.2f}\nأول حد مالي متحقق يمنع الدخول التالي.")


def accounts_kb(doc=None):
    return kb([[('📋 عرض الحسابات واختيار حساب','account:list:0')],
               [('➕ إضافة حساب','account:add')],
               [('🔎 الحساب النشط','account:active')],
               [('⬅️ الإعدادات','settings'),('🏠 الرئيسية','home')]])


def account_list_kb(doc,page=0):
    records=sorted(doc.get('accounts',{}).items(),key=lambda item:(item[1]['mode'],int(item[1]['uid'])))
    pages=max(1,math.ceil(len(records)/8));page=max(0,min(page,pages-1))
    rows=[[(account_label(record,key==doc.get('active_account_id')),f'account:pick:{key}')]
          for key,record in records[page*8:page*8+8]]
    if pages>1:
        rows.append([('◀️',f'account:list:{max(0,page-1)}'),(f'{page+1}/{pages}','noop'),('▶️',f'account:list:{min(pages-1,page+1)}')])
    return kb(rows+[[('➕ إضافة حساب','account:add')],[('⬅️ إدارة الحسابات','accounts')]])


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
                                   'لكل الحسابات المحفوظة. هل أنت متأكد؟',
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
                        +cycle_text(repo.doc)+'\n'+

                        f"WebSocket: {'متصل' if worker.po and worker.po.connected else 'غير متصل'}\n"
                        f"MongoDB writer: {'فعال' if repo.leader else 'غير فعال'}\n"
                        f"الزوج: {repo.doc['trading']['asset']}\n"
                        f"آخر كود خطأ: {worker.last_error or 'لا يوجد'}\n"
                        'البيانات السرية لا تظهر في اللوحة.')
                if worker.diagnostic:
                    text += '\n\n' + worker.diagnostic.public_text()
                rows = [[('🔍 تحقق من الصفقة المعلّقة', 'settle_review')], [('🗃 مراجعة وأرشفة يدوية', 'review')], [('⬅️ الرئيسية', 'home')]] if worker.pending else None
                return await panel(bot, cb, text, kb(rows) if rows else home())
            if action == 'payout_refresh':
                text=await worker.refresh_payout_ui()
                return await panel(bot,cb,text,payout_kb(repo.doc))
            if action == 'payout_diagnostic':
                return await panel(bot,cb,worker.payout_status()+'\n\n'+worker.payout_diagnostics(),payout_kb(repo.doc))
            if action == 'cycle':
                return await panel(bot,cb,cycle_text(repo.doc),cycle_kb())
            if action.startswith('cycle:finish:'):
                value=action.split(':')[2]
                if value not in ('ORDER','LADDER'):raise ValueError('قاعدة وقت غير صالحة')
                await worker.change({'schedule.finish':value})
                return await panel(bot,cb,cycle_text(repo.doc),cycle_kb())
            if action.startswith('cycle:mode:'):
                value=action.split(':')[2]
                if value not in ('OFF','STOP','REPEAT'):raise ValueError('وضع وقت غير صالح')
                await worker.change({'schedule.mode':value})
                return await panel(bot,cb,cycle_text(repo.doc),cycle_kb())
            if action in ('cycle_run','cycle_rest'):
                key='run_minutes' if action=='cycle_run' else 'rest_minutes'
                return await panel(bot,cb,'اختر المدة بالدقائق',kb([
                    [(f'{v} د',f'cycle:value:{key}:{v}') for v in (5,10,15)],
                    [(f'{v} د',f'cycle:value:{key}:{v}') for v in (30,60)],
                    [('✍️ مدة مخصصة','cycle:edit:'+key)],[('⬅️ التشغيل والراحة','cycle')]]))
            if action.startswith('cycle:value:'):
                _,_,key,value=action.split(':')
                if key not in ('run_minutes','rest_minutes'):raise ValueError('مدة غير صالحة')
                await worker.change({'schedule.'+key:valid_number(value,.01,10080)})
                return await panel(bot,cb,cycle_text(repo.doc),cycle_kb())
            if action.startswith('cycle:edit:'):
                key=action.split(':')[2]
                if key not in ('run_minutes','rest_minutes'):raise ValueError('مدة غير صالحة')
                await state.set_state(Input.cycle_number)
                await state.update_data(key=key)
                return await panel(bot,cb,'أرسل المدة بالدقائق، مثال 10. /cancel للإلغاء.',None)
            if action == 'payout':
                return await panel(bot, cb, worker.payout_status(), payout_kb(repo.doc))
            if action.startswith('payout:mode:'):
                value=action.split(':')[2]
                if value not in ('OFF','STOP','SWITCH'): raise ValueError('وضع حماية غير صالح')
                await worker.change({'payout.mode':value})
                return await panel(bot, cb, worker.payout_status(), payout_kb(repo.doc))
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
                return await panel(bot, cb, worker.payout_status(), payout_kb(repo.doc))
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
                return await panel(bot, cb, registry_text(repo.doc,worker.po), accounts_kb(repo.doc))
            if action == 'account:active':
                return await panel(bot,cb,account_text(repo.doc,worker.po),accounts_kb(repo.doc))
            if action.startswith('account:list:'):
                page=int(action.rsplit(':',1)[1])
                return await panel(bot,cb,'📋 الحسابات المحفوظة\nUID · النوع · الاسم\n✅ الحساب النشط',account_list_kb(repo.doc,page))
            if action == 'account:add':
                return await panel(bot,cb,'➕ إضافة حساب — اختر النوع',kb([
                    [('🟡 تجريبي PRACTICE','account:add:PRACTICE')],
                    [('🟢 حقيقي REAL','account:add:REAL')],[('⬅️ إدارة الحسابات','accounts')]]))
            if action.startswith('account:add:'):
                mode=action.rsplit(':',1)[1]
                if mode not in MODES:raise ValueError('نوع غير صالح')
                await state.set_state(Input.credential)
                await state.update_data(mode=mode,new_account=True)
                return await panel(bot,cb,'أرسل إطار auth الكامل أو SSID. إن أرسلت SSID خاماً سنطلب UID.\nسنحاول حذف رسالة الجلسة؛ لا ترسل كلمة المرور. /cancel للإلغاء',None)
            if action.startswith('account:pick:'):
                key=action.split(':',2)[2];record=repo.doc.get('accounts',{}).get(key)
                if not record:raise ValueError('الحساب غير موجود')
                warning='⚠️ حساب حقيقي — يستخدم أموالاً حقيقية.\n' if record['mode']=='REAL' else ''
                return await panel(bot,cb,warning+account_label(record)+'\nتأكيد اختيار الحساب؟ سيتوقف التداول عند التبديل.',kb([
                    [('✅ اختيار هذا الحساب','account:select:'+key)],[('إلغاء','account:list:0')]]))
            if action.startswith('account:select:'):
                key=action.split(':',2)[2]
                text=await worker.select_account(key)
                return await panel(bot,cb,text+'\n\n'+registry_text(repo.doc,worker.po),accounts_kb(repo.doc))
            if action == 'settle_review':
                text=await worker.reconcile_pending()
                return await panel(bot,cb,text,kb([[('🔄 تحقق مجدداً','settle_review')],[('🔄 الإحصائيات','stats')],[('⬅️ الرئيسية','home')]]))
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
                mode=action.split(':')[1]
                if mode not in MODES:raise ValueError('حساب غير صالح')
                key=repo.doc.get('active_accounts',{}).get(mode)
                if not key:raise ValueError('أضف حساباً محفوظاً من إدارة الحسابات أولاً')
                text=await worker.select_account(key)
                return await panel(bot,cb,text+'\n'+registry_text(repo.doc,worker.po),accounts_kb(repo.doc))
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
    async def credential(msg: Message,state: FSMContext):
        data=await state.get_data();raw=(msg.text or '').strip()
        with suppress(Exception):await msg.delete()
        try:
            mode=data['mode']
            if not raw.startswith('42'):
                if not raw or len(raw)>20000:raise ValueError('SSID غير صالح')
                await state.update_data(raw_ssid=raw)
                await state.set_state(Input.account_uid)
                return await ui.show('أرسل UID الرقمي الفعلي لهذا الحساب، وليس SSID آخر. /cancel للإلغاء',view='accounts')
            frame=auth_frame(raw,mode)
            await state.update_data(frame=frame)
            await state.set_state(Input.url)
            await ui.show('تم استلام الجلسة دون عرضها. أرسل رابط wss أو /skip للرابط الافتراضي. /cancel للإلغاء',view='accounts')
        except (ValueError,KeyError) as exc:
            await ui.show(str(exc) if isinstance(exc,ValueError) else 'أعد فتح إدارة الحسابات.',accounts_kb(repo.doc),view='accounts')

    @dp.message(Input.account_uid)
    async def account_uid(msg: Message,state: FSMContext):
        data=await state.get_data();uid=positive_uid(msg.text)
        if not uid:return await ui.show('UID يجب أن يكون رقماً موجباً فعلياً للحساب.',view='accounts')
        try:
            frame=auth_frame(data['raw_ssid'],data['mode'],int(uid))
            await state.update_data(frame=frame,raw_ssid='')
            await state.set_state(Input.url)
            await ui.show('أرسل رابط wss أو /skip للرابط الافتراضي. /cancel للإلغاء',view='accounts')
        except (ValueError,KeyError):await ui.show('أعد إضافة بيانات الجلسة من إدارة الحسابات.',accounts_kb(repo.doc),view='accounts')

    @dp.message(Input.url)
    async def url(msg: Message,state: FSMContext):
        data=await state.get_data()
        with suppress(Exception):await msg.delete()
        try:
            mode=data['mode']
            value=(config.REAL_URL if mode=='REAL' else config.DEMO_URL) if msg.text=='/skip' else valid_url(msg.text)
            await state.update_data(ws_url=value)
            await state.set_state(Input.account_name)
            await ui.show('أرسل اسماً مختصراً للحساب أو /skip لعرض UID والنوع فقط. /cancel للإلغاء',view='accounts')
        except (ValueError,KeyError):await ui.show('رابط غير صالح؛ أرسل رابط wss الخاص بالمنصة أو /skip.',view='accounts')

    @dp.message(Input.account_name)
    async def account_name(msg: Message,state: FSMContext):
        data=await state.get_data()
        try:
            name='' if msg.text=='/skip' else safe_name(msg.text)
            credentials=dict(ssid=data['frame'],ws_url=data['ws_url'])
            key=await worker.add_account(credentials,data['mode'],name)
            await state.clear()
            await ui.show('✅ تم حفظ الحساب: '+account_label(repo.doc['accounts'][key])+'\nلم يبدأ التداول ولم يُبدّل الحساب النشط تلقائياً.',account_list_kb(repo.doc),view='accounts')
        except ValueError as exc:await ui.show(str(exc),accounts_kb(repo.doc),view='accounts')
        except Exception:
            await state.clear()
            await ui.show('تعذّر تأكيد حفظ الحساب؛ أعد فتح القائمة للتحقق.',accounts_kb(repo.doc),view='accounts')

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

    @dp.message(Input.cycle_number)
    async def cycle_input(msg: Message,state: FSMContext):
        try:
            key=(await state.get_data()).get('key')
            if key not in ('run_minutes','rest_minutes'):raise ValueError('مدة غير صالحة')
            await worker.change({'schedule.'+key:valid_number(msg.text or '',.01,10080)})
            await state.clear()
            await ui.show(cycle_text(repo.doc),cycle_kb(),view='cycle')
        except (ValueError,TypeError):
            await ui.show('أرسل مدة موجبة بالدقائق أو /cancel.',view='cycle')
        except Exception as exc:
            await ui.show('تعذّر الحفظ: '+type(exc).__name__,cycle_kb(),view='cycle')

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
