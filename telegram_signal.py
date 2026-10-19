"""Per-instance signal source, detachable handlers and per-request armed queue.
Original UP/DOWN parsing and inline New signal workflow are preserved.
"""
import asyncio
import re
from collections import deque
from telethon import events
import config
from logger import log
from telegram_bootstrap import signal_username

DIRECTION_RE = re.compile(r'Direction\s*:\s*(UP|DOWN)\b', re.IGNORECASE)


class SignalListener:
    def __init__(self, client, username=None):
        self.client = client
        self.source_username = signal_username(username or config.SIGNAL_BOT_USERNAME)
        self.bot_entity = None
        self._pending = asyncio.Queue()
        self._handler = None
        self._accepting = False
        self._min_id = 0
        self._seen = deque(maxlen=128)

    async def init(self):
        self.bot_entity = await self.client.get_entity(self.source_username)
        if not getattr(self.bot_entity,'bot',False):
            raise ValueError('SIGNAL_TARGET_NOT_BOT')
        entity_id = self.bot_entity.id

        async def handler(event):
            if not self._accepting or event.sender_id != entity_id or event.chat_id != entity_id:
                return
            text = event.raw_text or ''
            message_id = event.id
            if message_id < self._min_id or (message_id,text) in self._seen:
                return
            if not DIRECTION_RE.search(text):
                return
            self._seen.append((message_id,text))
            await self._pending.put(text)

        self._handler = handler
        self.client.add_event_handler(handler, events.NewMessage(chats=entity_id, from_users=entity_id))
        self.client.add_event_handler(handler, events.MessageEdited(chats=entity_id, from_users=entity_id))
        log(f'[TG] Signal source ready: @{self.source_username}')

    def detach(self):
        self._accepting = False
        if self._handler:
            self.client.remove_event_handler(self._handler)
            self._handler = None
        self.drain_pending()

    def drain_pending(self):
        self._accepting = False
        while not self._pending.empty():
            try:
                self._pending.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def press_new_signal(self):
        latest_id = 0
        async for msg in self.client.iter_messages(self.bot_entity, limit=10):
            latest_id = max(latest_id, msg.id)
            if not msg.buttons:
                continue
            for row in msg.buttons:
                for button in row:
                    if button.text and config.NEW_SIGNAL_BUTTON_TEXT.lower() in button.text.lower():
                        self.drain_pending()
                        self._min_id = latest_id
                        self._seen.append((msg.id, msg.raw_text or ''))
                        self._accepting = True  # arm before await: fast reply must not be lost
                        try:
                            await msg.click(text=button.text)
                        except BaseException:
                            self.drain_pending()
                            raise
                        log(f'[TG] New signal requested from @{self.source_username}')
                        return True
        return False

    async def wait_for_signal(self, timeout):
        try:
            text = await asyncio.wait_for(self._pending.get(), timeout)
            match = DIRECTION_RE.search(text)
            direction = match.group(1).upper() if match else None
            log(f'[TG] Direction={direction}; source=@{self.source_username}')
            return direction
        except asyncio.TimeoutError:
            return None
        finally:
            self._accepting = False
