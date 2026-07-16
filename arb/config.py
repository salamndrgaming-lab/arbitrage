"""Configuration loading with embedded defaults.

``config.yaml`` in the working directory (or the path in ``ARB_CONFIG``)
overrides the defaults; the file is optional.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_MARKETS: dict[str, list[str]] = {
    "crypto": ["BTC", "ETH", "SOL", "XRP", "LTC", "DOGE", "ADA"],
    "fx": ["EUR", "GBP", "AUD"],
    "metals": ["XAU", "XAG"],
}

# name -> (region, default taker fee in bps). Reference-rate feeds have no fee.
DEFAULT_EXCHANGES: dict[str, tuple[str, float]] = {
    "binance": ("Global", 10),
    "kraken": ("US", 26),
    "coinbase": ("US", 60),
    "bitstamp": ("EU", 40),
    "bitfinex": ("BVI", 20),
    "kucoin": ("Seychelles", 10),
    "okx": ("Asia", 10),
    "gateio": ("Asia", 20),
    "frankfurter": ("ECB / EU", 0),
    "openerapi": ("Global", 0),
    "currencyapi": ("Global", 0),
    "stooq": ("EU", 0),
}

DEFAULT_QUOTE_EQUIVALENCE = [["USD", "USDT", "USDC"]]


@dataclass
class ExchangeConfig:
    name: str
    region: str
    enabled: bool = True
    taker_fee_bps: float = 10.0


@dataclass
class Config:
    poll_interval: float = 5.0
    min_net_bps: float = 10.0
    transfer_haircut_bps: float = 5.0
    markets: dict[str, list[str]] = field(
        default_factory=lambda: {m: list(a) for m, a in DEFAULT_MARKETS.items()}
    )
    quote_equivalence: list[list[str]] = field(
        default_factory=lambda: [list(g) for g in DEFAULT_QUOTE_EQUIVALENCE]
    )
    exchanges: dict[str, ExchangeConfig] = field(default_factory=dict)
    db_path: str = "arb.sqlite3"
    history_retention_hours: float = 72.0

    def __post_init__(self) -> None:
        if not self.exchanges:
            self.exchanges = {
                name: ExchangeConfig(name=name, region=region, taker_fee_bps=fee)
                for name, (region, fee) in DEFAULT_EXCHANGES.items()
            }

    @property
    def enabled_exchanges(self) -> dict[str, ExchangeConfig]:
        return {n: c for n, c in self.exchanges.items() if c.enabled}

    @property
    def all_assets(self) -> list[str]:
        return [a for assets in self.markets.values() for a in assets]

    def fees_bps(self) -> dict[str, float]:
        return {n: c.taker_fee_bps for n, c in self.enabled_exchanges.items()}

    def quote_group(self, quote: str) -> str:
        """Return a stable group id for a quote currency (its first member)."""
        for group in self.quote_equivalence:
            if quote in group:
                return group[0]
        return quote


def load_config(path: str | os.PathLike | None = None) -> Config:
    """Load config.yaml if present, otherwise return defaults.

    Environment overrides: ``ARB_CONFIG`` (file path), ``ARB_DB``
    (database path).
    """
    candidates = [path, os.environ.get("ARB_CONFIG"), "config.yaml"]
    raw: dict = {}
    for cand in candidates:
        if cand and Path(cand).is_file():
            raw = yaml.safe_load(Path(cand).read_text()) or {}
            break

    markets = {m: list(a) for m, a in DEFAULT_MARKETS.items()}
    if "markets" in raw:
        markets = {}
        for market, opts in raw["markets"].items():
            assets = opts.get("assets", []) if isinstance(opts, dict) else (opts or [])
            markets[market.lower()] = [a.upper() for a in assets]

    cfg = Config(
        poll_interval=float(raw.get("poll_interval", 5)),
        min_net_bps=float(raw.get("min_net_bps", 10)),
        transfer_haircut_bps=float(raw.get("transfer_haircut_bps", 5)),
        markets=markets,
        quote_equivalence=[
            [q.upper() for q in group]
            for group in raw.get("quote_equivalence", DEFAULT_QUOTE_EQUIVALENCE)
        ],
        db_path=raw.get("db_path", "arb.sqlite3"),
        history_retention_hours=float(raw.get("history_retention_hours", 72)),
    )

    raw_exchanges = raw.get("exchanges")
    if raw_exchanges:
        cfg.exchanges = {}
        for name, opts in raw_exchanges.items():
            name = name.lower()
            region, default_fee = DEFAULT_EXCHANGES.get(name, ("?", 10))
            opts = opts or {}
            cfg.exchanges[name] = ExchangeConfig(
                name=name,
                region=opts.get("region", region),
                enabled=bool(opts.get("enabled", True)),
                taker_fee_bps=float(opts.get("taker_fee_bps", default_fee)),
            )

    if os.environ.get("ARB_DB"):
        cfg.db_path = os.environ["ARB_DB"]
    return cfg
