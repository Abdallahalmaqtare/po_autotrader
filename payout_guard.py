"""Account-scoped broker payout quotes, not signal accuracy or trade profit.
Tuple schema reference: ChipaDevTeam/PocketOptionAPI websocket_client.py
_handle_payout_message: symbol[1], payout[5], tradable[14], expirations[15].
Unrecognized/expired data is UNKNOWN, never a guessed 92%.
"""
from dataclasses import dataclass
import asyncio
import math
import re
import time
from forex_assets import FOREX_OTC


def payout_defaults():
    return dict(mode='OFF', threshold=84.0, switch_min=85.0, max_age_seconds=60.0)


def validate_payout(p):
    if p.get('mode') not in ('OFF','STOP','SWITCH'):
        raise ValueError('وضع حماية العائد غير صالح')
    for key,lo,hi in [('threshold',0,100),('switch_min',0,100),('max_age_seconds',5,600)]:
        value=p.get(key)
        if isinstance(value,bool): raise ValueError('قيمة حماية العائد غير صالحة')
        try: n=float(value)
        except (ValueError,TypeError): raise ValueError('قيمة حماية العائد غير صالحة') from None
        if not math.isfinite(n) or not lo<=n<=hi: raise ValueError('قيمة حماية العائد خارج النطاق')
    if p['mode']=='SWITCH' and p['switch_min']<=p['threshold']:
        raise ValueError('حد الزوج البديل يجب أن يكون أعلى من حد الإيقاف')


def symbol(value):
    text=str(value or '').upper().replace('(OTC)','_OTC').replace(' OTC','_OTC')
    text=re.sub(r'[\s/]','',text)
    return text[:-4]+'_otc' if re.fullmatch(r'[A-Z]{6}_OTC',text) else None


def percent(value):
    if isinstance(value,bool): return None
    try: n=float(str(value).strip().rstrip('%'))
    except (TypeError,ValueError): return None
    return n if math.isfinite(n) and 0<=n<=100 else None


def flag(value):
    if isinstance(value,bool): return value
    if value in (0,1): return bool(value)
    return {'true':True,'false':False,'1':True,'0':False}.get(str(value).lower())


@dataclass
class Quote:
    payouts: dict
    at: float
    tradable: object = None
    durations: object = None
    hint_durations: object = None


class PayoutBook:
    EVENTS={'updateAssets','assets','listAssets','updateAsset','updatePayout','updatePayouts'}
    def __init__(self, mode):
        self.mode=mode
        self.quotes={}
        self.revision=0
        self.updated=asyncio.Event()
        self.events_seen=0
        self.rows_seen=0
        self.last_event=''
        self.last_issue=''

    def clear(self):
        self.quotes.clear(); self.revision+=1; self.updated.set()

    def feed(self,event):
        if not isinstance(event,list) or len(event)<2 or event[0] not in self.EVENTS: return
        self.events_seen+=1;self.last_event=event[0]
        data=event[1]
        for _ in range(5):
            if not isinstance(data,dict):break
            if 'isDemo' in data:
                demo=flag(data['isDemo'])
                if demo is None or demo != (self.mode=='PRACTICE'): return
            nested=next((data[k] for k in ('assets','data','result') if k in data and isinstance(data[k],(dict,list))),None)
            if nested is None:break
            data=nested
        if isinstance(data,dict) and any(k in data for k in ('symbol','asset')):
            entries=[data]
        elif isinstance(data,dict):
            entries=[]
            for key,value in data.items():
                if symbol(key) and isinstance(value,dict): entries.append(dict(value,symbol=key))
                elif symbol(key) and percent(value) is not None: entries.append(dict(symbol=key,payout=value))
        elif isinstance(data,list):
            entries=[data] if len(data)>5 and isinstance(data[1],str) and symbol(data[1]) else data
        else: return
        for item in entries:
            self.rows_seen+=1
            active=None; durations=None; hints=None
            if isinstance(item,list) and len(item)>=6:
                sym=symbol(item[1]); raw=item[5]
                active=flag(item[14]) if len(item)>14 else None
                if len(item)>15 and isinstance(item[15],list):
                    times=[r.get('time') for r in item[15] if isinstance(r,dict) and isinstance(r.get('time'),(int,float))]
                    hints=set(int(x) for x in times) or None
            elif isinstance(item,dict):
                if 'isDemo' in item and flag(item['isDemo']) != (self.mode=='PRACTICE'): continue
                sym=symbol(item.get('symbol',item.get('asset')))
                raw=item.get('payout',item.get('profit',item.get('percent')))
                for key in ('tradable','isActive','is_active','enabled'):
                    if key in item: active=flag(item[key]); break
                explicit=item.get('supported_durations',item.get('durations'))
                if isinstance(explicit,list):
                    vals=[x.get('time') if isinstance(x,dict) else x for x in explicit]
                    durations={int(x) for x in vals if isinstance(x,(int,float))} or None
            else: continue
            if not sym: continue
            rates={}
            if isinstance(raw,dict):
                for key,value in raw.items():
                    rate=percent(value)
                    if str(key).isdigit() and rate is not None: rates[int(key)]=rate
            else:
                rate=percent(raw)
                if rate is not None: rates[None]=rate
            if not rates:
                self.quotes.pop(sym,None)
                self.updated.set()
                continue
            previous=self.quotes.get(sym)
            if event[0] in ('updateAsset','updatePayout','updatePayouts') and previous:
                if active is None: active=previous.tradable
                if durations is None: durations=previous.durations
            self.quotes[sym]=Quote(rates,time.monotonic(),active,durations,hints)
            self.revision+=1
            self.updated.set()

    def rate(self,asset,duration,max_age=60):
        quote=self.quotes.get(asset)
        if quote is None or time.monotonic()-quote.at>max_age or quote.tradable is False: return None
        if quote.durations is not None and duration not in quote.durations: return None
        return quote.payouts.get(duration,quote.payouts.get(None))

    def candidates(self,duration,policy,exclude=()):
        result=[]
        for index,(_,asset) in enumerate(FOREX_OTC):
            rate=self.rate(asset,duration,policy['max_age_seconds'])
            if asset not in exclude and rate is not None and rate>=policy['switch_min'] and rate>policy['threshold']:
                result.append((asset,rate,index))
        return [(asset,rate) for asset,rate,_ in sorted(result,key=lambda r:(-r[1],r[2]))]


class PayoutBlocked(Exception):
    """Known PRE-SEND refusal; never classify it as an uncertain dispatched trade."""


def allowed(book,asset,duration,policy):
    if policy['mode']=='OFF': return True
    rate=book.rate(asset,duration,policy['max_age_seconds'])
    return rate is not None and rate>policy['threshold']


def quote_detail(book,asset,duration,max_age):
    if book is None: return 'DISCONNECTED',None,None
    quote=book.quotes.get(asset)
    if quote is None: return ('NO_TABLE' if not book.quotes else 'ASSET_MISSING'),None,None
    age=max(0,time.monotonic()-quote.at)
    raw=quote.payouts.get(duration,quote.payouts.get(None))
    if age>max_age:return 'STALE',raw,age
    if quote.tradable is False:return 'CLOSED',raw,age
    if quote.durations is not None and duration not in quote.durations:return 'DURATION',raw,age
    if raw is None:return 'DURATION',raw,age
    return 'KNOWN',raw,age


def policy_text(doc,book=None,status=''):
    p=doc['payout'];t=doc['trading']
    labels={'OFF':'◻️ معطلة','STOP':'⏹ إيقاف','SWITCH':'🔄 تبديل تلقائي'}
    code,raw,age=quote_detail(book,t['asset'],t['duration_seconds'],p['max_age_seconds'])
    reasons={'DISCONNECTED':'غير متصل بالمنصة','NO_TABLE':'لم يصل جدول العائد بعد',
             'ASSET_MISSING':'الزوج غير موجود في الجدول المستلم','STALE':'القراءة قديمة',
             'CLOSED':'الزوج مغلق','DURATION':'لا يوجد عائد صريح لهذه المدة','KNOWN':'قراءة حديثة'}
    shown=f'{raw:g}%' if raw is not None else '—'
    lines=['📉 حماية عائد الزوج',f"{labels[p['mode']]} | {t['asset']} | {t['duration_seconds']}ث",
           f'العائد المستلم: {shown}',f"حالة البيانات: {reasons[code]}",
           f"الإجراء عند ≤ {p['threshold']:g}% | البديل ≥ {p['switch_min']:g}%"]
    if age is not None:lines.append(f'عمر القراءة: {age:.0f}ث')
    if status:lines.append(status)
    return '\n'.join(lines)


def payout_diagnostic(book,decoder=None):
    if book is None:return 'تشخيص العائد: لا يوجد اتصال نشط بالحساب المختار.'
    stats=getattr(decoder,'stats',{}) if decoder else {}
    return (f'فحص العائد: أحداث={book.events_seen} | صفوف={book.rows_seen} | أصول={len(book.quotes)}\n'
            f"فك الحزم: ناجحة={stats.get('decoded',0)} | مرفقات={stats.get('attachments',0)} | مرفوضة={stats.get('rejected',0)}")
