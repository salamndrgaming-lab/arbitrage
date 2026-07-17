"""Risk gate: every candidate trade must pass every check, every time.

The RiskManager is deliberately stateless about strategy — it only knows
limits. It reads daily totals from the persistent store so restarts cannot
reset spent budgets, and it re-reads the kill switch on every decision so
a human can halt trading by touching a file, no deploy needed.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from ..config import TradingConfig
from ..models import Opportunity
from ..store import Store


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str

    @staticmethod
    def ok() -> "Decision":
        return Decision(True, "ok")

    @staticmethod
    def no(reason: str) -> "Decision":
        return Decision(False, reason)


class RiskManager:
    def __init__(self, cfg: TradingConfig, store: Store):
        self.cfg = cfg
        self.store = store
        self._last_trade_at: dict[str, float] = {}  # asset -> ts
        self.consecutive_failures = 0
        self.tripped = False  # circuit breaker: once True, stays True until restart

    # -- kill switch ------------------------------------------------------

    def kill_switch_active(self) -> bool:
        if os.environ.get("ARB_KILL_SWITCH"):
            return True
        return os.path.exists(self.cfg.kill_switch_file)

    # -- circuit breaker --------------------------------------------------

    def trip(self) -> None:
        """Trip the breaker directly — used when an unwind fails and
        one-sided exposure is left un-flattened. Trading halts until a
        human restarts the process."""
        self.tripped = True

    def record_result(self, success: bool) -> None:
        if success:
            self.consecutive_failures = 0
            return
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.cfg.max_consecutive_failures:
            self.tripped = True

    # -- the gate ---------------------------------------------------------

    def evaluate(self, opp: Opportunity, notional_usd: float,
                 now: float | None = None) -> Decision:
        now = now or time.time()
        c = self.cfg

        if self.tripped:
            return Decision.no(
                f"circuit breaker tripped after {self.consecutive_failures}"
                " consecutive failures; restart required"
            )
        if self.kill_switch_active():
            return Decision.no("kill switch active")

        if not opp.executable:
            return Decision.no("reference-feed leg is not executable")
        if opp.market not in c.markets:
            return Decision.no(f"market {opp.market!r} not enabled for trading")
        if opp.base not in c.assets:
            return Decision.no(f"asset {opp.base} not in trading allowlist")
        if opp.buy_exchange not in c.venues or opp.sell_exchange not in c.venues:
            return Decision.no(
                f"venue pair {opp.buy_exchange}/{opp.sell_exchange} not in allowlist"
            )

        if opp.net_bps < c.min_execute_bps:
            return Decision.no(
                f"net {opp.net_bps:.1f} bps below execute threshold {c.min_execute_bps}"
            )
        age = now - opp.ts
        if age > c.max_quote_age_seconds:
            return Decision.no(f"quotes too old ({age:.1f}s > {c.max_quote_age_seconds}s)")

        if notional_usd <= 0:
            return Decision.no("non-positive notional")
        if notional_usd > c.max_trade_notional_usd:
            return Decision.no(
                f"notional ${notional_usd:.0f} exceeds per-trade cap"
                f" ${c.max_trade_notional_usd:.0f}"
            )

        last = self._last_trade_at.get(opp.base, 0)
        if now - last < c.cooldown_seconds:
            return Decision.no(
                f"cooldown: last {opp.base} trade {now - last:.0f}s ago"
                f" (< {c.cooldown_seconds}s)"
            )

        stats = self.store.trade_stats_since(now - 24 * 3600)
        if stats["count"] >= c.max_trades_per_day:
            return Decision.no(f"daily trade count cap reached ({stats['count']})")
        if stats["notional"] + notional_usd > c.max_daily_notional_usd:
            return Decision.no(
                f"daily notional cap: ${stats['notional']:.0f} spent"
                f" + ${notional_usd:.0f} > ${c.max_daily_notional_usd:.0f}"
            )
        if stats["realized_pnl"] <= -c.max_daily_loss_usd:
            return Decision.no(
                f"daily loss limit hit (realized ${stats['realized_pnl']:.2f})"
            )

        return Decision.ok()

    def note_trade(self, asset: str, now: float | None = None) -> None:
        self._last_trade_at[asset] = now or time.time()
