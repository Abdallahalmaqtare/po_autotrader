"""Exact request/deal identity tracking. Never infer LOSS from the opening debit.
Unrecognized payloads/ambiguous IDs remain unresolved, not guessed.
"""
import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math

OPEN_EVENTS = {'successopenOrder', 'openOrderSuccess', 'orderOpened'}
CLOSE_EVENTS = {'successcloseOrder', 'updateClosedDeals', 'closedDeals', 'closedDeal', 'closeOption', 'orderClosed'}


def numeric(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(Decimal(str(value)))
        return value if math.isfinite(value) else None
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None


def nodes(value, inherited_account=None):
    if isinstance(value, dict):
        row = dict(value)
        if 'isDemo' not in row and inherited_account is not None:
            row['isDemo'] = inherited_account
        yield row
        flag = row.get('isDemo')
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from nodes(child, flag)
    elif isinstance(value, list):
        for child in value:
            yield from nodes(child, inherited_account)


def identifier(row, names):
    for name in names:
        val = row.get(name)
        if val is not None and not isinstance(val, (dict, list, bool)):
            return str(val)
    return None


def account_matches(row, mode):
    if 'isDemo' not in row:
        return True
    raw = row['isDemo']
    if isinstance(raw, (int, float, bool)) and raw in (0, 1):
        flag = int(raw)
    else:
        flag = {'true':1,'false':0,'1':1,'0':0}.get(str(raw).lower().strip())
    return flag is not None and flag == (1 if mode == 'PRACTICE' else 0)


@dataclass(frozen=True)
class ClosedResult:
    result: str
    net_profit: float | None
    deal_id: str | None
    request_id: str
    source: str = 'matched_close'


class OrderTracker:
    def __init__(self):
        self.request_id = None
        self.deal_id = None
        self.amount = 0.0
        self.asset = ''
        self.mode = 'PRACTICE'
        self.future = None
        self.early_closes = OrderedDict()
        self.last_result = None
        self.unmatched_closes = 0

    def begin(self, request_id, amount, asset, mode):
        self.request_id = str(request_id)
        self.deal_id = None
        self.amount = float(amount)
        self.asset = asset
        self.mode = mode
        self.last_result = None
        self.future = asyncio.get_running_loop().create_future()
        self.early_closes.clear()
        return self.future

    def compatible(self, row):
        if not account_matches(row, self.mode):
            return False
        if row.get('asset') and row['asset'] != self.asset:
            return False
        if 'amount' in row:
            amount = numeric(row['amount'])
            if amount is None or abs(amount-self.amount) > 1e-6:
                return False
        return True

    def feed(self, event):
        if self.future is None or self.future.done() or not isinstance(event, list) or len(event)<2:
            return
        kind, payload = event[:2]
        if not isinstance(kind, str):
            return
        if kind in OPEN_EVENTS:
            for row in nodes(payload):
                request = identifier(row, ('requestId','request_id'))
                deal = identifier(row, ('id','dealId','deal_id','orderId'))
                if request == self.request_id and deal and self.compatible(row):
                    self.deal_id = deal
                    if deal in self.early_closes:
                        self.finish(self.early_closes[deal])
        elif kind in CLOSE_EVENTS:
            for row in nodes(payload):
                if not self.compatible(row):
                    continue
                request = identifier(row, ('requestId','request_id'))
                deal = identifier(row, ('id','dealId','deal_id','orderId'))
                # Conflicting request ID overrides even a matching deal ID.
                if request is not None and request != self.request_id:
                    continue
                if self.deal_id is not None and deal is not None and deal != self.deal_id and request == self.request_id:
                    continue
                if request == self.request_id or (self.deal_id is not None and deal == self.deal_id):
                    self.finish(row)
                elif deal:
                    self.unmatched_closes += 1
                    self.early_closes[deal] = row
                    if len(self.early_closes)>32:
                        self.early_closes.popitem(last=False)

    def finish(self, row):
        if self.future is None or self.future.done():
            return
        # Only amount fields explicitly meaning money returned, NOT 'payout'
        # (often a percentage), can determine gross-return based results.
        returned = next((numeric(row[k]) for k in ('returnAmount','returnedAmount','returned_amount','grossReturn')
                         if k in row), None)
        net = numeric(row.get('netProfit', row.get('net_profit', row.get('profit'))))
        status = str(row.get('result', row.get('status', ''))).lower()
        explicit = {'win':'win','won':'win','loss':'loss','lost':'loss',
                    'draw':'draw','tie':'draw','refund':'draw'}.get(status)
        if returned is not None and returned >= 0:
            net = returned - self.amount
        if net is not None:
            derived = 'win' if net>1e-6 else 'loss' if net < -1e-6 else 'draw'
            if explicit is not None and explicit != derived:
                return  # conflicting payload: do not guess
            result = derived
            # An oversized positive value might be gross profit, not net profit.
            # Direction can be known but financial attribution stays unknown.
            financial = net if -self.amount-1e-6 <= net <= self.amount*1.01+1e-6 else None
        elif explicit is not None:
            result = explicit
            financial = -self.amount if result=='loss' else 0.0 if result=='draw' else None
        else:
            return
        self.last_result = ClosedResult(result, financial,
            identifier(row, ('id','dealId','deal_id','orderId')), self.request_id)
        self.future.set_result(self.last_result)

    async def wait(self, timeout):
        if self.future is None:
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(self.future), max(0.001,float(timeout)))
        except asyncio.TimeoutError:
            return None
