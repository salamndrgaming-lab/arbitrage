"""Polling loop: fetch quotes, run the engine, persist results.

One ``Poller`` instance owns the latest market snapshot. In live mode all
exchange adapters are queried concurrently each cycle; a venue that errors
is marked degraded but does not stall the others. In demo mode the
simulated feed supplies quotes with the same shape.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from .config import Config
from .engine import best_spreads, find_opportunities
from .exchanges import ExchangeError, build_adapters
from .models import ExchangeStatus, Opportunity, Quote
from .sim import DemoFeed
from .store import Store

log = logging.getLogger("arb.poller")


class Poller:
    def __init__(self, cfg: Config, store: Store | None = None):
        self.cfg = cfg
        self.store = store
        self.fees = cfg.fees_bps()
        names = list(cfg.enabled_exchanges)
        self.statuses: dict[str, ExchangeStatus] = {
            n: ExchangeStatus(name=n, region=c.region)
            for n, c in cfg.enabled_exchanges.items()
        }
        self.demo_feed = DemoFeed(names, cfg.assets) if cfg.mode == "demo" else None
        self.adapters = {} if self.demo_feed else build_adapters(names)

        self.quotes: dict[str, list[Quote]] = {}       # exchange -> quotes
        self.opportunities: list[Opportunity] = []
        self.best: dict[str, Opportunity] = {}
        self.last_cycle: float | None = None
        self.cycles = 0

    async def _fetch_live(self, client: httpx.AsyncClient) -> None:
        async def one(name: str):
            status = self.statuses[name]
            start = time.time()
            try:
                quotes = await self.adapters[name].fetch(client, self.cfg.assets)
                status.ok = True
                status.error = None
                status.last_success = time.time()
                status.latency_ms = round((time.time() - start) * 1000, 1)
                status.quotes = len(quotes)
                self.quotes[name] = quotes
            except ExchangeError as exc:
                status.ok = False
                status.error = str(exc)
                status.quotes = 0
                self.quotes.pop(name, None)
                log.warning("%s", exc)

        await asyncio.gather(*(one(n) for n in self.adapters))

    def _fetch_demo(self) -> None:
        self.quotes = self.demo_feed.tick()
        now = time.time()
        for name, status in self.statuses.items():
            status.ok = True
            status.last_success = now
            status.latency_ms = 0.0
            status.quotes = len(self.quotes.get(name, []))

    async def run_cycle(self, client: httpx.AsyncClient | None = None) -> None:
        if self.demo_feed:
            self._fetch_demo()
        else:
            assert client is not None
            await self._fetch_live(client)

        all_quotes = [q for qs in self.quotes.values() for q in qs]
        self.opportunities = find_opportunities(
            all_quotes, self.fees, self.cfg.min_net_bps,
            self.cfg.transfer_haircut_bps, self.cfg.quote_group,
        )
        self.best = best_spreads(
            all_quotes, self.fees, self.cfg.transfer_haircut_bps, self.cfg.quote_group
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
            "mode": "demo" if self.demo_feed else "live",
            "last_cycle": self.last_cycle,
            "cycles": self.cycles,
            "poll_interval": self.cfg.poll_interval,
            "min_net_bps": self.cfg.min_net_bps,
            "assets": self.cfg.assets,
            "exchanges": [s.to_dict() for s in self.statuses.values()],
            "fees_bps": self.fees,
        }
