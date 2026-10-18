"""
martingale.py - Money management (doubling on loss) + session statistics.

Rules (as specified):
  - Base amount is fixed (from .env TRADE_AMOUNT).
  - On LOSS  -> next SIGNAL trades with doubled amount (base * 2^step).
                The double happens on the NEXT signal, never on the same one.
  - On DRAW  -> next signal uses the SAME amount again (same doubling step).
  - On WIN   -> back to the fixed base amount.
  - If the trade at the LAST allowed doubling step also loses,
    the whole sequence counts as ONE losing trade and we reset to base.

Statistics:
  - Total trades   = completed sequences (wins + full losses)
  - Won trades     = sequences closed with a win
  - Lost trades    = sequences that lost ALL doubling steps
  - Draws / individual orders are shown separately for transparency.
"""
import config
from logger import log


class MoneyManager:
    def __init__(self, base_amount: float, max_doubles: int):
        self.base_amount = base_amount
        self.max_doubles = max(0, max_doubles)   # e.g. 4 -> orders: 10,20,40,80
        self.step = 0                             # 0 = base, last order is step max-1

        # ---- statistics ----
        self.orders_sent = 0     # every individual order placed
        self.won_trades = 0      # sequences ended with WIN
        self.lost_trades = 0     # sequences that lost ALL doubles
        self.draws = 0           # individual orders that ended in a draw
        self.pending = False     # a sequence is currently in progress

    # ------------------------------------------------------------------
    def current_amount(self) -> float:
        amount = self.base_amount * (2 ** self.step)
        return round(amount, 2)

    def label(self) -> str:
        if self.step == 0:
            return "BASE"
        return f"DOUBLE #{self.step}/{self.max_doubles}"

    # ------------------------------------------------------------------
    def register_result(self, result: str):
        """result: 'win' | 'loss' | 'draw'"""
        if result == "win":
            self.won_trades += 1
            if self.step > 0:
                log(f"[MM] WIN at double #{self.step} -> sequence WON, back to base {self.base_amount}$")
            else:
                log(f"[MM] WIN at base -> stay on {self.base_amount}$")
            self.step = 0
            self.pending = False

        elif result == "draw":
            self.draws += 1
            log(f"[MM] DRAW -> next signal uses the SAME amount "
                f"{self.current_amount()}$ ({self.label()})")
            # step unchanged; sequence still open

        elif result == "loss":
            if self.step >= self.max_doubles - 1:
                self.lost_trades += 1
                log(f"[MM] LOSS at final double #{self.step} -> sequence LOST "
                    f"(all {self.max_doubles} steps). Reset to base {self.base_amount}$")
                self.step = 0
                self.pending = False
            else:
                self.step += 1
                self.pending = True
                log(f"[MM] LOSS -> next signal will DOUBLE to "
                    f"{self.current_amount()}$ ({self.label()})")

    # ------------------------------------------------------------------
    @property
    def total_trades(self) -> int:
        return self.won_trades + self.lost_trades

    @property
    def win_rate(self) -> float:
        return (self.won_trades / self.total_trades * 100) if self.total_trades else 0.0

    # ------------------------------------------------------------------
    def print_stats(self):
        log("=" * 62)
        log("  SESSION STATISTICS")
        log(f"  Orders sent      : {self.orders_sent}")
        log(f"  Total trades     : {self.total_trades}  (completed sequences)")
        log(f"  Won trades       : {self.won_trades}")
        log(f"  Lost trades      : {self.lost_trades}  (lost all {self.max_doubles} steps)")
        log(f"  Draws (orders)   : {self.draws}")
        log(f"  Win rate         : {self.win_rate:.1f}%")
        log(f"  Next amount      : {self.current_amount()}$ ({self.label()})")
        log("=" * 62)
