"""Strict MetaTrading selection and request correlation layered on v18 parser."""
import asyncio
from datetime import datetime, timezone
from telegram_signal import SignalListener, parse_direction
from meta_navigation import MetaNavigator, NavigationError, asset_from_text, duration_from_text
import config


class SynchronizedSignalListener(SignalListener):
    def __init__(self,client):
        super().__init__(client)
        self.nav=None
        self.nav_lock=asyncio.Lock()
        self.prepared=None
        self.first_signal=False
        self.expected=None
        self.armed_at=None
        self.last_text=''
        self.last_received=0.0

    async def init(self):
        self.bot_entity=await self.client.get_entity(config.SIGNAL_BOT_USERNAME)
        self.nav=MetaNavigator(self.client,self.bot_entity)
        from telethon import events
        @self.client.on(events.NewMessage(from_users=self.bot_entity))
        @self.client.on(events.MessageEdited(from_users=self.bot_entity))
        async def incoming(event):
            if self.armed_at is None: return
            text=event.raw_text or ''
            if parse_direction(text) is None: return
            if self.expected is None: return
            asset,duration=self.expected
            if asset_from_text(text)!=asset or duration_from_text(text)!=duration: return
            stamp=getattr(event.message,'edit_date',None) or getattr(event.message,'date',None)
            if stamp is not None:
                if stamp.tzinfo is None: stamp=stamp.replace(tzinfo=timezone.utc)
                # Telegram timestamps have second resolution.
                if stamp.timestamp() < self.armed_at-1: return
            key=(getattr(event,'id',None),text)
            if key in self._seen_pending: return
            self._seen_pending.add(key)
            await self._pending.put(text)

    def arm(self):
        self.drain_pending()
        self.last_text=''
        self.armed_at=datetime.now(timezone.utc).timestamp()
        self._press_time=datetime.now(timezone.utc)

    def disarm(self):
        self.armed_at=None
        self.drain_pending()
        self.first_signal=False

    async def synchronize(self,asset,duration,cancel=None):
        async with self.nav_lock:
            self.disarm()
            self.expected=(asset,duration)
            self.prepared=None
            self.first_signal=await self.nav.configure(asset,duration,self.arm,cancel)
            self.prepared=(asset,duration)
            print(f'[META] synchronized {asset} {duration}s; first_signal={self.first_signal}',flush=True)

    async def available_assets(self,cancel=None):
        async with self.nav_lock:
            self.disarm(); self.prepared=None
            return await self.nav.available(cancel)

    async def next_direction(self,asset,duration,cancel=None):
        if self.first_signal and self.armed_at is not None and datetime.now(timezone.utc).timestamp()-self.armed_at>10:
            self.disarm(); self.prepared=None
        if self.prepared!=(asset,duration):
            await self.synchronize(asset,duration,cancel)
        if not self.first_signal:
            async with self.nav_lock:
                self.nav.cancel=cancel
                # Selection is verified on the live keyboard before requesting.
                msg=await self.nav.screen()
                text=getattr(msg,'raw_text','') or ''
                if asset_from_text(text)!=asset or duration_from_text(text)!=duration:
                    self.prepared=None
                    raise NavigationError('META_SELECTION_CHANGED')
                await self.nav.new_signal(self.arm)
        self.first_signal=False
        try:
            text=await asyncio.wait_for(self._pending.get(),timeout=config.SIGNAL_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self.disarm(); self.prepared=None
            raise NavigationError('META_SIGNAL_TIMEOUT') from None
        self.armed_at=None
        self.last_text=text
        self.last_received=asyncio.get_running_loop().time()
        return parse_direction(text)

    def verifies(self,asset,duration,max_age=10):
        return (self.expected==(asset,duration) and asset_from_text(self.last_text)==asset
                and duration_from_text(self.last_text)==duration
                and asyncio.get_running_loop().time()-self.last_received<=max_age)
