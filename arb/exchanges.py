"""Venue adapters over public (keyless) REST endpoints.

Two kinds of venue:

- **Tradable exchanges** (``tradable = True``): real order books with
  bid/ask. Crypto venues also carry FX and tokenized-metal instruments
  where listed (Kraken/Bitstamp list fiat EUR/USD, GBP/USD; Binance,
  KuCoin and Gate.io list PAXG, gold 1:1 token, mapped to XAU).
- **Reference-rate feeds** (``tradable = False``): FX/metals fixings from
  independent publishers (ECB via Frankfurter, open.er-api, currency-api,
  Stooq). They publish a single rate (bid == ask), refresh slowly
  (``min_interval``), and are used for divergence tracking — the engine
  never counts them as an arbitrage leg.

Each adapter fetches the assets for the markets it serves and normalizes
them into ``Quote`` objects. Adapters raise ``ExchangeError`` on failure;
the poller records it as that venue's status without breaking the cycle.
"""

from __future__ import annotations

import json
import time

import httpx

from .models import Quote

USER_AGENT = "arb-tracker/0.1 (+https://github.com/salamndrgaming-lab/arbitrage)"

Assets = dict[str, list[str]]  # market -> assets, filtered to the adapter's markets


class ExchangeError(RuntimeError):
    pass


class Adapter:
    """Base class: subclasses set metadata and implement ``fetch``."""

    name = "base"
    region = "?"
    markets: tuple[str, ...] = ("crypto",)
    tradable = True
    min_interval = 0.0  # seconds between fetches; 0 = every poll cycle

    async def fetch(self, client: httpx.AsyncClient, assets: Assets) -> list[Quote]:
        raise NotImplementedError

    async def _get(self, client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
        try:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT}, **kwargs)
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as exc:
            raise ExchangeError(
                f"{self.name}: HTTP {exc.response.status_code} for {url}"
            ) from exc
        except httpx.HTTPError as exc:
            raise ExchangeError(f"{self.name}: {exc}") from exc

    async def _get_json(self, client: httpx.AsyncClient, url: str, **kwargs):
        try:
            return (await self._get(client, url, **kwargs)).json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise ExchangeError(f"{self.name}: bad JSON from {url}: {exc}") from exc


# --------------------------------------------------------------------------
# Tradable crypto exchanges (some also list FX / tokenized metals)
# --------------------------------------------------------------------------


class _FetchAllVenue(Adapter):
    """Venues where we pull the full ticker list and filter locally.

    Absent instruments (delistings, regional gaps) are silently skipped
    instead of erroring the whole feed — important because FX/metal
    listings vary. ``_instrument`` maps (market, asset) -> native symbol
    or None.
    """

    quote_ccy = "USDT"

    def _instrument(self, market: str, asset: str) -> str | None:
        raise NotImplementedError

    def _wanted(self, assets: Assets) -> dict[str, tuple[str, str]]:
        wanted = {}
        for market, names in assets.items():
            for a in names:
                sym = self._instrument(market, a)
                if sym:
                    wanted[sym] = (a, market)
        return wanted


class Binance(_FetchAllVenue):
    name = "binance"
    region = "Global"
    markets = ("crypto", "fx", "metals")

    def _instrument(self, market, asset):
        if market == "metals":
            return "PAXGUSDT" if asset == "XAU" else None  # PAXG = 1 oz gold token
        return f"{asset}USDT"  # crypto and fx (Binance lists EURUSDT)

    async def fetch(self, client, assets):
        wanted = self._wanted(assets)
        data = await self._get_json(
            client, "https://api.binance.com/api/v3/ticker/bookTicker"
        )
        now = time.time()
        quotes = []
        for row in data:
            hit = wanted.get(row.get("symbol"))
            if hit:
                base, market = hit
                quotes.append(Quote(self.name, base, "USDT",
                                    float(row["bidPrice"]), float(row["askPrice"]),
                                    now, market))
        return quotes


class KuCoin(_FetchAllVenue):
    name = "kucoin"
    region = "Seychelles"
    markets = ("crypto", "metals")

    def _instrument(self, market, asset):
        if market == "metals":
            return "PAXG-USDT" if asset == "XAU" else None
        return f"{asset}-USDT"

    async def fetch(self, client, assets):
        wanted = self._wanted(assets)
        data = await self._get_json(
            client, "https://api.kucoin.com/api/v1/market/allTickers"
        )
        now = time.time()
        quotes = []
        for tick in data.get("data", {}).get("ticker", []):
            hit = wanted.get(tick.get("symbol"))
            if hit and tick.get("buy") and tick.get("sell"):
                base, market = hit
                quotes.append(Quote(self.name, base, "USDT",
                                    float(tick["buy"]), float(tick["sell"]),
                                    now, market))
        return quotes


class OKX(_FetchAllVenue):
    name = "okx"
    region = "Asia"
    markets = ("crypto", "fx", "metals")

    def _instrument(self, market, asset):
        if market == "metals":
            return "PAXG-USDT" if asset == "XAU" else None
        return f"{asset}-USDT"

    async def fetch(self, client, assets):
        wanted = self._wanted(assets)
        data = await self._get_json(
            client,
            "https://www.okx.com/api/v5/market/tickers",
            params={"instType": "SPOT"},
        )
        now = time.time()
        quotes = []
        for tick in data.get("data", []):
            hit = wanted.get(tick.get("instId"))
            if hit and tick.get("bidPx") and tick.get("askPx"):
                base, market = hit
                quotes.append(Quote(self.name, base, "USDT",
                                    float(tick["bidPx"]), float(tick["askPx"]),
                                    now, market))
        return quotes


class Gateio(_FetchAllVenue):
    name = "gateio"
    region = "Asia"
    markets = ("crypto", "fx", "metals")

    def _instrument(self, market, asset):
        if market == "metals":
            return "PAXG_USDT" if asset == "XAU" else None
        return f"{asset}_USDT"

    async def fetch(self, client, assets):
        wanted = self._wanted(assets)
        data = await self._get_json(client, "https://api.gateio.ws/api/v4/spot/tickers")
        now = time.time()
        quotes = []
        for tick in data:
            hit = wanted.get(tick.get("currency_pair"))
            if hit and tick.get("highest_bid") and tick.get("lowest_ask"):
                base, market = hit
                quotes.append(Quote(self.name, base, "USDT",
                                    float(tick["highest_bid"]), float(tick["lowest_ask"]),
                                    now, market))
        return quotes


class Kraken(Adapter):
    name = "kraken"
    region = "US"
    markets = ("crypto", "fx")  # Kraken lists real fiat pairs (EUR/USD, GBP/USD, AUD/USD)

    # Kraken uses legacy codes for some assets.
    CODES = {"BTC": "XBT", "DOGE": "XDG"}

    async def _ticker(self, client, code_to_base: dict[str, str], market: str) -> list[Quote]:
        pair_arg = ",".join(f"{code}USD" for code in code_to_base)
        data = await self._get_json(
            client, "https://api.kraken.com/0/public/Ticker", params={"pair": pair_arg}
        )
        if data.get("error"):
            raise ExchangeError(f"{self.name}: {data['error']}")
        now = time.time()
        quotes = []
        for key, tick in data.get("result", {}).items():
            # Result keys are padded ("XXBTZUSD", "ZEURZUSD"); match by contained code.
            base = next((b for c, b in code_to_base.items() if c in key), None)
            if base:
                quotes.append(Quote(self.name, base, "USD",
                                    float(tick["b"][0]), float(tick["a"][0]),
                                    now, market))
        return quotes

    async def fetch(self, client, assets):
        quotes: list[Quote] = []
        errors: list[str] = []
        # Separate calls per market so one bad pair list can't kill the other.
        for market in ("crypto", "fx"):
            names = assets.get(market)
            if not names:
                continue
            try:
                codes = {self.CODES.get(a, a): a for a in names}
                quotes.extend(await self._ticker(client, codes, market))
            except ExchangeError as exc:
                errors.append(str(exc))
        if errors and not quotes:
            raise ExchangeError("; ".join(errors))
        return quotes


class _PerPairVenue(Adapter):
    """Venues queried one instrument at a time; missing listings are skipped."""

    async def _pair(self, client, asset: str, market: str) -> Quote | None:
        raise NotImplementedError

    async def fetch(self, client, assets):
        quotes: list[Quote] = []
        errors: list[str] = []
        for market, names in assets.items():
            for asset in names:
                try:
                    q = await self._pair(client, asset, market)
                    if q:
                        quotes.append(q)
                except ExchangeError as exc:
                    errors.append(str(exc))
        if errors and not quotes:
            raise ExchangeError("; ".join(errors[:3]))
        return quotes


class Coinbase(_PerPairVenue):
    name = "coinbase"
    region = "US"
    markets = ("crypto",)

    async def _pair(self, client, asset, market):
        data = await self._get_json(
            client, f"https://api.exchange.coinbase.com/products/{asset}-USD/ticker"
        )
        if "bid" not in data or "ask" not in data:
            return None
        return Quote(self.name, asset, "USD",
                     float(data["bid"]), float(data["ask"]), time.time(), market)


class Bitstamp(_PerPairVenue):
    name = "bitstamp"
    region = "EU"
    markets = ("crypto", "fx")  # Bitstamp lists fiat EUR/USD and GBP/USD

    async def _pair(self, client, asset, market):
        data = await self._get_json(
            client, f"https://www.bitstamp.net/api/v2/ticker/{asset.lower()}usd/"
        )
        if "bid" not in data or "ask" not in data:
            return None
        return Quote(self.name, asset, "USD",
                     float(data["bid"]), float(data["ask"]), time.time(), market)


class Bitfinex(Adapter):
    name = "bitfinex"
    region = "BVI"
    markets = ("crypto",)

    @staticmethod
    def _symbol(asset: str) -> str:
        # Bases longer than 3 chars need a colon separator (tDOGE:USD).
        return f"t{asset}:USD" if len(asset) > 3 else f"t{asset}USD"

    async def fetch(self, client, assets):
        names = assets.get("crypto", [])
        sym_to_base = {self._symbol(a): a for a in names}
        data = await self._get_json(
            client,
            "https://api-pub.bitfinex.com/v2/tickers",
            params={"symbols": ",".join(sym_to_base)},
        )
        now = time.time()
        quotes = []
        for row in data:
            base = sym_to_base.get(row[0])
            if base:  # row: [SYMBOL, BID, BID_SIZE, ASK, ...]
                quotes.append(Quote(self.name, base, "USD",
                                    float(row[1]), float(row[3]), now, "crypto"))
        return quotes


# --------------------------------------------------------------------------
# Reference-rate feeds (not tradable; divergence tracking only)
# --------------------------------------------------------------------------


class _UsdRateFeed(Adapter):
    """Publishers of "1 USD = x ASSET" rates; we invert to ASSET/USD."""

    tradable = False

    def _rates(self, payload) -> dict[str, float]:
        """Return {ASSET: units of asset per 1 USD}."""
        raise NotImplementedError

    url = ""

    async def fetch(self, client, assets):
        payload = await self._get_json(client, self.url)
        rates = self._rates(payload)
        now = time.time()
        quotes = []
        for market, names in assets.items():
            for a in names:
                rate = rates.get(a)
                if rate and rate > 0:
                    px = 1 / rate
                    quotes.append(Quote(self.name, a, "USD", px, px, now, market))
        return quotes


class Frankfurter(_UsdRateFeed):
    """ECB reference rates via frankfurter.app (daily fixing)."""

    name = "frankfurter"
    region = "ECB / EU"
    markets = ("fx",)
    min_interval = 300.0
    url = "https://api.frankfurter.app/latest?base=USD"

    def _rates(self, payload):
        return {k.upper(): float(v) for k, v in payload.get("rates", {}).items()}


class OpenERAPI(_UsdRateFeed):
    """open.er-api.com aggregated FX rates (keyless tier)."""

    name = "openerapi"
    region = "Global"
    markets = ("fx",)
    min_interval = 600.0
    url = "https://open.er-api.com/v6/latest/USD"

    def _rates(self, payload):
        if payload.get("result") != "success":
            raise ExchangeError(f"{self.name}: {payload.get('error-type', 'bad response')}")
        return {k.upper(): float(v) for k, v in payload.get("rates", {}).items()}


class CurrencyAPI(_UsdRateFeed):
    """fawazahmed0 currency-api (jsdelivr CDN) — FX plus XAU/XAG."""

    name = "currencyapi"
    region = "Global"
    markets = ("fx", "metals")
    min_interval = 600.0
    url = ("https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest"
           "/v1/currencies/usd.min.json")

    def _rates(self, payload):
        return {k.upper(): float(v) for k, v in payload.get("usd", {}).items()
                if isinstance(v, (int, float))}


class Stooq(Adapter):
    """Stooq delayed quotes (CSV, keyless) — intraday FX and spot metals."""

    name = "stooq"
    region = "EU"
    markets = ("fx", "metals")
    tradable = False
    min_interval = 60.0

    async def fetch(self, client, assets):
        sym_to_hit = {}
        for market, names in assets.items():
            for a in names:
                sym_to_hit[f"{a.lower()}usd"] = (a, market)
        url = "https://stooq.com/q/l/"
        resp = await self._get(
            client, url,
            params={"s": ",".join(sym_to_hit), "f": "sd2t2ohlcv", "e": "csv"},
        )
        return self.parse_csv(resp.text, sym_to_hit)

    def parse_csv(self, text: str, sym_to_hit: dict[str, tuple[str, str]]) -> list[Quote]:
        now = time.time()
        quotes = []
        lines = [ln for ln in text.strip().splitlines() if ln]
        for line in lines[1:]:  # header: Symbol,Date,Time,Open,High,Low,Close,Volume
            cols = line.split(",")
            if len(cols) < 7:
                continue
            hit = sym_to_hit.get(cols[0].strip().lower())
            close = cols[6].strip()
            if not hit or close in ("", "N/D"):
                continue
            try:
                px = float(close)
            except ValueError:
                continue
            base, market = hit
            quotes.append(Quote(self.name, base, "USD", px, px, now, market))
        if not quotes:
            raise ExchangeError(f"{self.name}: no parseable rows in CSV response")
        return quotes


ADAPTERS: dict[str, type[Adapter]] = {
    cls.name: cls
    for cls in (Binance, Kraken, Coinbase, Bitstamp, Bitfinex, KuCoin, OKX, Gateio,
                Frankfurter, OpenERAPI, CurrencyAPI, Stooq)
}


def build_adapters(names: list[str]) -> dict[str, Adapter]:
    unknown = [n for n in names if n not in ADAPTERS]
    if unknown:
        raise ValueError(f"unknown exchanges: {unknown}; known: {sorted(ADAPTERS)}")
    return {n: ADAPTERS[n]() for n in names}
