"""Core data types shared across the tracker."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict


@dataclass(frozen=True)
class Quote:
    """Best bid/ask for one asset on one venue.

    ``market`` groups instruments ("crypto", "fx", "metals"); quotes are only
    ever compared within the same market. Reference feeds publish a single
    rate — for those bid == ask.
    """

    exchange: str
    base: str          # e.g. "BTC", "EUR", "XAU"
    quote: str         # actual quote currency on the venue, e.g. "USDT"
    bid: float
    ask: float
    ts: float = field(default_factory=time.time)
    market: str = "crypto"

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
    round-trip is profitable on paper. ``executable`` is True only when both
    legs are tradable venues (not reference-rate feeds).
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
    market: str = "crypto"
    executable: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExchangeStatus:
    """Health of one venue feed as seen by the poller."""

    name: str
    region: str
    ok: bool = False
    last_success: float | None = None
    latency_ms: float | None = None
    error: str | None = None
    quotes: int = 0
    tradable: bool = True
    markets: list[str] = field(default_factory=lambda: ["crypto"])

    def to_dict(self) -> dict:
        return asdict(self)
