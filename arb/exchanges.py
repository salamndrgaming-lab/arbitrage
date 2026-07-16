"""Live exchange adapters over public (keyless) REST endpoints.

Each adapter fetches best bid/ask for the configured base assets and
normalizes them into ``Quote`` objects. Adapters raise ``ExchangeError`` on
any failure; the poller records it as that venue's status without breaking
the cycle. All venues here quote against USD or USDT — the engine's
quote-equivalence groups make them comparable while flagging the mismatch.
"""

from __future__ import annotations

import json
import time

import httpx

from .models import Quote

USER_AGENT = "arb-tracker/0.1 (+https://github.com/salamndrgaming-lab/arbitrage)"


class ExchangeError(RuntimeError):
    pass


class Adapter:
    """Base class: subclasses set metadata and implement ``fetch``."""

    name = "base"
    region = "?"

    async def fetch(self, client: httpx.AsyncClient, assets: list[str]) -> list[Quote]:
        raise NotImplementedError

    async def _get_json(self, client: httpx.AsyncClient, url: str, **kwargs):
        try:
            resp = await client.get(url, headers={"User-Agent": USER_AGENT}, **kwargs)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            raise ExchangeError(
                f"{self.name}: HTTP {exc.response.status_code} for {url}"
            ) from exc
        except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
            raise ExchangeError(f"{self.name}: {exc}") from exc


class Binance(Adapter):
    name = "binance"
    region = "Global"

    async def fetch(self, client, assets):
        symbols = json.dumps([f"{a}USDT" for a in assets], separators=(",", ":"))
        data = await self._get_json(
            client,
            "https://api.binance.com/api/v3/ticker/bookTicker",
            params={"symbols": symbols},
        )
        now = time.time()
        quotes = []
        for row in data:
            base = row["symbol"].removesuffix("USDT")
            if base in assets:
                quotes.append(Quote(self.name, base, "USDT",
                                    float(row["bidPrice"]), float(row["askPrice"]), now))
        return quotes


class Kraken(Adapter):
    name = "kraken"
    region = "US"

    # Kraken uses legacy codes for some assets.
    CODES = {"BTC": "XBT", "DOGE": "XDG"}

    async def fetch(self, client, assets):
        code_to_base = {self.CODES.get(a, a): a for a in assets}
        pair_arg = ",".join(f"{code}USD" for code in code_to_base)
        data = await self._get_json(
            client, "https://api.kraken.com/0/public/Ticker", params={"pair": pair_arg}
        )
        if data.get("error"):
            raise ExchangeError(f"{self.name}: {data['error']}")
        now = time.time()
        quotes = []
        for key, tick in data.get("result", {}).items():
            # Result keys are padded ("XXBTZUSD"); match by contained code.
            base = next((b for c, b in code_to_base.items() if c in key), None)
            if base:
                quotes.append(Quote(self.name, base, "USD",
                                    float(tick["b"][0]), float(tick["a"][0]), now))
        return quotes


class Coinbase(Adapter):
    name = "coinbase"
    region = "US"

    async def fetch(self, client, assets):
        now = time.time()
        quotes = []
        for asset in assets:
            data = await self._get_json(
                client, f"https://api.exchange.coinbase.com/products/{asset}-USD/ticker"
            )
            if "bid" in data and "ask" in data:
                quotes.append(Quote(self.name, asset, "USD",
                                    float(data["bid"]), float(data["ask"]), now))
        return quotes


class Bitstamp(Adapter):
    name = "bitstamp"
    region = "EU"

    async def fetch(self, client, assets):
        now = time.time()
        quotes = []
        for asset in assets:
            data = await self._get_json(
                client, f"https://www.bitstamp.net/api/v2/ticker/{asset.lower()}usd/"
            )
            if "bid" in data and "ask" in data:
                quotes.append(Quote(self.name, asset, "USD",
                                    float(data["bid"]), float(data["ask"]), now))
        return quotes


class Bitfinex(Adapter):
    name = "bitfinex"
    region = "BVI"

    @staticmethod
    def _symbol(asset: str) -> str:
        # Bases longer than 3 chars need a colon separator (tDOGE:USD).
        return f"t{asset}:USD" if len(asset) > 3 else f"t{asset}USD"

    async def fetch(self, client, assets):
        sym_to_base = {self._symbol(a): a for a in assets}
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
                                    float(row[1]), float(row[3]), now))
        return quotes


class KuCoin(Adapter):
    name = "kucoin"
    region = "Seychelles"

    async def fetch(self, client, assets):
        data = await self._get_json(
            client, "https://api.kucoin.com/api/v1/market/allTickers"
        )
        wanted = {f"{a}-USDT": a for a in assets}
        now = time.time()
        quotes = []
        for tick in data.get("data", {}).get("ticker", []):
            base = wanted.get(tick.get("symbol"))
            if base and tick.get("buy") and tick.get("sell"):
                quotes.append(Quote(self.name, base, "USDT",
                                    float(tick["buy"]), float(tick["sell"]), now))
        return quotes


class OKX(Adapter):
    name = "okx"
    region = "Asia"

    async def fetch(self, client, assets):
        data = await self._get_json(
            client,
            "https://www.okx.com/api/v5/market/tickers",
            params={"instType": "SPOT"},
        )
        wanted = {f"{a}-USDT": a for a in assets}
        now = time.time()
        quotes = []
        for tick in data.get("data", []):
            base = wanted.get(tick.get("instId"))
            if base and tick.get("bidPx") and tick.get("askPx"):
                quotes.append(Quote(self.name, base, "USDT",
                                    float(tick["bidPx"]), float(tick["askPx"]), now))
        return quotes


class Gateio(Adapter):
    name = "gateio"
    region = "Asia"

    async def fetch(self, client, assets):
        data = await self._get_json(client, "https://api.gateio.ws/api/v4/spot/tickers")
        wanted = {f"{a}_USDT": a for a in assets}
        now = time.time()
        quotes = []
        for tick in data:
            base = wanted.get(tick.get("currency_pair"))
            if base and tick.get("highest_bid") and tick.get("lowest_ask"):
                quotes.append(Quote(self.name, base, "USDT",
                                    float(tick["highest_bid"]), float(tick["lowest_ask"]), now))
        return quotes


ADAPTERS: dict[str, type[Adapter]] = {
    cls.name: cls
    for cls in (Binance, Kraken, Coinbase, Bitstamp, Bitfinex, KuCoin, OKX, Gateio)
}


def build_adapters(names: list[str]) -> dict[str, Adapter]:
    unknown = [n for n in names if n not in ADAPTERS]
    if unknown:
        raise ValueError(f"unknown exchanges: {unknown}; known: {sorted(ADAPTERS)}")
    return {n: ADAPTERS[n]() for n in names}
