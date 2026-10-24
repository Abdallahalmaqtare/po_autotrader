"""Order-correlated settlement. Balance messages NEVER classify a trade.

Match requestId, or a broker ID previously bound by its exact requestId. Asset,
amount, mode and UID are additional guards, not substitutes for an identifier.
Unrecognised/contradictory financial schemas remain unresolved. No guessed payout.
"""
import asyncio
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re

EPS=Decimal('0.000001')


def number(value):
    if isinstance(value,bool) or value is None:return None
    try:
        n=Decimal(str(value))
        return n if n.is_finite() else None
    except (InvalidOperation,ValueError,TypeError):return None


def norm(value):return re.sub(r'[^a-z0-9]','',str(value).lower())


def flag(value):
    if isinstance(value,bool):return int(value)
    if value in (0,1,'0','1'):return int(value)
    if str(value).lower() in ('true','false'):return int(str(value).lower()=='true')
    return None


def nodes(payload):
    if isinstance(payload,dict):
        yield payload
        for k in ('deals','orders','data','result','order','deal','closedDeals'):
            if isinstance(payload.get(k),dict):
                context={x:payload[x] for x in ('requestId','request_id','uid','isDemo') if x in payload}
                yield from nodes({**context,**payload[k]})
            elif isinstance(payload.get(k),list):yield from nodes(payload[k])
    elif isinstance(payload,list):
        for value in payload:yield from nodes(value)


def outcome_finances(deal,stake):
    """Explicit NET fields or explicit gross returns. 'profit' is NET in the
    supported broker schema. A bare zero-profit is ambiguous (loss/refund), so
    require an explicit loss/draw or returned amount instead of inventing a loss.
    """
    amount=number(stake)
    if amount is None or amount<=0:return None
    status=str(deal.get('result',deal.get('status',''))).lower()
    aliases={'won':'win','win':'win','loss':'loss','lose':'loss','lost':'loss',
             'draw':'draw','tie':'draw','refund':'draw','refunded':'draw','equal':'draw','returned':'draw'}
    label=aliases.get(status)
    if deal.get('isDraw') in (True,1,'1','true'):label='draw'
    net_values=[number(deal[k]) for k in ('netProfit','net_profit','profit') if k in deal]
    gross_values=[number(deal[k]) for k in ('returnedAmount','returnAmount','totalReturned','returned_amount') if k in deal]
    if any(v is None for v in net_values+gross_values):return None
    if net_values and any(abs(v-net_values[0])>EPS for v in net_values):return None
    if gross_values and any(abs(v-gross_values[0])>EPS for v in gross_values):return None
    delta=net_values[0] if net_values else (gross_values[0]-amount if gross_values else None)
    if delta is None:
        if label=='loss':delta=-amount
        elif label=='draw':delta=Decimal(0)
        else:return None  # a win with no exact monetary amount is not a ledger entry
    # Known zero-profit + explicit loss: profit was winnings-only, return was zero.
    if abs(delta)<=EPS and label=='loss' and not gross_values:
        delta=-amount
    if gross_values and abs((gross_values[0]-amount)-delta)>EPS:return None
    if delta < -amount-EPS:return None
    result='win' if delta>EPS else 'loss' if delta < -EPS else 'draw'
    if label is not None and label!=result:return None
    if result=='draw' and not label and not gross_values and not any(k in deal for k in ('netProfit','net_profit')):
        return None
    return result,float(delta.quantize(EPS))


class DealTracker:
    def __init__(self,order,uid=None):
        self.order=deepcopy(order)
        self.request=str(order['request_id'])
        self.broker_id=str(order.get('broker_id') or '')
        self.uid=str(uid or '')
        self.ready=asyncio.Event()
        self.settlement=None
        self.conflict=False
        self.deferred=[]

    def compatible(self,deal):
        if 'isDemo' in deal and flag(deal['isDemo'])!=(1 if self.order['mode']=='PRACTICE' else 0):return False
        if 'uid' in deal and self.uid and str(deal['uid'])!=self.uid:return False
        asset=deal.get('asset',deal.get('symbol'))
        if asset is not None and norm(asset)!=norm(self.order['asset']):return False
        if 'amount' in deal:
            value=number(deal['amount'])
            if value is None or abs(value-number(self.order['amount']))>EPS:return False
        return True

    def identified(self,deal):
        request=deal.get('requestId',deal.get('request_id'))
        broker=deal.get('id',deal.get('dealId',deal.get('orderId',deal.get('order_id',deal.get('deal_id')))))
        if request is not None and str(request)!=self.request:return False
        if request is not None:return True
        return bool(self.broker_id and broker is not None and str(broker)==self.broker_id)

    def feed(self,event):
        if not isinstance(event,list) or len(event)<2:return False
        name,payload=event[:2]
        # Account guards on a deal envelope also apply to all nested deals.
        if isinstance(payload,dict) and not self.compatible(payload):return False
        event_name=norm(name)
        opening=event_name in ('successopenorder','orderopened','orderdata')
        final=event_name in ('successcloseorder','closeddeals','updatecloseddeals','closeddeal',
                             'closeorder','closeoption','orderclosed','historycloseddeals')
        if not opening and not final:return False
        changed=False
        for deal in nodes(payload):
            if not self.compatible(deal):continue
            if not self.identified(deal):
                if final and deal.get('id') is not None and ('profit' in deal or 'netProfit' in deal or 'returnedAmount' in deal):
                    self.deferred=(self.deferred+[deepcopy(deal)])[-50:]
                continue
            broker=deal.get('id',deal.get('dealId',deal.get('orderId',deal.get('order_id',deal.get('deal_id')))))
            if broker is not None:
                if self.broker_id and str(broker)!=self.broker_id:continue
                self.broker_id=str(broker)
            if opening:continue  # opening profit=0 is NOT a final loss
            amounts=outcome_finances(deal,self.order['amount'])
            if amounts is None:continue
            result,delta=amounts
            item=dict(order_id=self.order['id'],request_id=self.request,broker_id=self.broker_id,
                      mode=self.order['mode'],account_id=self.order.get('account_id'),
                      result=result,net_pnl=delta,source='broker_closed_deal')
            if self.settlement and self.settlement!=item:
                self.conflict=True;self.settlement=None;self.ready.set();return True
            if not self.conflict:
                self.settlement=item;self.ready.set();changed=True
        if opening and self.broker_id and self.deferred:
            waiting,self.deferred=self.deferred,[]
            for deal in waiting:
                if self.identified(deal):changed=self.feed(['closedDeals',deal]) or changed
        return changed
