"""Saved identities, keyed by UID + REAL/PRACTICE (never by session token).
Mode statistics remain a compatibility VIEW for the currently selected identity.
Snapshots are banked atomically on account switching; no active order may switch.
"""
from copy import deepcopy
import re


def account_key(uid,mode):return f'{mode}_{uid}'


def safe_name(value):
    text=''.join(c for c in str(value or '') if c.isprintable() and c not in '<>')
    return re.sub(r'\s+',' ',text).strip()[:36]


def credential_key(mode):return 'real_account' if mode=='REAL' else 'demo_account'


def make_record(credentials,mode,name=''):
    from cloud_settings import auth_frame,empty_stats,valid_url
    from account_details import credential_uid
    from recovery import state_defaults
    from work_cycle import cycle_state
    credentials=deepcopy(credentials)
    credentials['ssid']=auth_frame(credentials['ssid'],mode,int(credentials.get('uid') or 0))
    credentials['ws_url']=valid_url(credentials['ws_url'])
    uid=credential_uid(credentials,mode)
    if not uid:raise ValueError('يلزم UID فعلي للحساب')
    credentials['uid']=int(uid)
    return dict(uid=uid,mode=mode,name=safe_name(name),credentials=credentials,stats=empty_stats(),
                recovery=state_defaults(),martingale_step=0,sizing_signature=None,cycle=cycle_state())


def migrate(doc):
    """Import configured v20 accounts without altering historical totals. If UID
    is absent, retain its legacy view; NEVER attach its history to a new UID.
    """
    from account_details import credential_uid
    from recovery import selected_state
    from work_cycle import cycle_state
    records=doc.setdefault('accounts',{})
    selected=doc.setdefault('active_accounts',{'PRACTICE':None,'REAL':None})
    for mode in ('PRACTICE','REAL'):
        credentials=doc[credential_key(mode)]
        uid=credential_uid(credentials,mode)
        if not uid:continue
        key=account_key(uid,mode)
        if key in records and records[key].get('deleted'):continue
        if key not in records:
            record=make_record(credentials,mode)
            record['stats']=deepcopy(doc['account_stats'][mode])
            record['recovery']=deepcopy(selected_state(doc,mode))
            if mode==doc['account_mode']:
                record.update(martingale_step=doc['runtime'].get('martingale_step',0),
                              sizing_signature=deepcopy(doc['runtime'].get('sizing_signature')),
                              cycle=deepcopy(doc['runtime'].get('cycle',cycle_state())))
            records[key]=record
        if not selected.get(mode):selected[mode]=key
    doc.setdefault('active_account_id',selected.get(doc['account_mode']))
    if not doc.get('active_account_id'):doc['active_account_id']=selected.get(doc['account_mode'])
    return doc


def bank_active(doc):
    key=doc.get('active_account_id');record=doc.get('accounts',{}).get(key)
    if record:
        mode=record['mode']
        record['stats']=deepcopy(doc['account_stats'][mode])
        record['recovery']=deepcopy(doc['runtime']['recovery'][mode])
        record['martingale_step']=doc['runtime'].get('martingale_step',0)
        record['sizing_signature']=deepcopy(doc['runtime'].get('sizing_signature'))
        record['cycle']=deepcopy(doc['runtime']['cycle'])
        record['manual_pause']=deepcopy(doc['runtime'].get('manual_pause',{}))
        record['sequence_loss_amount']=doc['runtime'].get('sequence_loss_amount',0.0)


def select_document(doc,key):
    new=deepcopy(doc);bank_active(new)
    if key not in new.get('accounts',{}) or new['accounts'][key].get('deleted'):raise ValueError('الحساب غير موجود؛ حدّث القائمة')
    record=new['accounts'][key];mode=record['mode']
    new['account_mode']=mode;new['active_account_id']=key;new['active_accounts'][mode]=key
    new[credential_key(mode)]=deepcopy(record['credentials'])
    new['account_stats'][mode]=deepcopy(record['stats'])
    new['runtime']['recovery'][mode]=deepcopy(record['recovery'])
    new['runtime']['martingale_step']=record['martingale_step']
    new['runtime']['sizing_signature']=deepcopy(record['sizing_signature'])
    new['runtime']['cycle']=deepcopy(record['cycle'])
    from manual_pause import pause_defaults
    new['runtime']['manual_pause']=deepcopy(record.get('manual_pause') or pause_defaults())
    return new


def account_label(record,active=False):
    mode='حقيقي' if record['mode']=='REAL' else 'تجريبي'
    return ('✅ ' if active else '')+f"{record['uid']} · {mode}"+(f" · {record['name']}" if record.get('name') else '')


def registry_text(doc,po=None):
    from account_details import account_text
    return '👤 إدارة الحسابات\n'+account_text(doc,po)+f"\nالحسابات المحفوظة: {sum(not record.get('deleted') for record in doc.get('accounts',{}).values())}\nاختر حساباً من القائمة أو أضف حساباً جديداً."
