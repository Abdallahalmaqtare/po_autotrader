"""Async MongoDB persistence, lease fencing and atomic single-document accounting."""
import config
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from pymongo import AsyncMongoClient, ReturnDocument
from pymongo.write_concern import WriteConcern
from cloud_settings import defaults, validate_document, patched, empty_stats, MODES
from recovery import after_result, selected_state, reset_state, recovery_defaults
from work_cycle import cycle_state
from account_registry import migrate, bank_active, make_record, account_key, select_document
from trade_settlement import number
from decimal import Decimal
from manual_pause import pause_defaults


def utcnow():
    return datetime.now(timezone.utc)


class Repository:
    def __init__(self, uri, db_name, collection=None):
        self.client = None
        if collection is None:
            self.client = AsyncMongoClient(uri, serverSelectionTimeoutMS=8000,
                                           connectTimeoutMS=8000, socketTimeoutMS=10000,
                                           tz_aware=True)
            self.collection = self.client[db_name].get_collection(
                'bot_settings', write_concern=WriteConcern(w='majority'))
        else:
            self.collection = collection  # offline test injection
        self.owner = uuid4().hex
        self.doc = None
        self.lock = asyncio.Lock()
        self.leader = False

    async def initialize(self):
        if self.client:
            await self.client.admin.command('ping')
        await self.collection.update_one({'_id': 'current_settings'},
                                         {'$setOnInsert': defaults()}, upsert=True)
        raw = await self.collection.find_one({'_id': 'current_settings'})
        # Merge new schema defaults in RAM, without overriding any stored values.
        def merge(a, b):
            for k, v in b.items():
                if k not in a:
                    a[k] = deepcopy(v)
                elif isinstance(v, dict) and isinstance(a[k], dict):
                    merge(a[k], v)
            return a
        previous = deepcopy(raw)
        self.doc = migrate(validate_document(merge(raw, defaults())))
        if self.leader:
            missing = {}
            def collect(old, new, prefix=''):
                for key, value in new.items():
                    path = prefix + key
                    if key not in old:
                        missing[path] = deepcopy(value)
                    elif isinstance(value, dict) and isinstance(old[key], dict):
                        collect(old[key], value, path + '.')
            collect(previous, self.doc)
            for key in ('active_account_id','active_accounts'):
                if previous.get(key)!=self.doc.get(key):missing[key]=deepcopy(self.doc[key])
            if missing:
                result = await self.collection.update_one(self.fence(), {'$set': missing})
                if result.matched_count != 1:
                    raise RuntimeError('lease_lost')
        return self.doc

    def fence(self):
        return {'_id': 'current_settings', 'lease.owner': self.owner,
                'lease.expires_at': {'$gt': utcnow()}}

    async def acquire(self):
        now = utcnow()
        q = {'_id': 'current_settings', '$or': [
            {'lease.expires_at': {'$lte': now}},
            {'lease': {'$exists': False}}, {'lease.owner': self.owner}]}
        doc = await self.collection.find_one_and_update(q, {'$set': {
            'lease': {'owner': self.owner, 'expires_at': now + timedelta(seconds=30)}}},
            return_document=ReturnDocument.AFTER)
        self.leader = doc is not None
        if self.leader:
            # persist any missing schema fields after acquiring the only writer lease
            await self.initialize()
        return self.leader

    async def renew(self):
        r = await self.collection.update_one(self.fence(), {'$set': {
            'lease.expires_at': utcnow() + timedelta(seconds=30)}})
        self.leader = r.matched_count == 1
        if not self.leader:
            raise RuntimeError('lease_lost')

    async def release(self):
        await self.collection.update_one({'_id': 'current_settings', 'lease.owner': self.owner},
                                         {'$unset': {'lease': ''}})
        self.leader = False

    async def update(self, fields):
        async with self.lock:
            new_doc = patched(self.doc, fields)
            writes = deepcopy(fields)
            if ('trading.asset' in fields and self.doc['runtime'].get('manual_pause',{}).get('requested')
                    and not self.doc['runtime'].get('pending')):
                signature=deepcopy(self.doc['runtime'].get('sizing_signature'))
                if signature and len(signature)>1:
                    signature[1]=fields['trading.asset']
                    writes['runtime.sizing_signature']=signature
                    new_doc['runtime']['sizing_signature']=signature
                mode=self.doc['account_mode']
                recovery=deepcopy(new_doc['runtime']['recovery'][mode])
                if recovery.get('sequence_signature'):
                    recovery['sequence_signature'][1]=fields['trading.asset']
                    writes['runtime.recovery.'+mode]=recovery
                    new_doc['runtime']['recovery'][mode]=recovery
            if new_doc['schedule']!=self.doc['schedule']:
                writes['runtime.cycle']=cycle_state()
                new_doc['runtime']['cycle']=cycle_state()
            # Settings edits never overwrite finances. New sizing / recovery policy
            # explicitly cancels old recovery phases; old in-flight epochs cannot revive them.
            reset_all = (new_doc['recovery'] != self.doc['recovery'] or
                         new_doc['trading']['trade_amount'] != self.doc['trading']['trade_amount'])
            credential_modes = {mode for mode, key in (('REAL','real_account'), ('PRACTICE','demo_account'))
                                if new_doc[key] != self.doc[key]}
            reset_modes = set(MODES) if reset_all else credential_modes
            if reset_modes:
                for mode in reset_modes:
                    value = reset_state(selected_state(self.doc, mode))
                    writes['runtime.recovery.' + mode] = value
                    new_doc['runtime']['recovery'][mode] = value
                writes.update({'runtime.martingale_step': 0, 'runtime.sizing_signature': None})
                new_doc['runtime'].update(martingale_step=0, sizing_signature=None)
            if reset_all:
                for key,record in new_doc.get('accounts',{}).items():
                    record['recovery']=reset_state(record['recovery'])
                    record.update(martingale_step=0,sizing_signature=None,sequence_loss_amount=0.0)
                writes['accounts']=deepcopy(new_doc.get('accounts',{}))
            r = await self.collection.update_one(self.fence(), {'$set': writes})
            if r.matched_count != 1:
                raise RuntimeError('lease_lost')
            # DB first: a failed write is never reported as a saved setting.
            self.doc = new_doc

    async def save_cycle(self,state,expected):
        async with self.lock:
            if self.doc['schedule']!=expected:return False
            result=await self.collection.update_one(self.fence(),{'$set':{'runtime.cycle':deepcopy(state)}})
            if result.matched_count!=1:raise RuntimeError('lease_lost')
            self.doc['runtime']['cycle']=deepcopy(state)
            return True

    async def reserve(self, order):
        async with self.lock:
            q = self.fence()
            q['runtime.pending'] = None
            r = await self.collection.update_one(q, {'$set': {'runtime.pending': order}})
            if r.matched_count != 1:
                raise RuntimeError('pending_or_lease_lost')
            self.doc['runtime']['pending'] = deepcopy(order)

    async def resolve(self, order, result, before, after, next_step, dispatched=True,
                      seq_lost=0, seq_lost_amount=0.0):
        """One atomic pending-ID commit for stats, recovery, ledger and sizing.
        Strict transport supplies a matched deal receipt; balances are diagnostic.
        Legacy mock/import paths retain explicit delta accounting, never UI writes.
        """
        async with self.lock:
            amount=order['amount'];mode=order['mode']
            pending=self.doc['runtime'].get('pending')
            if not pending or pending['id']!=order['id']:
                raise RuntimeError('result_already_saved_or_lease_lost')
            if order.get('account_id') and order['account_id']!=self.doc.get('active_account_id'):
                raise RuntimeError('settlement_account_changed')
            receipt=order.get('settlement')
            delta=None
            if dispatched and result in ('win','loss','draw'):
                if order.get('strict_result'):
                    if not isinstance(receipt,dict) or any((
                        receipt.get('order_id')!=order['id'],receipt.get('request_id')!=order['request_id'],
                        receipt.get('result')!=result,receipt.get('mode')!=mode,
                        receipt.get('account_id')!=order.get('account_id'),
                        receipt.get('source')!='broker_closed_deal')):
                        raise ValueError('نتيجة الصفقة غير مرتبطة بالأمر المحفوظ')
                    delta=number(receipt.get('net_pnl'))
                    if delta is None or delta < -number(amount):raise ValueError('قيمة نتيجة غير صالحة')
                    label='win' if delta>0 else 'loss' if delta<0 else 'draw'
                    if label!=result:raise ValueError('تعارض النتيجة مع صافي الصفقة')
                    for old in self.doc.get('trade_ledger',[]):
                        if (receipt.get('broker_id') and old.get('broker_id')==receipt['broker_id']
                                and old.get('account_id')==order.get('account_id')):
                            raise RuntimeError('broker_deal_already_saved')
                else:
                    b,a=number(before),number(after)
                    if a is not None and b is not None:delta=a-b
            if order.get('strict_result') and dispatched and result is None:
                raise RuntimeError('unconfirmed_requires_review_not_loss')
            values={}
            if dispatched:
                values=dict(total_trades=1,total_staked=amount)
                if result in ('win','loss','draw'):
                    values[{'win':'won_trades','loss':'lost_trades','draw':'draws'}[result]]=1
                    if delta is not None:
                        values.update(net_pnl=float(delta),total_returned=float(max(Decimal(0),number(amount)+delta)),financial_trades=1)
                else:values['unconfirmed']=1
                if seq_lost:values.update(sequence_losses=seq_lost,lost_amount=round(seq_lost_amount,6))
            new=deepcopy(self.doc)
            for stats in (new['session_stats'],new['account_stats'][mode]):
                for k,v in values.items():
                    stats[k]=float((number(stats.get(k,0))+number(v)).quantize(Decimal('0.000001'))) if isinstance(v,float) else stats.get(k,0)+v
            previous=selected_state(self.doc,mode)
            current=after_result(self.doc.get('recovery',recovery_defaults()),previous,order,result,before,after,seq_lost) if dispatched else previous
            effective_step=0 if current['epoch']!=previous['epoch'] else next_step
            loss_before=float(new['runtime'].get('sequence_loss_amount',0))
            loss_next=(loss_before+abs(float(delta))) if dispatched and result=='loss' and delta is not None else loss_before
            if result=='win' or seq_lost or (result is None and dispatched and next_step==0):loss_next=0.0
            new['runtime'].update(pending=None,martingale_step=effective_step,
                sizing_signature=order.get('sizing_signature'),sequence_loss_amount=round(loss_next,6))
            new['runtime'].setdefault('recovery',{})[mode]=current
            ledger=list(new.get('trade_ledger',[]))
            if dispatched:
                ledger.append(dict(id=order['id'],request_id=order.get('request_id'),
                    broker_id=receipt.get('broker_id') if receipt else None,
                    account_id=order.get('account_id'),mode=mode,asset=order.get('asset'),amount=amount,
                    result=result or 'unconfirmed',net_pnl=float(delta) if delta is not None else None,
                    source='broker_closed_deal' if receipt else 'legacy_or_explicit_archive',
                    settled_at=utcnow()))
            new['trade_ledger']=ledger[-500:]
            bank_active(new)
            q=self.fence();q['runtime.pending.id']=order['id']
            writes={k:new[k] for k in ('session_stats','account_stats','runtime','trade_ledger','accounts')}
            r=await self.collection.update_one(q,{'$set':writes})
            if r.matched_count!=1:raise RuntimeError('result_already_saved_or_lease_lost')
            self.doc=new

    async def record_broker_id(self,order_id,broker_id):
        async with self.lock:
            pending=self.doc['runtime'].get('pending')
            if not pending or pending['id']!=order_id:return
            q=self.fence();q['runtime.pending.id']=order_id
            r=await self.collection.update_one(q,{'$set':{'runtime.pending.broker_id':str(broker_id)}})
            if r.matched_count!=1:raise RuntimeError('lease_lost')
            pending['broker_id']=str(broker_id)

    async def mark_uncertain(self,order):
        async with self.lock:
            q=self.fence();q['runtime.pending.id']=order['id']
            r=await self.collection.update_one(q,{'$set':{'runtime.pending.status':'unconfirmed'}})
            if r.matched_count!=1:raise RuntimeError('lease_lost')
            self.doc['runtime']['pending']['status']='unconfirmed'

    async def save_account(self,credentials,mode,name=''):
        record=make_record(credentials,mode,name);key=account_key(record['uid'],mode)
        async with self.lock:
            if self.doc['runtime'].get('pending'):raise ValueError('انتظر حسم الصفقة المعلّقة أولاً')
            new=deepcopy(self.doc);bank_active(new)
            existing=new['accounts'].get(key)
            if existing:
                existing['deleted']=False
                existing['credentials']=record['credentials']
                if record['name']:existing['name']=record['name']
            else:
                if len(new['accounts'])>=50:raise ValueError('الحد الأقصى 50 حساباً')
                new['accounts'][key]=record
            writes={'accounts':new['accounts']}
            if key==new.get('active_account_id'):
                field='real_account' if mode=='REAL' else 'demo_account'
                new[field]=deepcopy(new['accounts'][key]['credentials']);writes[field]=new[field]
            r=await self.collection.update_one(self.fence(),{'$set':writes})
            if r.matched_count!=1:raise RuntimeError('lease_lost')
            self.doc=new
        return key

    async def save_manual_pause(self,state):
        async with self.lock:
            r=await self.collection.update_one(self.fence(),{'$set':{'runtime.manual_pause':deepcopy(state)}})
            if r.matched_count!=1:raise RuntimeError('lease_lost')
            self.doc['runtime']['manual_pause']=deepcopy(state)

    async def resume_manual_pause(self,now):
        async with self.lock:
            new=deepcopy(self.doc);state={**pause_defaults(),**new['runtime'].get('manual_pause',{})}
            if new['runtime'].get('pending'):raise ValueError('نتيجة الصفقة لم تُحسم')
            cycle=new['runtime']['cycle']
            if state['cycle_key']==cycle.get('key'):
                if state['remaining_run'] is not None:cycle['run_until']=now+state['remaining_run']
                if state['remaining_rest'] is not None:cycle['rest_until']=now+state['remaining_rest']
            new['runtime']['manual_pause']=pause_defaults()
            q=self.fence();q['runtime.pending']=None
            r=await self.collection.update_one(q,{'$set':{'runtime.cycle':cycle,'runtime.manual_pause':pause_defaults()}})
            if r.matched_count!=1:raise RuntimeError('pending_or_lease_lost')
            self.doc=new

    async def edit_account(self,key,fields):
        from account_registry import safe_name
        from account_details import credential_uid
        from cloud_settings import valid_url,auth_frame
        if not fields or not set(fields)<= {'name','ssid','uid','ws_url'}:raise ValueError('تعديل غير صالح')
        async with self.lock:
            if self.doc['runtime'].get('pending'):raise ValueError('انتظر حسم الصفقة قبل تعديل الحساب')
            new=deepcopy(self.doc);bank_active(new)
            record=new.get('accounts',{}).get(key)
            if not record or record.get('deleted'):raise ValueError('الحساب غير موجود')
            credentials=deepcopy(record['credentials'])
            for k in ('ssid','uid','ws_url'):
                if k in fields:credentials[k]=fields[k]
            credentials['ws_url']=valid_url(credentials['ws_url'])
            credentials['ssid']=auth_frame(credentials['ssid'],record['mode'],credentials.get('uid',int(record['uid'])))
            if credential_uid(credentials,record['mode'])!=record['uid']:
                raise ValueError('UID الجلسة مختلف؛ أضف حساباً جديداً بدلاً من نقل سجل حساب آخر')
            record['credentials']=credentials
            if 'name' in fields:record['name']=safe_name(fields['name'])
            writes={'accounts':new['accounts']}
            if key==new.get('active_account_id'):
                mode=record['mode'];field='real_account' if mode=='REAL' else 'demo_account'
                new[field]=deepcopy(credentials);writes[field]=new[field]
            r=await self.collection.update_one(self.fence(),{'$set':writes})
            if r.matched_count!=1:raise RuntimeError('lease_lost')
            self.doc=new

    async def delete_account(self,key):
        from recovery import state_defaults
        async with self.lock:
            if self.doc['runtime'].get('pending'):raise ValueError('لا تحذف الحساب قبل حسم الصفقة المعلّقة')
            new=deepcopy(self.doc);bank_active(new)
            record=new.get('accounts',{}).get(key)
            if not record or record.get('deleted'):raise ValueError('الحساب غير موجود')
            # Tombstone retains financial provenance, not a reusable session.
            record['deleted']=True;record['credentials']={}
            mode=record['mode'];writes={'accounts':new['accounts']}
            if new['active_accounts'].get(mode)==key:
                new['active_accounts'][mode]=None
                field='real_account' if mode=='REAL' else 'demo_account'
                new[field]={'ssid':'','uid':0,'ws_url':config.REAL_URL if mode=='REAL' else config.DEMO_URL}
                writes[field]=new[field];writes['active_accounts']=new['active_accounts']
            if new.get('active_account_id')==key:
                new['active_account_id']=None;new['account_stats'][mode]=empty_stats()
                new['runtime'].update(martingale_step=0,sizing_signature=None,sequence_loss_amount=0,
                                      cycle=cycle_state(),manual_pause=pause_defaults())
                new['runtime']['recovery'][mode]=state_defaults()
                writes.update({k:new[k] for k in ('active_account_id','account_stats','runtime')})
            r=await self.collection.update_one(self.fence(),{'$set':writes})
            if r.matched_count!=1:raise RuntimeError('lease_lost')
            self.doc=new

    async def select_account(self,key):
        async with self.lock:
            if self.doc['runtime'].get('pending'):raise ValueError('لا يمكن تبديل الحساب قبل حسم الصفقة المعلّقة')
            new=select_document(self.doc,key)
            # Sequence debt belongs to the identity, never to the new account.
            new['runtime']['sequence_loss_amount']=new['accounts'][key].get('sequence_loss_amount',0.0)
            writes={k:new[k] for k in ('accounts','active_accounts','active_account_id','account_mode',
                                      'demo_account','real_account','account_stats','runtime')}
            q=self.fence();q['runtime.pending']=None
            r=await self.collection.update_one(q,{'$set':writes})
            if r.matched_count!=1:raise RuntimeError('pending_or_lease_lost')
            self.doc=new

    async def acknowledge_pending(self):
        order = self.doc['runtime'].get('pending')
        if order:
            # Unknown order is explicitly archived as unconfirmed, not retried.
            archived=deepcopy(order);archived['strict_result']=False
            await self.resolve(archived, None, None, None, 0, dispatched=True)

    async def reset_step(self):
        async with self.lock:
            r = await self.collection.update_one(self.fence(), {'$set': {'runtime.martingale_step': 0,'runtime.sequence_loss_amount':0.0}})
            if r.matched_count != 1:
                raise RuntimeError('lease_lost')
            self.doc['runtime']['martingale_step'] = 0
            self.doc['runtime']['sequence_loss_amount'] = 0.0

    async def reset_stats(self):
        async with self.lock:
            if self.doc['runtime'].get('pending'):raise ValueError('لا يمكن التصفير قبل حسم الصفقة المعلّقة')
            new=deepcopy(self.doc)
            new['session_stats']=empty_stats();new['account_stats']={m:empty_stats() for m in MODES}
            new['runtime'].update(martingale_step=0,sequence_loss_amount=0.0,sizing_signature=None,
                recovery={m:reset_state(selected_state(new,m)) for m in MODES})
            for record in new.get('accounts',{}).values():
                record.update(stats=empty_stats(),recovery=reset_state(record['recovery']),
                    martingale_step=0,sizing_signature=None,sequence_loss_amount=0.0)
            r=await self.collection.update_one(self.fence(),{'$set':{k:new[k] for k in ('session_stats','account_stats','runtime','accounts')}})
            if r.matched_count!=1:raise RuntimeError('lease_lost')
            self.doc=new

    async def close(self):
        if self.client:
            await self.client.close()
