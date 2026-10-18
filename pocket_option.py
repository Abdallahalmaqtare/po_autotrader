"""
pocket_option.py - Direct WebSocket client for Pocket Option.
Connected directly to: demo-api-eu.po.market

Trade-result detection (v4 - balance comparison):
  closeOrder/closed-deal frames may arrive late or with a 451- prefix, so
  the PRIMARY check is comparing the account balance BEFORE the order with
  the balance AFTER it closes:
      balance_after >  balance_before -> WIN
      balance_after <  balance_before -> LOSS
      balance_after == balance_before -> DRAW
  Balance is tracked live from the platform's updateBalance frames.
  Strict closed-deals capture is kept as a fast-path when available.
"""
import asyncio
import inspect
import json
import re
import ssl
import time
from typing import Optional

import websockets

import config
from logger import log

WS_URL = config.PO_WS_URL

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0.0.0 Safari/537.36"
)

MAX_RECONNECT_DELAY = 30

RESULT_WAIT_SECONDS = 8  # fallback only; main.py passes DURATION + RESULT_TIMEOUT_EXTRA from .env
CLOSE_TS_TOLERANCE = 15
CLOSED_DEAL_MARKERS = ("ClosedDeals", "closedDeals", "closedDeal", "closeOption")
CLOSE_TS_KEYS = ("closeTimestamp", "close_timestamp", "closeTime", "close_time")
BALANCE_RE = re.compile(r'"balance"\s*:\s*(-?[\d.]+)')


class PocketOptionWS:
    """Persistent WebSocket connection to Pocket Option with auto-reconnect."""

    def __init__(self, ssid: str):
        self.ssid = ssid.strip().strip("'\"")
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.connected = False
        self._lock = asyncio.Lock()
        self._recv_task: Optional[asyncio.Task] = None
        self._reconnect_delay = 1

        # ---- balance tracking (primary result source) ----
        self.balance: Optional[float] = None
        self._balance_event = asyncio.Event()
        self._balance_before: Optional[float] = None

        # ---- strict closed-deal fast-path (secondary) ----
        self._awaiting_result = False
        self._result_queue: asyncio.Queue = asyncio.Queue()
        self._expected_close_ts: float = 0.0
        self._pending_asset_norm: str = ""
        self._pending_amount: float = 0.0

    # ------------------------------------------------------------------
    @staticmethod
    def _norm_asset(name: str) -> str:
        return str(name).lower().replace("/", "").replace("_", "").replace(" ", "")

    # ------------------------------------------------------------------
    def _build_auth_frame(self) -> str:
        if self.ssid.startswith("42") and '"auth"' in self.ssid:
            return self.ssid
        # isDemo comes from ACCOUNT_TYPE; uid 0 = resolved server-side
        demo = 0 if config.ACCOUNT_TYPE == "REAL" else 1
        return (
            f'42["auth",{{"session":"{self.ssid}",'
            f'"isDemo":{demo},"uid":0,"platform":1}}]'
        )

    # ------------------------------------------------------------------
    async def connect(self):
        attempt = 0
        while True:
            attempt += 1
            try:
                log(f"[PO] Connecting to demo-api-eu.po.market (attempt {attempt}) ...")

                extra_headers = {
                    "User-Agent": USER_AGENT,
                    "Origin": "https://pocketoption.com",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                }
                connect_kwargs = {
                    "ping_interval": None,
                    "max_size": 2**22,
                    "close_timeout": 5,
                    "ssl": ssl.create_default_context(),
                }
                sig = inspect.signature(websockets.connect)
                if "additional_headers" in sig.parameters:
                    connect_kwargs["additional_headers"] = extra_headers
                elif "extra_headers" in sig.parameters:
                    connect_kwargs["extra_headers"] = extra_headers

                self.ws = await websockets.connect(WS_URL, **connect_kwargs)

                greeting = await asyncio.wait_for(self.ws.recv(), timeout=10)
                if isinstance(greeting, bytes):
                    greeting = greeting.decode("utf-8", errors="ignore")
                if not greeting.startswith("0"):
                    raise ConnectionError(f"Unexpected greeting: {greeting[:80]}")

                await self.ws.send("40")

                deadline = time.time() + 10
                ns_ready = False
                while time.time() < deadline:
                    msg = await asyncio.wait_for(self.ws.recv(), timeout=10)
                    if isinstance(msg, bytes):
                        msg = msg.decode("utf-8", errors="ignore")
                    if msg == "2":
                        await self.ws.send("3")
                    elif msg.startswith("40"):
                        ns_ready = True
                        break
                if not ns_ready:
                    raise ConnectionError("Namespace connect timeout")

                await self.ws.send(self._build_auth_frame())

                auth_deadline = time.time() + 10
                authed = False
                while time.time() < auth_deadline:
                    msg = await asyncio.wait_for(self.ws.recv(), timeout=10)
                    text = msg.decode("utf-8", errors="ignore") if isinstance(msg, bytes) else msg
                    if text == "2":
                        await self.ws.send("3")
                        continue
                    self._track_balance(text)
                    if any(key in text for key in ("successauth", "updateBalance", "updateAssets", "451-", "42")):
                        authed = True
                        break
                if not authed:
                    raise ConnectionError("Auth failed - please verify PO_SSID in .env")

                change_sym = f'42["changeSymbol",{{"asset":"{config.ASSET}","period":{config.DURATION_SECONDS}}}]'
                await self.ws.send(change_sym)

                self.connected = True
                self._reconnect_delay = 1
                log(f"[PO] WebSocket CONNECTED & authenticated successfully! "
                    f"(account: {config.ACCOUNT_TYPE} | balance: {self.balance})")

                self._recv_task = asyncio.create_task(self._receiver_loop())
                return

            except Exception as e:
                log(f"[PO] Connection error: {e}")
                self.connected = False
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, MAX_RECONNECT_DELAY)

    # ------------------------------------------------------------------
    # -------- live balance tracking ------------------------------------
    # ------------------------------------------------------------------
    def _track_balance(self, text: str):
        """Updates self.balance from any frame carrying a balance value."""
        if "balance" not in text:
            return
        m = BALANCE_RE.search(text)
        if not m:
            return
        try:
            value = float(m.group(1))
        except ValueError:
            return
        if self.balance != value:
            self.balance = value
            self._balance_event.set()
            self._balance_event = asyncio.Event()

    # ------------------------------------------------------------------
    # -------- strict closed-deal fast-path ------------------------------
    # ------------------------------------------------------------------
    def _find_close_ts(self, deal: dict) -> Optional[float]:
        for k in CLOSE_TS_KEYS:
            if k in deal:
                try:
                    return float(deal[k])
                except (TypeError, ValueError):
                    continue
        return None

    def _iter_deals(self, node):
        if isinstance(node, dict):
            if "profit" in node:
                yield node
            for v in node.values():
                if isinstance(v, (dict, list)):
                    yield from self._iter_deals(v)
        elif isinstance(node, list):
            for v in node:
                if isinstance(v, (dict, list)):
                    yield from self._iter_deals(v)

    def _maybe_capture_result(self, text: str):
        """Fast-path: exact closed-deal match (also tolerates 451- prefixed frames)."""
        if not self._awaiting_result:
            return
        if time.time() > self._expected_close_ts + CLOSE_TS_TOLERANCE:
            return  # outside the window -> balance comparison will decide
        if not any(marker in text for marker in CLOSED_DEAL_MARKERS):
            return
        start = text.find("[")
        if start == -1:
            return
        try:
            arr = json.loads(text[start:])
        except Exception:
            return
        if not isinstance(arr, list):
            return

        best = None
        for deal in self._iter_deals(arr[1:] if len(arr) > 1 else arr):
            ts = self._find_close_ts(deal)
            if ts is None:
                continue
            if ts > 1e12:
                ts = ts / 1000.0
            diff = ts - self._expected_close_ts
            if abs(diff) > CLOSE_TS_TOLERANCE and not (0 <= diff <= 120):
                continue
            d_asset = deal.get("asset") or deal.get("symbol") or ""
            if d_asset and self._norm_asset(d_asset) != self._pending_asset_norm:
                continue
            try:
                profit = float(deal["profit"])
            except (TypeError, ValueError):
                continue
            score = abs(diff)
            if best is None or score < best[0]:
                best = (score, profit)

        if best is None:
            return

        profit = best[1]
        self._awaiting_result = False
        result = "win" if profit > 0 else ("loss" if profit < 0 else "draw")
        log(f"[PO] TRADE RESULT confirmed from closed-deal frame -> profit={profit} "
            f"(amount={self._pending_amount}$) -> {result.upper()}")
        try:
            self._result_queue.put_nowait(result)
        except asyncio.QueueFull:
            pass

    # ------------------------------------------------------------------
    async def _receiver_loop(self):
        try:
            async for msg in self.ws:
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", errors="ignore")
                if msg == "2":
                    await self.ws.send("3")
                else:
                    self._track_balance(msg)
                    self._maybe_capture_result(msg)
        except Exception as e:
            log(f"[PO] Receiver stopped: {e}")
        finally:
            self.connected = False
            log("[PO] Connection LOST - auto-reconnect will trigger on next trade")

    # ------------------------------------------------------------------
    async def ensure_connected(self):
        if not self.connected:
            await self.connect()

    # ------------------------------------------------------------------
    async def place_trade(self, direction: str, amount: float = None) -> bool:
        action = "call" if direction.upper() == "UP" else "put"
        if amount is None:
            amount = config.TRADE_AMOUNT

        order = {
            "asset": config.ASSET,
            "amount": amount,
            "action": action,
            "isDemo": config.IS_DEMO,
            "optionType": 100,
            "time": config.DURATION_SECONDS,
            "requestId": int(time.time() * 1000) % 2_000_000_000,
        }
        frame = '42["openOrder",' + json.dumps(order, separators=(",", ":")) + "]"

        async with self._lock:
            await self.ensure_connected()
            try:
                # Capture BEFORE send: send() may yield while the receiver handles
                # the stake deduction. A post-send snapshot can already be debited.
                self._balance_before = self.balance
                while not self._result_queue.empty():
                    self._result_queue.get_nowait()
                t0 = time.perf_counter()
                await self.ws.send(frame)
                elapsed_ms = (time.perf_counter() - t0) * 1000
                self._awaiting_result = True
                self._expected_close_ts = time.time() + config.DURATION_SECONDS
                self._pending_asset_norm = self._norm_asset(config.ASSET)
                self._pending_amount = amount
                log(f"[PO] ORDER SENT  -> {config.ASSET} | {action.upper()} | "
                    f"{amount}$ | {config.DURATION_SECONDS}s | "
                    f"sent in {elapsed_ms:.1f} ms | id={order['requestId']} | "
                    f"balance_before={self._balance_before}")
                return True
            except Exception as e:
                log(f"[PO] Failed to send order: {e} - will reconnect")
                self._awaiting_result = False
                self.connected = False
                try:
                    await self.connect()
                except Exception:
                    pass
                return False

    # ------------------------------------------------------------------
    async def wait_for_result(self, timeout: int = RESULT_WAIT_SECONDS) -> Optional[str]:
        """v6 balance logic restored, with a bounded loss recheck.

        No requirement for a NEW post-expiry balance message: losing trades
        may leave the already-debited balance unchanged at expiry.
        Do not use the old time-only closed-deal fast path to decide sizing;
        nearby historical deals are not uniquely matched to this order.
        A finite grace window reduces, but cannot eliminate, late-settlement
        errors. Use only with one bot/order and no manual balance movements.
        """
        import math

        budget = max(0.0, float(timeout))
        settle = max(0.0, float(config.BALANCE_SETTLE_SECONDS))
        grace = max(0.0, float(config.LOSS_RECHECK_SECONDS))
        # Wait the whole configured interval, not just until the first balance
        # event. That first event may be a debit or an intermediate settlement.
        self._awaiting_result = False
        await asyncio.sleep(budget)
        if settle:
            await asyncio.sleep(settle)

        before = self._balance_before
        after = self.balance
        if (before is None or after is None or
                not math.isfinite(float(before)) or
                not math.isfinite(float(after))):
            log("[PO] Balance unavailable -> UNCONFIRMED (no result recorded)")
            return None

        epsilon = 0.000001
        if after < before - epsilon and grace:
            log(f"[PO] Possible LOSS -> rechecking for {grace:g}s before doubling")
            # Always consume the entire grace window even if a balance packet
            # arrives early. Allow a delayed winning payout to correct the debit.
            await asyncio.sleep(grace)
            after = self.balance
            if after is None or not math.isfinite(float(after)):
                log("[PO] Balance unavailable on recheck -> UNCONFIRMED")
                return None

        delta = after - before
        result = "win" if delta > epsilon else ("loss" if delta < -epsilon else "draw")
        log(f"[PO] BALANCE CHECK -> before={before} | after={after} | delta={delta:+.6f}")
        log(f"[PO] RESULT by v6 balance comparison + loss recheck -> {result.upper()}")
        return result
