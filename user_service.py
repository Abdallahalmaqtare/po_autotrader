"""Shared private bot gateway. Authorization is checked on every update; each
approved Telegram ID gets a document, FSM, config context and worker of its own.
"""
import asyncio,time,hashlib
from pymongo.errors import DuplicateKeyError
from contextlib import suppress
from copy import deepcopy
from dataclasses import dataclass,field
from aiogram import Dispatcher
from aiogram.types import Update,InlineKeyboardButton
import config
from tenant_scope import Scope,bind
from access_control import AccessStore,ResourceClaims
from database import Repository
from cloud_settings import defaults
from worker import TradingWorker
from control_bot import build_dispatcher,kb,home
from branded_ui import get_panels
from account_details import credential_uid
from telegram_bootstrap import session_from_env

class UserBot:
    def __init__(self,bot,is_owner=False):self._bot=bot;self.is_owner=is_owner;self._aboud_panels=None
    def __getattr__(self,name):return getattr(self._bot,name)
    async def __call__(self,*args,**kwargs):return await self._bot(*args,**kwargs)

@dataclass
class Runtime:
    uid:int
    repo:object
    worker:object
    bot:object
    dp:object
    scope:object
    touched:float=0
    claims:set=field(default_factory=set)

def fresh_user_defaults():
    d=defaults();d['_id']='temporary';d['account_mode']='PRACTICE'
    for key in ('demo_account','real_account'):d[key]['ssid']='';d[key]['uid']=0
    d.update(accounts={},active_accounts={'PRACTICE':None,'REAL':None},active_account_id=None,
             telegram_session='',telegram_api_id=0,telegram_api_hash='')
    return d

class UserService:
    def __init__(self,repo,bot,owner,users,settings,claims,shutdown):
        self.root=repo;self.bot=bot;self.owner=int(owner);self.shutdown=shutdown;self.settings=settings
        self.access=AccessStore(users,self.owner,config.CONTROL_BOT_TOKEN);self.claims=ResourceClaims(claims)
        self.runtimes={};self.lock=asyncio.Lock();self.admin_add=False
        self.owner_values={k:getattr(config,k) for k in ('TELEGRAM_API_ID','TELEGRAM_API_HASH','TELEGRAM_STRING_SESSION','SIGNAL_BOT_USERNAME')}
    async def runtime(self,uid):
        uid=int(uid)
        async with self.lock:
            if uid in self.runtimes:return self.runtimes[uid]
            if len(self.runtimes)>=25:raise ValueError('الخدمة ممتلئة مؤقتاً؛ تواصل مع الأدمن')
            values=dict(self.owner_values) if uid==self.owner else {'ACCOUNT_TYPE':'PRACTICE','IS_DEMO':1}
            values['TELEGRAM_EXPECTED_USER_ID']=uid
            scope=Scope(uid,values)
            with bind(scope):
                repo=self.root if uid==self.owner else Repository('','',collection=self.settings,document_id=f'user:{uid}',default_factory=fresh_user_defaults)
                if repo is not self.root:
                    await repo.initialize()
                    if not await repo.acquire():raise ValueError('حسابك قيد التحميل؛ أعد المحاولة')
                for field,key in (('telegram_api_id','TELEGRAM_API_ID'),('telegram_api_hash','TELEGRAM_API_HASH'),('telegram_session','TELEGRAM_STRING_SESSION')):
                    if repo.doc.get(field):scope.values[key]=repo.doc[field]
                scope.values['SIGNAL_BOT_USERNAME']=repo.doc.get('signal_bot_username') or self.owner_values['SIGNAL_BOT_USERNAME']
                worker=TradingWorker(repo);proxy=UserBot(self.bot,uid==self.owner);dp=build_dispatcher(repo,worker,proxy)
                rt=Runtime(uid,repo,worker,proxy,dp,scope,time.monotonic());self.runtimes[uid]=rt
                async def before_start():
                    record=await self.access.get(uid)
                    if not record or record.get('status')!='active':raise ValueError('الدخول موقوف')
                    mode=repo.doc['account_mode'];creds=repo.doc['real_account' if mode=='REAL' else 'demo_account']
                    account=credential_uid(creds,mode)
                    if not account:raise ValueError('أضف حساب تداول صالحاً أولاً')
                    session=session_from_env(config.TELEGRAM_STRING_SESSION)
                    fingerprint=hashlib.sha256(session.auth_key.key).hexdigest()
                    keys={f'account:{mode}:{account}','session:'+fingerprint}
                    obtained=[]
                    for key in sorted(keys):
                        if not await self.claims.claim(key,repo.owner,float(repo.doc['trading']['duration_seconds'])+float(config.LONG_RESULT_GRACE_SECONDS)+30):
                            for old in obtained:await self.claims.release(old,repo.owner)
                            raise ValueError('الحساب أو جلسة الإشارات مستخدمان في تشغيل آخر')
                        obtained.append(key)
                    rt.claims.update(keys)
                worker.before_start=before_start
                return rt
    async def revoke(self,uid,delete=False):
        uid=int(uid);await self.access.status(self.owner,uid,'deleted' if delete else 'blocked')
        rt=self.runtimes.get(uid)
        if rt:
            with bind(rt.scope):await rt.worker.pause()
            if delete and not rt.worker.pending:await self.dispose(uid,True)
        elif delete:
            await self.settings.update_one({'_id':f'user:{uid}'},{'$set':{'telegram_session':'','telegram_api_hash':'','telegram_api_id':0,'accounts':{},'demo_account.ssid':'','real_account.ssid':''}})
    async def dispose(self,uid,scrub=False):
        rt=self.runtimes.pop(int(uid),None)
        if not rt:return
        with bind(rt.scope):
            with suppress(Exception):await rt.worker.close()
            pending=rt.worker.pending
            for key in rt.claims:
                if pending:
                    until=float(pending.get('created_at',time.time()))+float(pending.get('duration_seconds',0))+float(config.LONG_RESULT_GRACE_SECONDS)+30
                    await self.claims.extend(key,rt.repo.owner,until)
                else:await self.claims.release(key,rt.repo.owner)
            if scrub:await rt.repo.collection.update_one(rt.repo.fence(),{'$set':{'telegram_session':'','telegram_api_hash':'','telegram_api_id':0,'accounts':{},'demo_account.ssid':'','real_account.ssid':''}})
            if rt.repo is not self.root:
                if rt.repo.leader:await rt.repo.release()
                await rt.repo.close()
            await rt.dp.storage.close()
    async def supervise(self):
        while not self.shutdown.is_set():
            try:await asyncio.wait_for(self.shutdown.wait(),8)
            except asyncio.TimeoutError:pass
            for uid,rt in list(self.runtimes.items()):
                with bind(rt.scope):
                    try:
                        if rt.repo is not self.root:await rt.repo.renew()
                        record=await self.access.get(uid)
                        if not record or record.get('status')!='active':
                            rt.worker.request_pause()
                            if not rt.worker.running and not rt.worker.pending:
                                await self.dispose(uid,bool(record and record.get('status')=='deleted'));continue
                        active=rt.worker.running or rt.worker.in_flight or rt.worker.pending
                        for key in list(rt.claims):
                            if active:
                                if not await self.claims.claim(key,rt.repo.owner,float((rt.worker.pending or {}).get('duration_seconds',0))+float(config.LONG_RESULT_GRACE_SECONDS)+30):raise RuntimeError('resource_lease_lost')
                            else:await self.claims.release(key,rt.repo.owner);rt.claims.discard(key)
                        if uid!=self.owner and not active and time.monotonic()-rt.touched>900:await self.dispose(uid)
                    except asyncio.CancelledError:raise
                    except Exception:rt.worker.request_stop()
    async def close(self):
        for uid in list(self.runtimes):
            with suppress(Exception):await self.dispose(uid)
    async def admin_panel(self,page=0):
        records=await self.access.list();pages=max(1,(len(records)+9)//10);page=max(0,min(page,pages-1))
        rows=[[(f"{r['_id']} · {r.get('status','pending')}",f"users:view:{r['_id']}")] for r in records[page*10:page*10+10]]
        if pages>1:rows.append([('◀️',f'users:page:{max(0,page-1)}'),(f'{page+1}/{pages}',f'users:page:{page}'),('▶️',f'users:page:{min(pages-1,page+1)}')])
        await self.bot.send_message(self.owner,'👥 إدارة المستخدمين',reply_markup=kb(rows+[[('➕ إضافة بمعرّف Telegram','users:add')],[('🏠 الرئيسية','home')]]))
    async def admin_action(self,event,data):
        if event.from_user.id!=self.owner:raise PermissionError('owner_only')
        await event.answer()
        if data=='users':await self.admin_panel();return
        if data=='users:add':self.admin_add=True;await self.bot.send_message(self.owner,'أرسل Telegram ID الرقمي للمستخدم. /cancel للإلغاء');return
        _,action,raw=data.split(':');uid=int(raw)
        if action=='page':await self.admin_panel(uid);return
        if uid<=0 or uid==self.owner:return
        if action=='view':
            d=await self.access.get(uid) or {}
            await self.bot.send_message(self.owner,f"المستخدم {uid}\nالحالة: {d.get('status','غير مسجل')}",reply_markup=kb([[('🔑 إصدار رمز / إعادة تفعيل',f'users:issue:{uid}')],[('⛔ إيقاف',f'users:block:{uid}'),('🗑 حذف',f'users:ask_delete:{uid}')],[('⬅️ المستخدمون','users')]]))
        elif action=='issue':
            code=await self.access.issue(self.owner,uid)
            await self.bot.send_message(self.owner,f'رمز المستخدم {uid}:\n{code}\nصالح 10 دقائق لهذا المعرّف فقط.')
            with suppress(Exception):await self.bot.send_message(uid,'أصدر الأدمن رمز دخول لك. احصل عليه منه وأرسله هنا.')
        elif action=='block':await self.revoke(uid);await self.bot.send_message(self.owner,f'أُوقف المستخدم {uid}.')
        elif action=='ask_delete':await self.bot.send_message(self.owner,f'حذف المستخدم {uid}؟',reply_markup=kb([[('نعم، حذف',f'users:delete:{uid}')],[('إلغاء','users')]]))
        elif action=='delete':await self.revoke(uid,True);await self.bot.send_message(self.owner,f'أُلغي دخول {uid}. تُمسح الجلسات بعد انتهاء أي صفقة معلّقة.')
    async def route(self,update):
        event=update.callback_query or update.message
        if event is None:return
        msg=event.message if update.callback_query else event
        if not msg or msg.chat.type!='private' or msg.chat.id!=event.from_user.id:return
        uid=event.from_user.id;data=(event.data or '') if update.callback_query else ''
        text=(event.text or '').strip() if update.message else '';activated=False
        try:
            if data.startswith('users'):
                if uid!=self.owner:await event.answer('غير مسموح',show_alert=True);return
                await self.admin_action(event,data);return
            if uid==self.owner and self.admin_add and update.message:
                if text=='/cancel':self.admin_add=False;return
                if not text.isdecimal():await self.bot.send_message(uid,'المعرّف الرقمي فقط');return
                code=await self.access.issue(uid,int(text));self.admin_add=False
                await self.bot.send_message(uid,f'رمز المستخدم {int(text)}: {code}\nصالح 10 دقائق لهذا المعرّف فقط.');return
            record=await self.access.get(uid)
            if not record or record.get('status')!='active':
                if update.callback_query:await event.answer('يلزم اعتماد الأدمن',show_alert=True);return
                if record and record.get('status') in ('blocked','deleted'):await self.bot.send_message(uid,'الدخول موقوف. تواصل مع الأدمن.');return
                if text.isdecimal() and len(text)==8:
                    with suppress(Exception):await event.delete()
                    if not await self.access.verify(uid,text):await self.bot.send_message(uid,'الرمز غير صحيح أو منتهٍ. تواصل مع الأدمن.');return
                    activated=True
                else:
                    notify=await self.access.request(uid,event.from_user.full_name)
                    await self.bot.send_message(uid,f'مرحبًا بك في بوت عبود تريدينج 👋\nمعرّفك: {uid}\nأرسل رمز الدخول الذي يمنحك الأدمن إياه.')
                    if notify:await self.bot.send_message(self.owner,f'طلب دخول جديد\nTelegram ID: {uid}',reply_markup=kb([[('🔑 إنشاء رمز',f'users:issue:{uid}')],[('⛔ إيقاف',f'users:block:{uid}')]]))
                    return
            rt=await self.runtime(uid);rt.touched=time.monotonic()
            with bind(rt.scope):
                if activated:await get_panels(rt.bot).show('مرحبًا بك في عبود تريدينج 👋',home(rt.worker),view='home')
                else:await rt.dp.feed_update(rt.bot,update)
        except asyncio.CancelledError:raise
        except Exception as exc:
            # Never echo API session/auth/code input or exception text.
            await self.bot.send_message(uid,'تعذّر إكمال الطلب: '+type(exc).__name__+'. حاول مجدداً أو تواصل مع الأدمن.')

def gateway_dispatcher(service):
    dp=Dispatcher()
    @dp.message()
    async def on_message(message,event_update:Update):await service.route(event_update)
    @dp.callback_query()
    async def on_callback(callback,event_update:Update):await service.route(event_update)
    return dp
