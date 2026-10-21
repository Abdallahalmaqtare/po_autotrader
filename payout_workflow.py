"""Control-layer payout guard and MetaTrading selection; no order API here."""
import asyncio
from copy import deepcopy
from contextlib import suppress
import time
import config
from diagnostics import SafeIssue
from meta_navigation import NavigationError
from payout_guard import allowed, policy_text


class PayoutWorkflow:
    def payout_book(self):
        po=self.po
        if not po or not po.connected or po.mode!=self.repo.doc['account_mode']: return None
        return getattr(po,'payout_book',None)

    def payout_status(self):
        return policy_text(self.repo.doc,self.payout_book(),self.payout_note)

    async def synchronize_selection(self):
        """No trades. While OFF, Confirm may prepare a first signal; Start still required."""
        await self._telegram()
        t=deepcopy(self.repo.doc['trading'])
        self.stage='meta_sync'
        try:
            await self.listener.synchronize(t['asset'],t['duration_seconds'])
        except NavigationError as exc:
            print(f'[META] sync not completed: {exc}',flush=True)
            raise SafeIssue('META_SYNC_FAILED') from None
        self.stage='meta_ready'
        return 'تمت مزامنة الزوج والمدة مع MetaTrading؛ التداول لا يبدأ إلا بأمر Start.'

    def entry_eligible(self,t,policy,mode):
        if self.stop_event.is_set(): return False
        if mode!=self.repo.doc['account_mode'] or t!=self.repo.doc['trading'] or policy!=self.repo.doc['payout']: return False
        if not self.listener.verifies(t['asset'],t['duration_seconds'],max_age=max(10,t['entry_delay_seconds']+2)): return False
        if policy['mode']=='OFF': return True
        book=self.payout_book()
        return book is not None and allowed(book,t['asset'],t['duration_seconds'],policy)

    async def wait_payout_change(self,t,policy):
        book=self.payout_book()
        if policy['mode']=='OFF' or book is None:
            await asyncio.Event().wait()
        while True:
            if not allowed(book,t['asset'],t['duration_seconds'],policy): return
            book.updated.clear()
            if not allowed(book,t['asset'],t['duration_seconds'],policy): return
            try:
                await asyncio.wait_for(book.updated.wait(),1)
            except asyncio.TimeoutError:
                pass

    async def guard_payout(self,t,policy,mode,wait=True):
        if policy['mode']=='OFF':
            self.payout_note=''
            return True
        book=self.payout_book()
        quote=book.quotes.get(t['asset']) if book else None
        stale=quote is not None and time.monotonic()-quote.at>policy['max_age_seconds']
        refresh=getattr(self.po,'refresh_payouts',None)
        if wait and stale and callable(refresh) and time.monotonic()-self.payout_refresh_at>30:
            self.payout_refresh_at=time.monotonic()
            self.stage='payout_refresh'
            try:
                await refresh()
            except (TimeoutError,ConnectionError,OSError):
                await self._pause(2)
            return False
        until=time.monotonic()+min(config.CONNECT_TIMEOUT_SECONDS,5) if wait else time.monotonic()
        while book is not None and book.rate(t['asset'],t['duration_seconds'],policy['max_age_seconds']) is None and time.monotonic()<until:
            if self.stop_event.is_set() or t!=self.repo.doc['trading'] or policy!=self.repo.doc['payout']: return False
            if policy['mode']=='SWITCH' and book.candidates(t['duration_seconds'],policy,exclude=(t['asset'],)): break
            await self._pause(.1)
        if mode!=self.repo.doc['account_mode'] or t!=self.repo.doc['trading'] or policy!=self.repo.doc['payout']: return False
        rate=book.rate(t['asset'],t['duration_seconds'],policy['max_age_seconds']) if book else None
        if rate is not None and rate>policy['threshold']:
            self.payout_note=f'عائد الزوج {rate:g}%'
            return True
        if policy['mode']=='STOP':
            self.payout_note=('العائد غير متاح أو قديم' if rate is None else
                             f"العائد {rate:g}% ≤ {policy['threshold']:g}%")
            self.stage='payout_stopped'; self.stop_event.set()
            print('[PAYOUT] trading stopped: '+self.payout_note,flush=True)
            if self.notify:
                with suppress(Exception):
                    await asyncio.wait_for(self.notify('🛑 حماية عائد الزوج\n'+self.payout_note),5)
            return False
        # SWITCH: no rates / no shared pair means WAIT, never bypass the guard.
        candidates=book.candidates(t['duration_seconds'],policy,exclude=(t['asset'],)) if book else []
        if not candidates:
            self.stage='payout_wait'
            self.payout_note='لا يوجد بديل بعائد حديث مناسب؛ لا دخول.'
            await self._pause(2)
            return False
        self.stage='meta_scan'
        try:
            available=await self.listener.available_assets(self.stop_event)
        except NavigationError as exc:
            self.payout_note='تعذّر فحص الأزواج في MetaTrading؛ لا دخول.'
            self.stage='meta_retry'
            print(f'[META] catalog scan pending: {exc}',flush=True)
            await self._pause(2)
            return False
        if self.stop_event.is_set() or t!=self.repo.doc['trading'] or mode!=self.repo.doc['account_mode'] or policy!=self.repo.doc['payout']: return False
        # Re-read quotes after paging; scanning cannot make old quotes fresh.
        ranked=book.candidates(t['duration_seconds'],policy,exclude=(t['asset'],))
        for asset,payout in ranked:
            if asset not in available: continue
            self.stage='meta_sync'
            try:
                await self.listener.synchronize(asset,t['duration_seconds'],self.stop_event)
            except NavigationError as exc:
                print(f'[META] candidate unavailable: {asset}; {exc}',flush=True)
                continue
            if self.stop_event.is_set() or t!=self.repo.doc['trading'] or mode!=self.repo.doc['account_mode'] or policy!=self.repo.doc['payout']: return False
            now=book.rate(asset,t['duration_seconds'],policy['max_age_seconds'])
            if now is None or now<policy['switch_min'] or now<=policy['threshold']:
                continue
            async with self.operations:
                if (self.stop_event.is_set() or t!=self.repo.doc['trading']
                        or mode!=self.repo.doc['account_mode'] or policy!=self.repo.doc['payout']): return False
                now=book.rate(asset,t['duration_seconds'],policy['max_age_seconds'])
                if now is None or now<policy['switch_min'] or now<=policy['threshold']: continue
                await self.repo.update({'trading.asset':asset})
                self.payout_note=f'تم التحويل إلى {asset} بعائد {now:g}%'
                print('[PAYOUT] '+self.payout_note,flush=True)
            # Run a fresh cycle with the changed snapshot. Its first Confirm signal
            # is already queued, so it must NOT press New signal first.
            return False
        self.stage='payout_wait'
        self.payout_note='لا يوجد زوج مشترك مناسب للعائد والمدة؛ لا دخول.'
        await self._pause(2)
        return False
