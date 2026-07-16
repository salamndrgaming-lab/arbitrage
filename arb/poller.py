"""Polling loop: fetch quotes, run the engine, persist results.

One ``Poller`` instance owns the latest market snapshot. All venue adapters
are queried concurrently each cycle; a venue that errors is marked degraded
but does not stall the others. Slow-moving reference feeds declare a
``min_interval`` and are served from the cached quotes between refreshes.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .config import Config
from .engine import best_spreads, find_opportunities
from .exchanges import Adapter, ExchangeError, build_adapters
from .models import ExchangeStatus, Opportunity, Quote
from .store import Store

log = logging.getLogger("arb.poller")


class Poller:
    def __init__(
        self,
        cfg: Config,
        store: Store | None = None,
        adapters: dict[str, Adapter] | None = None,
    ):
        self.cfg = cfg
        self.store = store
        self.fees = cfg.fees_bps()
        self.adapters = (
            adapters if adapters is not None
            else build_adapters(list(cfg.enabled_exchanges))
        )
        self.statuses: dict[str, ExchangeStatus] = {}
        for name, adapter in self.adapters.items():
            ex_cfg = cfg.exchanges.get(name)
            self.statuses[name] = ExchangeStatus(
                name=name,
                region=ex_cfg.region if ex_cfg else adapter.region,
                tradable=adapter.tradable,
                markets=[m for m in adapter.markets if cfg.markets.get(m)],
            )
        self.tradable = {n for n, a in self.adapters.items() if a.tradable}

        self.quotes: dict[str, list[Quote]] = {}       # venue -> quotes
        self.opportunities: list[Opportunity] = []
        self.best: dict[str, Opportunity] = {}
        self.last_cycle: float | None = None
        self.cycles = 0
        self._last_fetch: dict[str, float] = {}

    def _assets_for(self, adapter: Adapter) -> dict[str, list[str]]:
        return {
            m: self.cfg.markets[m]
            for m in adapter.markets
            if self.cfg.markets.get(m)
        }

    async def _fetch(self, client: httpx.AsyncClient) -> None:
        async def one(name: str):
            adapter = self.adapters[name]
            status = self.statuses[name]
            assets = self._assets_for(adapter)
            if not assets:
                return
            now = time.time()
            # Reference feeds refresh slowly; reuse cached quotes in between —
            # but always retry a venue that is currently failing.
            if (
                status.ok
                and adapter.min_interval
                and now - self._last_fetch.get(name, 0) < adapter.min_interval
            ):
                return
            start = time.time()
            try:
                quotes = await adapter.fetch(client, assets)
                status.ok = True
                status.error = None
                status.last_success = time.time()
                status.latency_ms = round((time.time() - start) * 1000, 1)
                status.quotes = len(quotes)
                self.quotes[name] = quotes
                self._last_fetch[name] = time.time()
            except ExchangeError as exc:
                status.ok = False
                status.error = str(exc)
                status.quotes = 0
                self.quotes.pop(name, None)
                log.warning("%s", exc)

        await asyncio.gather(*(one(n) for n in self.adapters))

    async def run_cycle(self, client: httpx.AsyncClient) -> None:
        await self._fetch(client)

        all_quotes = [q for qs in self.quotes.values() for q in qs]
        self.opportunities = find_opportunities(
            all_quotes, self.fees, self.cfg.min_net_bps,
            self.cfg.transfer_haircut_bps, self.cfg.quote_group, self.tradable,
        )
        self.best = best_spreads(
            all_quotes, self.fees, self.cfg.transfer_haircut_bps,
            self.cfg.quote_group, self.tradable,
        )
        self.last_cycle = time.time()
        self.cycles += 1

        if self.store:
            self.store.record_spreads(self.best)
            self.store.record_opportunities(self.opportunities)
            if self.cycles % 500 == 0:
                self.store.prune(self.cfg.history_retention_hours)

    async def run_forever(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        async with httpx.AsyncClient(timeout=10) as client:
            while not stop.is_set():
                started = time.time()
                try:
                    await self.run_cycle(client)
                except Exception:
                    log.exception("poll cycle failed")
                elapsed = time.time() - started
                delay = max(0.5, self.cfg.poll_interval - elapsed)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass

    def snapshot(self) -> dict:
        """JSON-ready view of the current market state."""
        return {
            "last_cycle": self.last_cycle,
            "cycles": self.cycles,
            "poll_interval": self.cfg.poll_interval,
            "min_net_bps": self.cfg.min_net_bps,
            "markets": self.cfg.markets,
            "exchanges": [s.to_dict() for s in self.statuses.values()],
            "fees_bps": self.fees,
        }
