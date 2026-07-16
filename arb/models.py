"""Core data types shared across the tracker."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict


@dataclass(frozen=True)
class Quote:
    """Best bid/ask for one asset on one exchange."""

    exchange: str
    base: str          # e.g. "BTC"
    quote: str         # actual quote currency on the venue, e.g. "USDT"
    bid: float
    ask: float
    ts: float = field(default_factory=time.time)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Opportunity:
    """A buy-here / sell-there pair for one asset.

    Prices are per unit of the base asset. ``net_bps`` nets out both venues'
    taker fees and the configured transfer haircut; positive means the
    round-trip is profitable on paper.
    """

    base: str
    buy_exchange: str
    buy_quote: str
    buy_price: float       # ask on the buy venue
    sell_exchange: str
    sell_quote: str
    sell_price: float      # bid on the sell venue
    gross_bps: float
    net_bps: float
    cross_quote: bool      # True when buy/sell quote currencies differ
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExchangeStatus:
    """Health of one exchange feed as seen by the poller."""

    name: str
    region: str
    ok: bool = False
    last_success: float | None = None
    latency_ms: float | None = None
    error: str | None = None
    quotes: int = 0

    def to_dict(self) -> dict:
        return asdict(self)
