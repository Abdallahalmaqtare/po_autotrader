"""
telegram_signal.py - MTProto client (Telethon) that:
  1) Auto-presses the inline button "New signal" in the signal bot chat
  2) Waits for the bot's signal message
  3) Parses ONLY the Direction (UP / DOWN) - accuracy % is ignored
"""
import re
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from telethon import TelegramClient, events

import config
from logger import log

# Accept both old and new labels; normalize to the existing trading interface.
DIRECTION_RE = re.compile(
    r"(?<!\w)(?:Direction|Meta\s+Signal)\s*:\s*(UP|DOWN|HIGHER|LOWER)\b",
    re.IGNORECASE)
DIRECTION_MAP = {'UP': 'UP', 'HIGHER': 'UP', 'DOWN': 'DOWN', 'LOWER': 'DOWN'}


def parse_direction(text):
    text = re.sub(r"[\u200b-\u200f\u202a-\u202e\u2060-\u2069]", "", text or "")
    text = re.sub(r"[*_`]", "", text)
    matches = {DIRECTION_MAP[m.group(1).upper()] for m in DIRECTION_RE.finditer(text)}
    # Never choose arbitrarily if a message carries contradictory directions.
    return next(iter(matches)) if len(matches) == 1 else None

# Matches "Current time (UTC+3): 23:41:56" -> used to identify signal messages
SIGNAL_MARKER_RE = re.compile(r"(Direction|Meta\s+Signal|Current time|Asset)\s*:", re.IGNORECASE)


class SignalListener:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.bot_entity = None
        self._pending: asyncio.Queue[str] = asyncio.Queue()
        self._press_time: Optional[datetime] = None
        self._seen_pending = set()

    async def init(self):
        self.bot_entity = await self.client.get_entity(config.SIGNAL_BOT_USERNAME)
        log(f"[TG] Signal bot resolved: @{config.SIGNAL_BOT_USERNAME}")

        @self.client.on(events.NewMessage(from_users=self.bot_entity))
        @self.client.on(events.MessageEdited(from_users=self.bot_entity))
        async def on_bot_message(event):
            text = event.raw_text or ""
            direction = parse_direction(text)
            if direction is None:
                return  # preparation/unknown/conflicting text is never a trade
            key = (getattr(event, 'id', None), direction)
            if key in self._seen_pending:
                return  # same final card delivered both as new and edited event
            if len(self._seen_pending) >= 256:
                self._seen_pending.clear()
            self._seen_pending.add(key)
            await self._pending.put(text)

    def drain_pending(self):
        """Drops any stale signal texts left in the queue from earlier cycles."""
        self._seen_pending.clear()
        dropped = 0
        while not self._pending.empty():
            try:
                self._pending.get_nowait()
                dropped += 1
            except asyncio.QueueEmpty:
                break
        if dropped:
            log(f"[TG] Drained {dropped} stale signal(s) from previous cycle")

    # ------------------------------------------------------------------
    async def press_new_signal(self) -> bool:
        """Finds the latest message carrying the 'New signal' button and clicks it."""
        t0 = datetime.now(timezone.utc)
        log(f"[TG] Pressing '{config.NEW_SIGNAL_BUTTON_TEXT}' ...")

        async for msg in self.client.iter_messages(self.bot_entity, limit=10):
            if not msg.buttons:
                continue
            for row in msg.buttons:
                for btn in row:
                    if btn.text and config.NEW_SIGNAL_BUTTON_TEXT.lower() in btn.text.lower():
                        await msg.click(text=btn.text)
                        self._press_time = datetime.now(timezone.utc)
                        ms = (self._press_time - t0).total_seconds() * 1000
                        log(f"[TG] Button PRESSED at {self._press_time.isoformat(timespec='milliseconds')} "
                            f"(took {ms:.0f} ms)")
                        return True

        log("[TG] ERROR: 'New signal' button not found in recent messages")
        return False

    # ------------------------------------------------------------------
    async def wait_for_signal(self, timeout: int) -> Optional[str]:
        """Waits for the next signal message; returns 'UP', 'DOWN' or None."""
        try:
            text = await asyncio.wait_for(self._pending.get(), timeout=timeout)
        except asyncio.TimeoutError:
            log(f"[TG] TIMEOUT: no signal received within {timeout}s")
            return None

        direction = parse_direction(text)
        # Extract extra fields for logging (optional, only for display)
        asset_m = re.search(r"Asset\s*:\s*(.+)", text)
        acc_m = re.search(r"Accuracy rate\s*:\s*([\d.]+%)", text)
        asset = asset_m.group(1).strip() if asset_m else "?"
        accuracy = acc_m.group(1) if acc_m else "ignored"
        log(f"[TG] SIGNAL RECEIVED -> Direction: {direction} | Bot asset: {asset} | "
            f"Accuracy: {accuracy} (ignored as configured)")
        return direction
