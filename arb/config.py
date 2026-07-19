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
class TradingConfig:
    """Guardrails for live execution. Defaults are deliberately tiny.

    ``enabled`` alone does NOT arm trading — the environment variable
    ``ARB_TRADING_ARMED=I-ACCEPT-THE-RISK`` and venue API credentials are
    also required (see arb/trading/trader.py::arm_check).
    """

    enabled: bool = False
    venues: list[str] = field(default_factory=lambda: ["binance", "kraken"])
    assets: list[str] = field(default_factory=lambda: ["BTC", "ETH"])
    markets: list[str] = field(default_factory=lambda: ["crypto"])
    min_execute_bps: float = 30.0          # execute only well above display threshold
    max_trade_notional_usd: float = 100.0
    max_daily_notional_usd: float = 1000.0
    max_trades_per_day: int = 20
    max_daily_loss_usd: float = 50.0
    cooldown_seconds: float = 60.0
    max_quote_age_seconds: float = 3.0
    max_consecutive_failures: int = 3
    kill_switch_file: str = "TRADING_KILL_SWITCH"
    # Market-out the overfilled leg of a partial immediately, bounding
    # one-sided exposure to seconds. Disable only if you prefer to resolve
    # partials by hand — an unwind failure trips the circuit breaker.
    unwind_partials: bool = True
    # Live WS top-of-book feeds: re-verify the spread and cap size at the
    # displayed quantity right before firing. Advisory — with no fresh
    # book the trader falls back to REST quotes (stale-check still applies).
    use_ws_books: bool = True


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
    trading: TradingConfig = field(default_factory=TradingConfig)
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

    raw_trading = raw.get("trading")
    if raw_trading:
        t = TradingConfig()
        cfg.trading = TradingConfig(
            enabled=bool(raw_trading.get("enabled", False)),
            venues=[v.lower() for v in raw_trading.get("venues", t.venues)],
            assets=[a.upper() for a in raw_trading.get("assets", t.assets)],
            markets=[m.lower() for m in raw_trading.get("markets", t.markets)],
            min_execute_bps=float(raw_trading.get("min_execute_bps", t.min_execute_bps)),
            max_trade_notional_usd=float(
                raw_trading.get("max_trade_notional_usd", t.max_trade_notional_usd)),
            max_daily_notional_usd=float(
                raw_trading.get("max_daily_notional_usd", t.max_daily_notional_usd)),
            max_trades_per_day=int(
                raw_trading.get("max_trades_per_day", t.max_trades_per_day)),
            max_daily_loss_usd=float(
                raw_trading.get("max_daily_loss_usd", t.max_daily_loss_usd)),
            cooldown_seconds=float(raw_trading.get("cooldown_seconds", t.cooldown_seconds)),
            max_quote_age_seconds=float(
                raw_trading.get("max_quote_age_seconds", t.max_quote_age_seconds)),
            max_consecutive_failures=int(
                raw_trading.get("max_consecutive_failures", t.max_consecutive_failures)),
            kill_switch_file=raw_trading.get("kill_switch_file", t.kill_switch_file),
            unwind_partials=bool(raw_trading.get("unwind_partials", t.unwind_partials)),
            use_ws_books=bool(raw_trading.get("use_ws_books", t.use_ws_books)),
        )

    if os.environ.get("ARB_DB"):
        cfg.db_path = os.environ["ARB_DB"]
    return cfg
