"""Observed-screen MTProto navigation. Never click a historical keyboard.
No callback payloads are invented: click only actual buttons returned by Telegram.
"""
import asyncio
import re
import time
from payout_guard import symbol


class NavigationError(Exception):
    pass


def clean(text):
    return re.sub(r'[\u200b-\u200f\ufe0f\u202a-\u202e\u2066-\u2069]','',str(text or '')).strip()


def asset_from_text(text):
    matches=re.findall(r'\b([A-Z]{3}\s*/\s*[A-Z]{3}\s*(?:\(\s*OTC\s*\)|OTC))',text or '',re.I)
    values={symbol(x) for x in matches}
    return next(iter(values)) if len(values)==1 else None


def duration_from_text(text):
    match=re.search(r'\b(\d+)\s*(sec(?:onds?)?|s|minutes?|mins?)\b',text or '',re.I)
    if not match: return None
    return int(match[1])*(60 if match[2].lower().startswith('m') else 1)


def duration_label(seconds):
    return f'S{seconds}' if seconds<60 else f'M{seconds//60}'


class MetaNavigator:
    def __init__(self,client,entity,timeout=15):
        self.client=client; self.entity=entity; self.timeout=timeout
        self.cancel=None

    def check(self):
        if self.cancel is not None and self.cancel.is_set(): raise asyncio.CancelledError

    async def screen(self):
        self.check()
        async for msg in self.client.iter_messages(self.entity,limit=8):
            if not getattr(msg,'out',False): return msg
        raise NavigationError('META_NO_SCREEN')

    def buttons(self,msg):
        return [btn for row in (getattr(msg,'buttons',None) or []) for btn in row]

    def button(self,msg,label):
        return next((b for b in self.buttons(msg) if clean(b.text).casefold()==label.casefold()),None)

    def assets(self,msg):
        return {symbol(clean(b.text)):b for b in self.buttons(msg) if symbol(clean(b.text))}

    def fingerprint(self,msg):
        return (getattr(msg,'id',None),getattr(msg,'raw_text',''),
                tuple(clean(b.text) for b in self.buttons(msg)))

    async def click(self,msg,button,transition=True):
        self.check()
        # A URL button must never be opened instead of issuing a bot callback.
        if getattr(button,'url',None): raise NavigationError('META_URL_BUTTON')
        before=self.fingerprint(msg)
        try:
            await asyncio.wait_for(msg.click(text=button.text),timeout=self.timeout)
        except asyncio.TimeoutError:
            pass  # callback may have succeeded; observe, never resend blindly
        except Exception as exc:
            if type(exc).__name__!='BotResponseTimeoutError': raise
        if not transition: return
        deadline=time.monotonic()+self.timeout
        while time.monotonic()<deadline:
            self.check()
            current=await self.screen()
            if self.fingerprint(current)!=before: return current
            await asyncio.sleep(.15)
        raise NavigationError('META_SCREEN_TIMEOUT')

    async def asset_screen(self):
        started=False
        for _ in range(12):
            try:
                msg=await self.screen()
            except NavigationError:
                if started: raise
                started=True
                await self.client.send_message(self.entity,'/start')
                await asyncio.sleep(.2)
                continue
            if self.assets(msg): return msg
            if (btn:=self.button(msg,'Trade')) is not None:
                await self.click(msg,btn); continue
            if (btn:=self.button(msg,'OTC')) is not None:
                await self.click(msg,btn); continue
            if (btn:=self.button(msg,'Back')) is not None:
                await self.click(msg,btn); continue
            if started: raise NavigationError('META_MENU_UNKNOWN')
            # Recover a fresh bot conversation through the real /start command.
            started=True
            before=self.fingerprint(msg)
            await self.client.send_message(self.entity,'/start')
            deadline=time.monotonic()+self.timeout
            while time.monotonic()<deadline:
                await asyncio.sleep(.15)
                current=await self.screen()
                if self.fingerprint(current)!=before: break
        raise NavigationError('META_BACK_LIMIT')

    def page(self,msg):
        for btn in self.buttons(msg):
            m=re.fullmatch(r'(\d+)\s*/\s*(\d+)',clean(btn.text))
            if m: return int(m[1]),int(m[2])
        return None

    def arrow(self,msg,right):
        labels=('>','›','❯','▶','➡','→') if right else ('<','‹','❮','◀','⬅','←')
        return next((b for b in self.buttons(msg) if clean(b.text) in labels),None)

    async def first_page(self,msg):
        seen=set()
        for _ in range(20):
            key=self.fingerprint(msg)
            if key in seen: raise NavigationError('META_PAGE_LOOP')
            seen.add(key)
            if self.page(msg) and self.page(msg)[0]==1: return msg
            if not (btn:=self.arrow(msg,False)): return msg
            msg=await self.click(msg,btn)
        raise NavigationError('META_PAGE_LIMIT')

    async def available(self,cancel=None):
        self.cancel=cancel
        msg=await self.first_page(await self.asset_screen())
        seen=set(); available=set()
        for _ in range(20):
            key=self.fingerprint(msg)
            if key in seen: break
            seen.add(key); available.update(self.assets(msg))
            if self.page(msg) and self.page(msg)[0]>=self.page(msg)[1]: return available
            btn=self.arrow(msg,True)
            if btn is None: return available
            msg=await self.click(msg,btn)
        raise NavigationError('META_PAGE_LIMIT')

    async def configure(self,asset,duration,before_confirm,cancel=None):
        """True = Confirm requested a FIRST signal; False = matching final card."""
        self.cancel=cancel
        try:
            msg=await self.screen()
        except NavigationError:
            msg=await self.asset_screen()
        text=getattr(msg,'raw_text','') or ''
        if (self.button(msg,'New signal') and asset_from_text(text)==asset
                and duration_from_text(text)==duration): return False
        msg=await self.first_page(await self.asset_screen())
        seen=set()
        for _ in range(20):
            key=self.fingerprint(msg)
            if key in seen: raise NavigationError('META_PAGE_LOOP')
            seen.add(key)
            btn=self.assets(msg).get(asset)
            if btn is not None:
                msg=await self.click(msg,btn); break
            if self.page(msg) and self.page(msg)[0]>=self.page(msg)[1]: raise NavigationError('META_ASSET_UNAVAILABLE')
            if not (btn:=self.arrow(msg,True)): raise NavigationError('META_ASSET_UNAVAILABLE')
            msg=await self.click(msg,btn)
        else: raise NavigationError('META_PAGE_LIMIT')
        text=getattr(msg,'raw_text','') or ''
        if asset_from_text(text)!=asset: raise NavigationError('META_ASSET_MISMATCH')
        btn=self.button(msg,duration_label(duration))
        if btn is None: raise NavigationError('META_DURATION_UNAVAILABLE')
        msg=await self.click(msg,btn)
        text=getattr(msg,'raw_text','') or ''
        if asset_from_text(text)!=asset or duration_from_text(text)!=duration:
            raise NavigationError('META_CONFIRM_MISMATCH')
        btn=self.button(msg,'Confirm')
        if btn is None: raise NavigationError('META_CONFIRM_MISSING')
        before_confirm()
        await self.click(msg,btn,transition=False)
        return True

    async def new_signal(self,before_click):
        msg=await self.screen()
        btn=self.button(msg,'New signal')
        if btn is None: raise NavigationError('META_NEW_SIGNAL_MISSING')
        before_click()
        await self.click(msg,btn,transition=False)
