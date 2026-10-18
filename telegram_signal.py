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

# Matches "Direction: UP" or "Direction: DOWN" (case-insensitive)
DIRECTION_RE = re.compile(r"Direction\s*:\s*(UP|DOWN)", re.IGNORECASE)

# Matches "Current time (UTC+3): 23:41:56" -> used to identify signal messages
SIGNAL_MARKER_RE = re.compile(r"(Direction|Current time|Asset)\s*:", re.IGNORECASE)


class SignalListener:
    def __init__(self, client: TelegramClient):
        self.client = client
        self.bot_entity = None
        self._pending: asyncio.Queue[str] = asyncio.Queue()
        self._press_time: Optional[datetime] = None

    async def init(self):
        self.bot_entity = await self.client.get_entity(config.SIGNAL_BOT_USERNAME)
        log(f"[TG] Signal bot resolved: @{config.SIGNAL_BOT_USERNAME}")

        @self.client.on(events.NewMessage(from_users=self.bot_entity))
        async def on_bot_message(event):
            text = event.raw_text or ""
            if not DIRECTION_RE.search(text):
                return  # not a signal message (e.g. "Meta is preparing the signal...")
            await self._pending.put(text)

    def drain_pending(self):
        """Drops any stale signal texts left in the queue from earlier cycles."""
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

        m = DIRECTION_RE.search(text)
        direction = m.group(1).upper() if m else None
        # Extract extra fields for logging (optional, only for display)
        asset_m = re.search(r"Asset\s*:\s*(.+)", text)
        acc_m = re.search(r"Accuracy rate\s*:\s*([\d.]+%)", text)
        asset = asset_m.group(1).strip() if asset_m else "?"
        accuracy = acc_m.group(1) if acc_m else "ignored"
        log(f"[TG] SIGNAL RECEIVED -> Direction: {direction} | Bot asset: {asset} | "
            f"Accuracy: {accuracy} (ignored as configured)")
        return direction
