"""Simulated market feed for demo mode and offline development.

Each asset has a shared mid price following a random walk; each exchange
quotes around that mid with its own half-spread and a slowly mean-reverting
idiosyncratic offset. Occasional "dislocation" events push one venue's
offset far enough to create realistic arbitrage windows that appear, decay,
and close — so the dashboard has something to show without network access.
"""

from __future__ import annotations

import random
import time

from .models import Quote

BASE_PRICES = {
    "BTC": 96_000.0,
    "ETH": 4_600.0,
    "SOL": 210.0,
    "XRP": 2.30,
    "LTC": 130.0,
    "DOGE": 0.35,
    "ADA": 1.05,
}

# Matches the live adapters' actual quote currencies.
EXCHANGE_QUOTES = {
    "binance": "USDT",
    "kraken": "USD",
    "coinbase": "USD",
    "bitstamp": "USD",
    "bitfinex": "USD",
    "kucoin": "USDT",
    "okx": "USDT",
    "gateio": "USDT",
}


class DemoFeed:
    def __init__(self, exchanges: list[str], assets: list[str], seed: int | None = None):
        self.rng = random.Random(seed)
        self.exchanges = exchanges
        self.assets = [a for a in assets if a in BASE_PRICES] or ["BTC"]
        self.mids = {a: BASE_PRICES[a] for a in self.assets}
        # Per-venue idiosyncratic offset in bps, mean-reverting.
        self.offsets = {(e, a): self.rng.uniform(-3, 3)
                        for e in exchanges for a in self.assets}
        # Per-venue half-spread in bps.
        self.half_spreads = {e: self.rng.uniform(1.0, 5.0) for e in exchanges}
        # Active dislocations: (exchange, asset) -> remaining ticks.
        self.dislocations: dict[tuple[str, str], int] = {}

    def _step(self) -> None:
        for a in self.assets:
            self.mids[a] *= 1 + self.rng.gauss(0, 0.0004)
        # Maybe start a new dislocation (~1 in 6 ticks).
        if self.rng.random() < 0.18:
            key = (self.rng.choice(self.exchanges), self.rng.choice(self.assets))
            if key not in self.dislocations:
                self.dislocations[key] = self.rng.randint(3, 10)
                self.offsets[key] += self.rng.choice([-1, 1]) * self.rng.uniform(25, 90)
        for key in list(self.dislocations):
            self.dislocations[key] -= 1
            if self.dislocations[key] <= 0:
                del self.dislocations[key]
                self.offsets[key] = self.rng.uniform(-3, 3)
        for key, off in self.offsets.items():
            # Mean-revert and jitter.
            self.offsets[key] = off * 0.92 + self.rng.gauss(0, 1.2)

    def tick(self) -> dict[str, list[Quote]]:
        """Advance one step and return quotes per exchange."""
        self._step()
        now = time.time()
        out: dict[str, list[Quote]] = {}
        for e in self.exchanges:
            half = self.half_spreads[e] / 10_000
            quotes = []
            for a in self.assets:
                px = self.mids[a] * (1 + self.offsets[(e, a)] / 10_000)
                quotes.append(Quote(e, a, EXCHANGE_QUOTES.get(e, "USD"),
                                    bid=px * (1 - half), ask=px * (1 + half), ts=now))
            out[e] = quotes
        return out
