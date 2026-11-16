"""Versioned in-document registries. Active v20 fields remain compatibility views.
Never log or display credential payloads. Snapshots move under the repository lock.
"""
from copy import deepcopy
import re
from uuid import uuid4
import config
from telegram_bootstrap import signal_username


def safe_name(value):
    value=str(value or '').strip()
    if not 1 <= len(value) <= 40 or any(ord(c)<32 for c in value):
        raise ValueError('اسم قصير مطلوب (1–40 حرفاً)')
    return value


def source_spec(value):
    username=signal_username(value.get('username'))
    mode=value.get('setup_mode','AUTO')
    if mode not in ('AUTO','MANUAL'):raise ValueError('طريقة إعداد المصدر غير صالحة')
    button=str(value.get('button_text','New signal')).strip()
    if not 1<=len(button)<=64 or any(ord(c)<32 for c in button):
        raise ValueError('اسم زر الإشارة غير صالح')
    return dict(name=safe_name(value.get('name') or username), username=username,
                setup_mode=mode,button_text=button)


def selected_source(doc):
    registry=doc.get('signal_sources') or {}
    key=doc.get('active_signal_source')
    if key in registry:return deepcopy(registry[key])
    return dict(name='المصدر الحالي',username=config.SIGNAL_BOT_USERNAME,
                setup_mode='AUTO',button_text=config.NEW_SIGNAL_BUTTON_TEXT)


def source_key(doc):
    spec=selected_source(doc)
    return tuple(spec.get(k) for k in ('username','setup_mode','button_text'))


def manual_mode(doc):
    return selected_source(doc)['setup_mode']=='MANUAL'


def account_key(mode):
    return 'real_account' if mode=='REAL' else 'demo_account'


def active_profile_id(doc,mode=None):
    return doc.get('active_accounts',{}).get(mode or doc['account_mode'])


def snapshot(doc,mode=None):
    mode=mode or doc['account_mode']
    runtime=doc['runtime']
    stored=doc.get('broker_profiles',{}).get(active_profile_id(doc,mode),{})
    return dict(stats=deepcopy(doc['account_stats'][mode]),
                recovery=deepcopy(runtime['recovery'][mode]),
                martingale_step=runtime.get('martingale_step',0) if mode==doc['account_mode'] else stored.get('martingale_step',0),
                sizing_signature=deepcopy(runtime.get('sizing_signature')) if mode==doc['account_mode'] else deepcopy(stored.get('sizing_signature')),
                cycle=deepcopy(runtime.get('cycle')) if mode==doc['account_mode'] else deepcopy(stored.get('cycle')))


def migrate(doc):
    if 'signal_sources' not in doc:
        doc['signal_sources']={'legacy':source_spec(selected_source(doc))}
        doc['active_signal_source']='legacy'
    if 'broker_profiles' not in doc:
        doc['broker_profiles']={};doc['active_accounts']={}
        for mode in ('PRACTICE','REAL'):
            key='legacy_demo' if mode=='PRACTICE' else 'legacy_real'
            doc['active_accounts'][mode]=key
            doc['broker_profiles'][key]=dict(name='الحساب السابق '+mode,mode=mode,
                credentials=deepcopy(doc[account_key(mode)]),**snapshot(doc,mode))
    return doc


def capture(doc,mode=None):
    mode=mode or doc['account_mode']
    key=active_profile_id(doc,mode)
    if key in doc.get('broker_profiles',{}):
        item=doc['broker_profiles'][key]
        item.update(snapshot(doc,mode),credentials=deepcopy(doc[account_key(mode)]))


def project(doc,key):
    from recovery import state_defaults
    from work_cycle import cycle_state
    record=doc['broker_profiles'][key];mode=record['mode']
    doc['active_accounts'][mode]=key
    doc['account_mode']=mode
    doc[account_key(mode)]=deepcopy(record['credentials'])
    doc['account_stats'][mode]=deepcopy(record['stats'])
    doc['runtime']['recovery'][mode]=deepcopy(record.get('recovery',state_defaults()))
    doc['runtime'].update(martingale_step=int(record.get('martingale_step',0)),
        sizing_signature=deepcopy(record.get('sizing_signature')),
        cycle=deepcopy(record.get('cycle') or cycle_state()))


def validate_registries(doc):
    sources=doc.get('signal_sources',{})
    if not isinstance(sources,dict) or not 1<=len(sources)<=100:raise ValueError('قائمة المصادر غير صالحة')
    if doc.get('active_signal_source') not in sources:raise ValueError('مصدر نشط غير صالح')
    for key,value in sources.items():
        if not re.fullmatch(r'[a-zA-Z0-9_]{1,40}',key):raise ValueError('معرّف مصدر غير صالح')
        if source_spec(value)!=value:raise ValueError('إعدادات مصدر غير صالحة')
    profiles=doc.get('broker_profiles',{})
    if not isinstance(profiles,dict) or not 2<=len(profiles)<=200:raise ValueError('قائمة الحسابات غير صالحة')
    from cloud_settings import valid_url
    for key,item in profiles.items():
        if not re.fullmatch(r'[a-zA-Z0-9_]{1,40}',key) or item.get('mode') not in ('REAL','PRACTICE'):
            raise ValueError('حساب محفوظ غير صالح')
        safe_name(item['name']);valid_url(item['credentials']['ws_url'])
    for mode in ('REAL','PRACTICE'):
        key=active_profile_id(doc,mode)
        if key not in profiles or profiles[key]['mode']!=mode:raise ValueError('الحساب النشط غير صالح')


def top_changes(old,new):
    return {key:deepcopy(value) for key,value in new.items() if old.get(key)!=value and key not in ('_id','lease')}


class RegistryRepository:
    async def registry_write(self, mutate):
        from cloud_settings import validate_document
        async with self.lock:
            new=deepcopy(self.doc)
            result=mutate(new)
            validate_registries(new);validate_document(new)
            writes=top_changes(self.doc,new)
            if writes:
                query=self.fence();query['runtime.pending']=None
                r=await self.collection.update_one(query,{'$set':writes})
                if r.matched_count!=1:raise RuntimeError('pending_or_lease_lost')
                self.doc=new
            return result

    async def add_source(self,spec):
        spec=source_spec(spec);key='s_'+uuid4().hex[:12]
        def mutate(d):
            if any(s['username'].casefold()==spec['username'].casefold() for s in d['signal_sources'].values()):
                raise ValueError('هذا البوت مضاف مسبقاً')
            d['signal_sources'][key]=spec
            return key
        return await self.registry_write(mutate)

    async def edit_source(self,key,changes):
        if set(changes)-{'name','username','setup_mode','button_text'}:raise ValueError('حقل مصدر غير صالح')
        def mutate(d):
            if key not in d['signal_sources']:raise ValueError('المصدر غير موجود')
            spec=source_spec({**d['signal_sources'][key],**changes})
            if any(k!=key and s['username'].casefold()==spec['username'].casefold() for k,s in d['signal_sources'].items()):
                raise ValueError('البوت مضاف مسبقاً')
            d['signal_sources'][key]=spec
        return await self.registry_write(mutate)

    async def select_source(self,key):
        def mutate(d):
            if key not in d['signal_sources']:raise ValueError('المصدر غير موجود')
            d['active_signal_source']=key
        return await self.registry_write(mutate)

    async def delete_source(self,key):
        def mutate(d):
            if key==d['active_signal_source']:raise ValueError('بدّل المصدر النشط قبل حذفه')
            if key not in d['signal_sources']:raise ValueError('المصدر غير موجود')
            del d['signal_sources'][key]
        return await self.registry_write(mutate)

    async def add_profile(self,name,mode,credentials):
        from cloud_settings import auth_frame,valid_url,empty_stats
        from recovery import state_defaults
        from work_cycle import cycle_state
        if mode not in ('REAL','PRACTICE'):raise ValueError('نوع حساب غير صالح')
        credentials=deepcopy(credentials)
        credentials['ssid']=auth_frame(credentials['ssid'],mode,credentials.get('uid',0))
        credentials['ws_url']=valid_url(credentials['ws_url'])
        credentials.setdefault('uid',0)
        key='a_'+uuid4().hex[:12];name=safe_name(name)
        def mutate(d):
            # UID is public identity; identical UID+mode must share one ledger.
            from account_details import credential_uid
            uid=credential_uid(credentials,mode)
            if any(p['mode']==mode and credential_uid(p['credentials'],mode)==uid
                   for p in d['broker_profiles'].values()):raise ValueError('UID لهذا النوع مضاف؛ حدّث جلسته بدلاً من تكراره')
            d['broker_profiles'][key]=dict(name=name,mode=mode,credentials=credentials,
                stats=empty_stats(),recovery=state_defaults(),martingale_step=0,
                sizing_signature=None,cycle=cycle_state())
            return key
        return await self.registry_write(mutate)

    async def select_profile(self,key):
        def mutate(d):
            if key not in d['broker_profiles']:raise ValueError('الحساب غير موجود')
            capture(d)
            if active_profile_id(d)==key:return
            project(d,key)
        return await self.registry_write(mutate)

    async def rename_profile(self,key,name):
        name=safe_name(name)
        def mutate(d):
            if key not in d['broker_profiles']:raise ValueError('الحساب غير موجود')
            d['broker_profiles'][key]['name']=name
        return await self.registry_write(mutate)

    async def edit_profile(self,key,credentials):
        from cloud_settings import auth_frame,valid_url
        from account_details import credential_uid
        credentials=deepcopy(credentials)
        def mutate(d):
            if key not in d['broker_profiles']:raise ValueError('الحساب غير موجود')
            p=d['broker_profiles'][key];mode=p['mode']
            new={**p['credentials'],**credentials}
            new['ssid']=auth_frame(new['ssid'],mode,new.get('uid',0));valid_url(new['ws_url'])
            olduid=credential_uid(p['credentials'],mode);newuid=credential_uid(new,mode)
            if olduid and olduid!=newuid:raise ValueError('UID مختلف؛ أضف حساباً جديداً لعزل الإحصائيات')
            p['credentials']=new
            if active_profile_id(d,mode)==key:d[account_key(mode)]=deepcopy(new)
        return await self.registry_write(mutate)

    async def delete_profile(self,key):
        def mutate(d):
            if key in d['active_accounts'].values():raise ValueError('اختر بديلاً من نفس النوع قبل حذف الحساب المحدد')
            if key not in d['broker_profiles']:raise ValueError('الحساب غير موجود')
            del d['broker_profiles'][key]
        return await self.registry_write(mutate)
