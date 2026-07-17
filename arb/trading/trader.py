"""Autonomous trading loop: market data -> risk gate -> dual-leg execution.

The trader owns its own market-data poller. Each cycle it takes the best
executable opportunity, sizes it under the per-trade cap, passes it
through every risk check, re-verifies balances on both venues, then fires
both IOC legs concurrently. Every attempt — filled, partial, or failed —
is written to the audit trail before the next cycle starts.

Arming is all-or-nothing (see ``arm_check``); a partial fill or any
execution error counts toward the circuit breaker, which permanently
disarms the process until a human restarts it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx

from . import ARMING_ENV, ARMING_PHRASE
from ..config import Config
from ..models import Opportunity
from ..poller import Poller
from ..store import Store
from .execution import ExecutionError, ExecutionVenue, build_executors
from .risk import RiskManager

log = logging.getLogger("arb.trader")


class NotArmedError(RuntimeError):
    pass


def arm_check(cfg: Config) -> None:
    """Every condition must hold or the trader refuses to construct."""
    t = cfg.trading
    if not t.enabled:
        raise NotArmedError("trading.enabled is false in config")
    if os.environ.get(ARMING_ENV) != ARMING_PHRASE:
        raise NotArmedError(
            f"environment variable {ARMING_ENV} is not set to {ARMING_PHRASE!r}"
        )
    if not t.venues:
        raise NotArmedError("trading.venues is empty")
    if not t.assets:
        raise NotArmedError("trading.assets is empty")


class Trader:
    def __init__(
        self,
        cfg: Config,
        store: Store,
        executors: dict[str, ExecutionVenue] | None = None,
        poller: Poller | None = None,
    ):
        arm_check(cfg)
        self.cfg = cfg
        self.store = store
        self.executors = executors if executors is not None else build_executors(
            cfg.trading.venues
        )
        self.poller = poller or Poller(cfg, store)
        self.risk = RiskManager(cfg.trading, store)
        self.trades_attempted = 0

    # ------------------------------------------------------------------

    def _size(self, opp: Opportunity) -> tuple[float, float]:
        """(qty in base units, notional in USD) under the per-trade cap."""
        notional = self.cfg.trading.max_trade_notional_usd
        qty = notional / opp.buy_price
        return qty, notional

    async def _check_balances(
        self, client: httpx.AsyncClient, opp: Opportunity, qty: float, notional: float
    ) -> str | None:
        """Return a rejection reason, or None if both legs are funded."""
        try:
            buy_bal, sell_bal = await asyncio.gather(
                self.executors[opp.buy_exchange].balances(client),
                self.executors[opp.sell_exchange].balances(client),
            )
        except ExecutionError as exc:
            return f"balance check failed: {exc}"
        # Buy leg needs quote currency; require headroom for fees.
        need_quote = notional * 1.01
        have_quote = buy_bal.get(opp.buy_quote.upper(), 0.0)
        if have_quote < need_quote:
            return (f"insufficient {opp.buy_quote} on {opp.buy_exchange}"
                    f" ({have_quote:.2f} < {need_quote:.2f})")
        have_base = sell_bal.get(opp.base.upper(), 0.0)
        if have_base < qty:
            return (f"insufficient {opp.base} on {opp.sell_exchange}"
                    f" ({have_base:.8f} < {qty:.8f})")
        return None

    async def _execute(self, client: httpx.AsyncClient, opp: Opportunity,
                       qty: float, notional: float) -> None:
        self.trades_attempted += 1
        self.risk.note_trade(opp.base)
        buy_exec = self.executors[opp.buy_exchange]
        sell_exec = self.executors[opp.sell_exchange]

        results = await asyncio.gather(
            buy_exec.place_ioc_limit(client, opp.base, opp.buy_quote, "buy",
                                     qty, opp.buy_price),
            sell_exec.place_ioc_limit(client, opp.base, opp.sell_quote, "sell",
                                      qty, opp.sell_price),
            return_exceptions=True,
        )
        buy_res, sell_res = results
        buy_err = isinstance(buy_res, BaseException)
        sell_err = isinstance(sell_res, BaseException)

        if buy_err or sell_err:
            status = "failed" if buy_err and sell_err else "partial"
            detail = "; ".join(
                f"{leg}: {res}" for leg, res in (("buy", buy_res), ("sell", sell_res))
                if isinstance(res, BaseException)
            )
            log.error("trade %s %s: %s", opp.base, status, detail)
            self.store.record_trade(opp, qty, notional, status, detail)
            self.risk.record_result(False)
            return

        both_filled = buy_res.fully_filled and sell_res.fully_filled
        status = "filled" if both_filled else "partial"
        detail = (f"buy {buy_res.filled_qty}/{qty} @{buy_res.price}"
                  f" ({buy_res.raw_status}); sell {sell_res.filled_qty}/{qty}"
                  f" @{sell_res.price} ({sell_res.raw_status})")
        log.info("trade %s %s: %s", opp.base, status, detail)
        self.store.record_trade(opp, qty, notional, status, detail)
        # A partial leaves one-sided inventory exposure -> counts as a failure
        # for the circuit breaker even though nothing errored.
        self.risk.record_result(both_filled)

    # ------------------------------------------------------------------

    async def run_cycle(self, client: httpx.AsyncClient) -> None:
        await self.poller.run_cycle(client)

        if self.risk.tripped:
            return
        for opp in self.poller.opportunities:
            qty, notional = self._size(opp)
            decision = self.risk.evaluate(opp, notional)
            if not decision.allowed:
                log.debug("skip %s %s->%s: %s", opp.base, opp.buy_exchange,
                          opp.sell_exchange, decision.reason)
                continue
            reason = await self._check_balances(client, opp, qty, notional)
            if reason:
                log.info("skip %s: %s", opp.base, reason)
                continue
            await self._execute(client, opp, qty, notional)
            break  # at most one trade per cycle, by design

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        log.warning(
            "LIVE TRADING ARMED: venues=%s assets=%s per-trade cap $%s,"
            " daily cap $%s, daily loss limit $%s",
            self.cfg.trading.venues, self.cfg.trading.assets,
            self.cfg.trading.max_trade_notional_usd,
            self.cfg.trading.max_daily_notional_usd,
            self.cfg.trading.max_daily_loss_usd,
        )
        async with httpx.AsyncClient(timeout=10) as client:
            while not stop.is_set():
                started = time.time()
                try:
                    await self.run_cycle(client)
                except Exception:
                    log.exception("trader cycle failed")
                    self.risk.record_result(False)
                if self.risk.tripped:
                    log.error("circuit breaker tripped — trading disarmed until restart")
                    break
                if self.risk.kill_switch_active():
                    log.warning("kill switch active — exiting trader loop")
                    break
                elapsed = time.time() - started
                delay = max(0.5, self.cfg.poll_interval - elapsed)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    def status(self) -> dict:
        stats = self.store.trade_stats_since(time.time() - 24 * 3600)
        return {
            "armed": True,
            "tripped": self.risk.tripped,
            "kill_switch": self.risk.kill_switch_active(),
            "consecutive_failures": self.risk.consecutive_failures,
            "trades_attempted": self.trades_attempted,
            "last_24h": stats,
            "limits": {
                "min_execute_bps": self.cfg.trading.min_execute_bps,
                "max_trade_notional_usd": self.cfg.trading.max_trade_notional_usd,
                "max_daily_notional_usd": self.cfg.trading.max_daily_notional_usd,
                "max_trades_per_day": self.cfg.trading.max_trades_per_day,
                "max_daily_loss_usd": self.cfg.trading.max_daily_loss_usd,
            },
            "venues": list(self.executors),
            "assets": self.cfg.trading.assets,
        }
