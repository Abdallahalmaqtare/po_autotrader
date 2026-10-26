"""Extend v20 only for >=60s: immutable duration, expiry + bounded settlement.
A pause never cancels this wait. Missing post-expiry data is unknown, not loss.
"""
import asyncio,math,time
import config
class LongResultMixin:
    result_clock=staticmethod(time.monotonic)
    async def place_trade(self,direction,amount=None):
        duration=int(config.DURATION_SECONDS);began=self.result_clock()
        ok=await super().place_trade(direction,amount)
        self.long_order=dict(duration=duration,expires=began+duration,before=self._balance_before,
            debit_seen=False,last_value=self.balance,changed_at=began)
        if ok and self._balance_before is not None and self.balance is not None:
            stake=float(amount if amount is not None else config.TRADE_AMOUNT)
            self.long_order['debit_seen']=abs(self.balance-(self._balance_before-stake))<=1e-6
        return ok
    async def wait_for_result(self,timeout=8):
        order=getattr(self,'long_order',None)
        if not order or order['duration']<60:return await super().wait_for_result(timeout)
        before=order['before'];clock=self.result_clock
        if before is None or not math.isfinite(float(before)):return None
        settle=max(1,float(config.BALANCE_SETTLE_SECONDS))
        extra=max(0,float(timeout)-order['duration'])
        start=order['expires']+extra+settle
        end=start+max(float(config.LONG_RESULT_GRACE_SECONDS),float(config.LOSS_RECHECK_SECONDS))
        while clock()<start:await self.result_sleep(min(1,start-clock()))
        while True:
            now=clock();after=self.balance
            fresh=getattr(self,'balance_at',0)>=order['expires']
            quiet=now-order.get('changed_at',now)>=settle
            if self.connected and fresh and after is not None and math.isfinite(float(after)) and quiet:
                delta=float(after)-float(before)
                if delta>1e-6:return 'win'
                if abs(delta)<=1e-6 and order['debit_seen']:return 'draw'
                if now>=end and delta< -1e-6:return 'loss'
            if now>=end:return None
            await self.result_sleep(min(1,end-now))
    async def result_sleep(self,seconds):await asyncio.sleep(max(0,seconds))
    def observe_long_balance(self,value,at):
        order=getattr(self,'long_order',None)
        if order and order.get('before') is not None:
            if order.get('last_value')!=value:order['changed_at']=at
            order['last_value']=value
            if value<order['before']-1e-6:order['debit_seen']=True
