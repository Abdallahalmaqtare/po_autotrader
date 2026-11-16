"""AUTO delegates to stable v20 navigator. MANUAL only presses live New signal.
Manual sources may omit metadata; explicit conflicting asset/duration is rejected.
Direction parser is the same UP/DOWN/Higher/Lower parser as v20.
"""
import asyncio
from datetime import datetime,timezone
from synchronized_signals import SynchronizedSignalListener
from meta_navigation import MetaNavigator,NavigationError,asset_from_text,duration_from_text
from telegram_signal import parse_direction
import config


class SourceSignalListener(SynchronizedSignalListener):
    def bind_source(self,spec):
        self.source_username=spec['username']
        self.setup_mode=spec['setup_mode']
        self.button_text=spec['button_text']

    async def init(self):
        if getattr(self,'setup_mode','AUTO')!='MANUAL':
            config.SIGNAL_BOT_USERNAME=self.source_username
            await super().init()
            self.nav.new_signal_label=getattr(self,'button_text','New signal')
            return
        from telethon import events
        self.bot_entity=await self.client.get_entity(self.source_username)
        self.nav=MetaNavigator(self.client,self.bot_entity)
        @self.client.on(events.NewMessage(from_users=self.bot_entity))
        @self.client.on(events.MessageEdited(from_users=self.bot_entity))
        async def incoming(event):
            if self.armed_at is None or self.expected is None:return
            text=event.raw_text or ''
            if parse_direction(text) is None:return
            if not self.metadata_matches(text,*self.expected):return
            message=getattr(event,'message',event)
            stamp=getattr(message,'edit_date',None) or getattr(message,'date',None)
            if stamp is not None:
                if stamp.tzinfo is None:stamp=stamp.replace(tzinfo=timezone.utc)
                if stamp.timestamp()<self.armed_at-1:return
            key=(getattr(event,'id',None),text)
            if key in self._seen_pending:return
            self._seen_pending.add(key)
            await self._pending.put(text)

    def metadata_matches(self,text,asset,duration):
        explicit_asset=asset_from_text(text)
        explicit_duration=duration_from_text(text)
        return ((explicit_asset is None or explicit_asset==asset)
                and (explicit_duration is None or explicit_duration==duration))

    async def synchronize(self,asset,duration,cancel=None):
        if getattr(self,'setup_mode','AUTO')!='MANUAL':
            return await super().synchronize(asset,duration,cancel)
        # Verification only: never Back/Confirm/pair/duration/start in manual mode.
        async with self.nav_lock:
            self.disarm();self.expected=(asset,duration)
            self.nav.cancel=cancel
            msg=await self.nav.screen()
            if self.nav.button(msg,self.button_text) is None:
                raise NavigationError('MANUAL_NEW_SIGNAL_REQUIRED')
            if not self.metadata_matches(getattr(msg,'raw_text','') or '',asset,duration):
                raise NavigationError('MANUAL_SELECTION_MISMATCH')
            self.prepared=(asset,duration)

    async def available_assets(self,cancel=None):
        if getattr(self,'setup_mode','AUTO')=='MANUAL':
            raise NavigationError('MANUAL_NAVIGATION_DISABLED')
        return await super().available_assets(cancel)

    async def next_direction(self,asset,duration,cancel=None):
        if getattr(self,'setup_mode','AUTO')!='MANUAL':
            return await super().next_direction(asset,duration,cancel)
        async with self.nav_lock:
            self.nav.cancel=cancel;self.expected=(asset,duration);self.disarm()
            msg=await self.nav.screen()
            button=self.nav.button(msg,self.button_text)
            if button is None:raise NavigationError('MANUAL_NEW_SIGNAL_REQUIRED')
            if not self.metadata_matches(getattr(msg,'raw_text','') or '',asset,duration):
                raise NavigationError('MANUAL_SELECTION_MISMATCH')
            self.arm()
            try:
                await self.nav.click(msg,button,transition=False)
                text=await asyncio.wait_for(self._pending.get(),config.SIGNAL_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                self.disarm()
                raise NavigationError('MANUAL_SIGNAL_TIMEOUT') from None
            except BaseException:
                self.disarm();raise
            self.armed_at=None;self.last_text=text
            self.last_received=asyncio.get_running_loop().time()
            self.prepared=(asset,duration)
            return parse_direction(text)

    def verifies(self,asset,duration,max_age=10):
        if getattr(self,'setup_mode','AUTO')!='MANUAL':
            return super().verifies(asset,duration,max_age)
        return (self.expected==(asset,duration) and parse_direction(self.last_text) is not None
                and self.metadata_matches(self.last_text,asset,duration)
                and asyncio.get_running_loop().time()-self.last_received<=max_age)
