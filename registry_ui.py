"""Admin-only registry menus/input. Called behind the existing AdminOnly middleware."""
from contextlib import suppress
from aiogram.fsm.state import State,StatesGroup
from aiogram.types import InlineKeyboardMarkup,InlineKeyboardButton
from profile_registry import selected_source,active_profile_id,source_spec
from account_details import credential_uid
from cloud_settings import auth_frame,valid_url
import config


class RegistryInput(StatesGroup):
    source=State()
    source_button=State()
    account_name=State()
    account_auth=State()
    account_url=State()
    rename=State()


def keyboard(rows):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t,callback_data=v) for t,v in row] for row in rows])


def source_text(doc):
    s=selected_source(doc)
    return (f"📡 مصادر الإشارات\nالنشط: {s['name']} — @{s['username']}\n"
            f"الإعداد: {'تلقائي' if s['setup_mode']=='AUTO' else 'يدوي'} | الزر: {s['button_text']}\n"
            'مصدر واحد نشط في كل مرة. التبديل يوقف التداول؛ Start للاستئناف.')


def sources_kb(doc,page=0):
    items=list(doc['signal_sources'].items());pages=max(1,(len(items)+5)//6)
    page=max(0,min(page,pages-1))
    rows=[[(('✅ ' if key==doc['active_signal_source'] else '')+s['name'], 'src:open:'+key)]
          for key,s in items[page*6:page*6+6]]
    rows += [[('‹',f'src:page:{max(0,page-1)}'),(f'{page+1}/{pages}','noop'),('›',f'src:page:{min(pages-1,page+1)}')],
             [('➕ إضافة بوت إشارات','src:add')],[('⬅️ الإعدادات','settings')]]
    return keyboard(rows)


def source_detail(doc,key):
    s=doc['signal_sources'][key]
    text=(f"📡 {s['name']}\n@{s['username']}\nالإعداد: {s['setup_mode']}\nزر الإشارة: {s['button_text']}")
    return text,keyboard([[('✅ استخدام هذا المصدر','src:select:'+key)],
        [('⚙️ إعداد تلقائي','src:auto:'+key),('✋ إعداد يدوي','src:manual:'+key)],
        [('✏️ اسم زر الإشارة','src:button:'+key),('🗑 حذف','src:delete:'+key)],
        [('⬅️ المصادر','sources')]])


def accounts_list_text(doc):
    key=active_profile_id(doc)
    name=doc['broker_profiles'][key]['name']
    return f'👥 حسابات Pocket Option\nالنشط: {name} | {doc["account_mode"]}\nاختر حساباً لعرض UID وإدارته. التداول بحساب واحد فقط.'


def accounts_list_kb(doc,page=0):
    items=list(doc['broker_profiles'].items());pages=max(1,(len(items)+5)//6)
    page=max(0,min(page,pages-1))
    rows=[]
    for key,p in items[page*6:page*6+6]:
        uid=credential_uid(p['credentials'],p['mode']) or '—'
        mark='✅ ' if key==active_profile_id(doc) else ''
        rows.append([(f"{mark}{p['name'][:18]} | {p['mode']} | {uid}",'acct:open:'+key)])
    rows += [[('‹',f'acct:page:{max(0,page-1)}'),(f'{page+1}/{pages}','noop'),('›',f'acct:page:{min(pages-1,page+1)}')],
             [('➕ حساب حقيقي','acct:add:REAL'),('➕ حساب تجريبي','acct:add:PRACTICE')],
             [('⬅️ الإعدادات','settings')]]
    return keyboard(rows)


def account_detail(doc,key):
    p=doc['broker_profiles'][key];uid=credential_uid(p['credentials'],p['mode']) or '—'
    text=f"👤 {p['name']}\nالنوع: {p['mode']}\nUID: {uid}\nالصافي: ${p['stats']['net_pnl']:+.2f}"
    return text,keyboard([[('✅ اختيار الحساب','acct:select:'+key)],
        [('🔑 تحديث الجلسة','acct:auth:'+key),('✏️ الاسم','acct:rename:'+key)],
        [('🗑 حذف الحساب','acct:delete:'+key)],[('⬅️ الحسابات','account_profiles')]])


def install_registry_inputs(dp,worker,repo,ui):
    @dp.message(RegistryInput.source)
    async def registry_source(msg,state):
        try:
            # username or `name | @username`; new source defaults to MANUAL.
            parts=(msg.text or '').split('|',1)
            spec=source_spec(dict(username=parts[-1].strip(),name=parts[0].strip().lstrip('@') if len(parts)>1 else None,
                                  setup_mode='MANUAL',button_text='New signal'))
            key=await worker.source_action('add',value=spec)
            await state.clear()
            text,kb=source_detail(repo.doc,key)
            await ui.show('تمت الإضافة؛ لم يتغيّر المصدر النشط.\n'+text,kb,view='sources')
        except (ValueError,KeyError):
            await ui.show('أرسل @username أو الاسم | @username الصحيح. /cancel للإلغاء',view='sources')
        except Exception as exc:
            await ui.show('لم يُؤكد الحفظ: '+type(exc).__name__,view='sources')

    @dp.message(RegistryInput.source_button)
    async def registry_button(msg,state):
        try:
            key=(await state.get_data())['key']
            await worker.source_action('edit',key,{'button_text':msg.text or ''})
            await state.clear();text,kb=source_detail(repo.doc,key)
            await ui.show(text,kb,view='sources')
        except ValueError as exc:
            await ui.show(str(exc),view='sources')
        except Exception as exc:
            await ui.show('لم يُؤكد الحفظ: '+type(exc).__name__,view='sources')

    @dp.message(RegistryInput.account_name)
    async def registry_account_name(msg,state):
        from profile_registry import safe_name
        try:
            name=safe_name(msg.text)
            await state.update_data(name=name)
            await state.set_state(RegistryInput.account_auth)
            await ui.show('أرسل إطار auth الكامل وفيه UID. لا ترسل كلمة مرور. /cancel للإلغاء',view='accounts')
        except ValueError as exc:
            await ui.show(str(exc),view='accounts')

    @dp.message(RegistryInput.account_auth)
    async def registry_auth(msg,state):
        data=await state.get_data()
        with suppress(Exception):await msg.delete()
        try:
            frame=auth_frame(msg.text or '',data['mode'],0)
            await state.update_data(frame=frame)
            await state.set_state(RegistryInput.account_url)
            await ui.show('أرسل رابط wss أو /skip للرابط الافتراضي. /cancel للإلغاء',view='accounts')
        except (ValueError,KeyError):
            await ui.show('إطار auth كامل مطلوب مع session وuid موجب؛ لن تُعرض قيمته. /cancel للإلغاء',view='accounts')

    @dp.message(RegistryInput.account_url)
    async def registry_url(msg,state):
        data=await state.get_data()
        try:
            mode=data['mode']
            value=(config.REAL_URL if mode=='REAL' else config.DEMO_URL) if msg.text=='/skip' else valid_url(msg.text)
            credentials=dict(ssid=data['frame'],ws_url=value,uid=0)
            if data.get('key'):
                key=data['key'];await worker.account_action('edit',key,credentials)
            else:
                key=await worker.account_action('add',value=dict(name=data['name'],mode=mode,credentials=credentials))
            await state.clear();text,kb=account_detail(repo.doc,key)
            await ui.show('تم الحفظ؛ التداول متوقف.\n'+text,kb,view='accounts')
        except ValueError as exc:
            await ui.show(str(exc),view='accounts')
        except Exception as exc:
            await ui.show('لم يُؤكد الحفظ: '+type(exc).__name__,view='accounts')

    @dp.message(RegistryInput.rename)
    async def registry_rename(msg,state):
        try:
            key=(await state.get_data())['key']
            await worker.account_action('rename',key,msg.text)
            await state.clear();text,kb=account_detail(repo.doc,key)
            await ui.show(text,kb,view='accounts')
        except ValueError as exc:
            await ui.show(str(exc),view='accounts')
        except Exception as exc:
            await ui.show('لم يُؤكد الحفظ: '+type(exc).__name__,view='accounts')


async def registry_callback(action,state,worker,repo,ui):
    if action=='sources' or action.startswith('src:page:'):
        page=int(action.split(':')[2]) if action.startswith('src:page:') else 0
        await ui.show(source_text(repo.doc),sources_kb(repo.doc,page),view='sources');return True
    if action=='account_profiles' or action.startswith('acct:page:'):
        page=int(action.split(':')[2]) if action.startswith('acct:page:') else 0
        await ui.show(accounts_list_text(repo.doc),accounts_list_kb(repo.doc,page),view='accounts');return True
    if action=='src:add':
        await state.set_state(RegistryInput.source)
        await ui.show('أرسل اسم البوت أو: الاسم | @username\nالمصدر الجديد يبدأ يدوياً. /cancel للإلغاء',view='sources');return True
    if action.startswith('src:'):
        _,op,key=action.split(':',2)
        if key not in repo.doc['signal_sources']:raise ValueError('المصدر غير موجود')
        if op=='select':await worker.source_action('select',key)
        elif op in ('auto','manual'):await worker.source_action('edit',key,{'setup_mode':op.upper()})
        elif op=='button':
            await state.set_state(RegistryInput.source_button);await state.update_data(key=key)
            await ui.show('أرسل نص زر طلب الإشارة، مثال New signal. /cancel للإلغاء',view='sources');return True
        elif op=='delete':
            await ui.show('حذف المصدر المحفوظ؟',keyboard([[('تأكيد الحذف','src:remove:'+key)],[('إلغاء','sources')]]),view='sources');return True
        elif op=='remove':
            await worker.source_action('delete',key)
            await ui.show(source_text(repo.doc),sources_kb(repo.doc),view='sources');return True
        elif op!='open':raise ValueError('عملية مصدر غير صالحة')
        text,kb=source_detail(repo.doc,key)
        await ui.show(text,kb,view='sources');return True
    if action.startswith('acct:'):
        _,op,key=action.split(':',2)
        if op=='add':
            if key not in ('REAL','PRACTICE'):raise ValueError('نوع حساب غير صالح')
            await state.set_state(RegistryInput.account_name);await state.update_data(mode=key)
            await ui.show('أرسل اسماً مميزاً للحساب. /cancel للإلغاء',view='accounts');return True
        if key not in repo.doc['broker_profiles']:raise ValueError('الحساب غير موجود')
        p=repo.doc['broker_profiles'][key]
        if op=='select' and p['mode']=='REAL':
            await ui.show('⚠️ اختيار حساب حقيقي يستخدم أموالاً حقيقية. تأكيد؟',keyboard([
                [('تأكيد REAL','acct:confirm:'+key)],[('إلغاء','account_profiles')]]),view='accounts');return True
        if op in ('select','confirm'):
            await worker.account_action('select',key)
        elif op=='auth':
            await state.set_state(RegistryInput.account_auth)
            await state.update_data(key=key,mode=p['mode'])
            await ui.show('أرسل إطار auth الجديد لنفس UID. /cancel للإلغاء',view='accounts');return True
        elif op=='rename':
            await state.set_state(RegistryInput.rename);await state.update_data(key=key)
            await ui.show('أرسل الاسم الجديد. /cancel للإلغاء',view='accounts');return True
        elif op=='delete':
            await ui.show('حذف الحساب وسجله المحفوظ؟',keyboard([[('تأكيد الحذف','acct:remove:'+key)],
                [('إلغاء','account_profiles')]]),view='accounts');return True
        elif op=='remove':
            await worker.account_action('delete',key)
            await ui.show(accounts_list_text(repo.doc),accounts_list_kb(repo.doc),view='accounts');return True
        elif op!='open':raise ValueError('عملية حساب غير صالحة')
        text,kb=account_detail(repo.doc,key)
        await ui.show(text+'\nStart للاستئناف بعد التبديل.',kb,view='accounts');return True
    return False
