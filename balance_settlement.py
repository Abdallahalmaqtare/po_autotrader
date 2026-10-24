"""Bounded compatibility fallback for a witnessed debit then post-expiry credit.
It does NOT call a debit-only balance a loss, and unchanged/stale balances aren't
proof of draw. Exact broker results have priority. Use one trader per account;
external deposits/trades can make balance inference ambiguous even with guards.
"""
from decimal import Decimal
import time
from trade_settlement import number,EPS


class BalanceEvidence:
    def __init__(self,order,balance,balance_at,now=None):
        self.order=order.copy();self.clock=now or time.monotonic
        self.started=self.clock();self.before=number(balance);self.stake=number(order['amount'])
        self.expires=self.started+max(0,float(order['duration_seconds']))
        self.fresh=bool(balance_at and 0<=self.started-balance_at<=30)
        self.debit_at=None;self.credit_at=None;self.latest=None;self.latest_at=0.0;self.last_frame_at=0.0
        self.ambiguous=False;self.receipt=None

    def feed(self,value,at=None):
        at=self.clock() if at is None else at;n=number(value)
        if n is None or self.before is None or self.stake is None:return
        if n<self.before-self.stake-EPS or n>self.before+self.stake+EPS:
            self.ambiguous=True
        if self.debit_at is None and abs(n-(self.before-self.stake))<=EPS:
            self.debit_at=at
        if self.debit_at is not None and n>=self.before-EPS and at>=self.expires:
            self.credit_at=at
        self.last_frame_at=at
        if self.latest is None or abs(n-self.latest)>EPS:self.latest_at=at
        self.latest=n

    def evaluate(self,connected=True,quiet=.5,loss_ready=False,open_ack=False):
        now=self.clock()
        if (not connected or not self.fresh or self.ambiguous or self.debit_at is None
            or now<self.expires+quiet
            or now-self.latest_at<quiet or self.latest is None):return None
        delta=self.latest-self.before
        if abs(delta)<=EPS and self.credit_at is not None:result='draw';delta=Decimal(0);source='balance_refund'
        elif EPS<delta<=self.stake+EPS and self.credit_at is not None:result='win';source='balance_credit'
        elif abs(delta+self.stake)<=EPS and loss_ready and open_ack and self.last_frame_at>=self.expires+quiet:
            result='loss';source='balance_loss'
        else:return None
        self.receipt=dict(order_id=self.order['id'],request_id=str(self.order['request_id']),
            broker_id='',mode=self.order['mode'],account_id=self.order.get('account_id'),
            result=result,net_pnl=float(delta.quantize(EPS)),source=source,
            evidence=dict(before=float(self.before),after=float(self.latest),stake=float(self.stake),
                debit_at=self.debit_at,credit_at=self.credit_at if self.credit_at is not None else self.last_frame_at,expires=self.expires,
                last_frame_at=self.last_frame_at,open_ack=bool(open_ack),final_window=bool(loss_ready),
                evaluated_at=now,fresh=True,ambiguous=False,connected=True))
        return self.receipt


def validate_balance_receipt(receipt,order):
    e=receipt.get('evidence')
    if not isinstance(e,dict) or e.get('fresh') is not True or e.get('ambiguous') is not False or e.get('connected') is not True:return False
    values={k:number(e.get(k)) for k in ('before','after','stake','debit_at','credit_at','expires','evaluated_at')}
    if any(v is None for v in values.values()):return False
    if values['stake']!=number(order['amount']):return False
    if not values['debit_at']<=values['credit_at']<=values['evaluated_at']:return False
    if values['credit_at']<values['expires'] or values['evaluated_at']<values['expires']:return False
    delta=values['after']-values['before']
    claimed=number(receipt.get('net_pnl'))
    if claimed is None or abs(delta-claimed)>EPS:return False
    return ((receipt['source']=='balance_refund' and receipt['result']=='draw' and abs(delta)<=EPS)
        or (receipt['source']=='balance_credit' and receipt['result']=='win' and EPS<delta<=values['stake']+EPS)
        or (receipt['source']=='balance_loss' and receipt['result']=='loss' and abs(delta+values['stake'])<=EPS
            and e.get('open_ack') is True and e.get('final_window') is True
            and number(e.get('last_frame_at')) is not None and number(e['last_frame_at'])>=values['expires']))
