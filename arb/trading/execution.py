"""Authenticated execution adapters — these place REAL orders.

v1 implements Binance and Kraken spot with immediate-or-cancel limit
orders: an IOC at the quoted price either fills (fully or partially) at
that price or better, or cancels — it can never chase the market. Both
venues must have pre-positioned balances (this strategy holds inventory on
both sides and never transfers mid-trade).

Credentials come only from the environment (``ARB_BINANCE_API_KEY`` /
``ARB_BINANCE_API_SECRET``, ``ARB_KRAKEN_API_KEY`` / ``ARB_KRAKEN_API_SECRET``)
— never from config files, never logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time
import urllib.parse
from dataclasses import dataclass

import httpx


class ExecutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderResult:
    venue: str
    side: str            # "buy" | "sell"
    symbol: str
    requested_qty: float
    filled_qty: float
    price: float
    order_id: str
    raw_status: str

    @property
    def fully_filled(self) -> bool:
        return self.filled_qty >= self.requested_qty * 0.999


class ExecutionVenue:
    """Base: subclasses implement signing, balances and IOC limit orders."""

    name = "base"

    @classmethod
    def from_env(cls) -> "ExecutionVenue":
        prefix = f"ARB_{cls.name.upper()}"
        key = os.environ.get(f"{prefix}_API_KEY")
        secret = os.environ.get(f"{prefix}_API_SECRET")
        if not key or not secret:
            raise ExecutionError(
                f"{cls.name}: missing {prefix}_API_KEY / {prefix}_API_SECRET"
            )
        return cls(key, secret)

    def __init__(self, api_key: str, api_secret: str):
        self._key = api_key
        self._secret = api_secret

    async def balances(self, client: httpx.AsyncClient) -> dict[str, float]:
        """Free balances by currency code (uppercase)."""
        raise NotImplementedError

    async def place_ioc_limit(
        self, client: httpx.AsyncClient, base: str, quote: str,
        side: str, qty: float, price: float,
    ) -> OrderResult:
        raise NotImplementedError


class BinanceExecution(ExecutionVenue):
    name = "binance"
    BASE = "https://api.binance.com"

    def _signed(self, params: dict) -> dict:
        params = {**params, "timestamp": int(time.time() * 1000), "recvWindow": 5000}
        query = urllib.parse.urlencode(params)
        sig = hmac.new(self._secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        params["signature"] = sig
        return params

    def _headers(self) -> dict:
        return {"X-MBX-APIKEY": self._key}

    async def balances(self, client):
        resp = await client.get(
            f"{self.BASE}/api/v3/account",
            params=self._signed({}), headers=self._headers(),
        )
        if resp.status_code != 200:
            raise ExecutionError(f"binance balances: HTTP {resp.status_code} {resp.text[:200]}")
        return {
            b["asset"].upper(): float(b["free"])
            for b in resp.json().get("balances", [])
            if float(b["free"]) > 0
        }

    async def place_ioc_limit(self, client, base, quote, side, qty, price):
        symbol = f"{base}{quote}"
        params = self._signed({
            "symbol": symbol,
            "side": side.upper(),
            "type": "LIMIT",
            "timeInForce": "IOC",
            "quantity": f"{qty:.8f}".rstrip("0").rstrip("."),
            "price": f"{price:.8f}".rstrip("0").rstrip("."),
            "newOrderRespType": "RESULT",
        })
        resp = await client.post(
            f"{self.BASE}/api/v3/order", params=params, headers=self._headers()
        )
        if resp.status_code != 200:
            raise ExecutionError(f"binance order: HTTP {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        return OrderResult(
            venue=self.name, side=side, symbol=symbol,
            requested_qty=qty,
            filled_qty=float(data.get("executedQty", 0)),
            price=price,
            order_id=str(data.get("orderId", "")),
            raw_status=data.get("status", "?"),
        )


class KrakenExecution(ExecutionVenue):
    name = "kraken"
    BASE = "https://api.kraken.com"
    CODES = {"BTC": "XBT", "DOGE": "XDG"}
    # Kraken balance codes back to canonical.
    BAL_CODES = {"XXBT": "BTC", "XBT": "BTC", "XXDG": "DOGE", "XDG": "DOGE",
                 "ZUSD": "USD", "ZEUR": "EUR", "ZGBP": "GBP", "ZAUD": "AUD"}

    def _sign(self, path: str, data: dict) -> dict:
        data = {**data, "nonce": int(time.time() * 1000)}
        postdata = urllib.parse.urlencode(data)
        message = path.encode() + hashlib.sha256(
            str(data["nonce"]).encode() + postdata.encode()
        ).digest()
        sig = hmac.new(base64.b64decode(self._secret), message, hashlib.sha512)
        headers = {
            "API-Key": self._key,
            "API-Sign": base64.b64encode(sig.digest()).decode(),
        }
        return {"data": data, "headers": headers}

    async def _private(self, client, path: str, data: dict) -> dict:
        signed = self._sign(path, data)
        resp = await client.post(
            f"{self.BASE}{path}", data=signed["data"], headers=signed["headers"]
        )
        if resp.status_code != 200:
            raise ExecutionError(f"kraken: HTTP {resp.status_code} {resp.text[:200]}")
        payload = resp.json()
        if payload.get("error"):
            raise ExecutionError(f"kraken: {payload['error']}")
        return payload.get("result", {})

    async def balances(self, client):
        result = await self._private(client, "/0/private/Balance", {})
        out: dict[str, float] = {}
        for code, amount in result.items():
            canon = self.BAL_CODES.get(code, code.lstrip("XZ") or code).upper()
            out[canon] = out.get(canon, 0.0) + float(amount)
        return {k: v for k, v in out.items() if v > 0}

    async def place_ioc_limit(self, client, base, quote, side, qty, price):
        pair = f"{self.CODES.get(base, base)}{quote}"
        result = await self._private(client, "/0/private/AddOrder", {
            "pair": pair,
            "type": side.lower(),
            "ordertype": "limit",
            "price": f"{price:.6f}".rstrip("0").rstrip("."),
            "volume": f"{qty:.8f}".rstrip("0").rstrip("."),
            "timeinforce": "IOC",
        })
        txids = result.get("txid", [])
        # Kraken AddOrder does not return fill qty synchronously; query it.
        filled = qty
        if txids:
            try:
                orders = await self._private(
                    client, "/0/private/QueryOrders", {"txid": ",".join(txids)}
                )
                filled = sum(float(o.get("vol_exec", 0)) for o in orders.values())
            except ExecutionError:
                filled = 0.0  # unknown -> treat as unfilled so the trader flags it
        return OrderResult(
            venue=self.name, side=side, symbol=pair,
            requested_qty=qty, filled_qty=filled, price=price,
            order_id=",".join(txids) or "?",
            raw_status="submitted",
        )


EXECUTION_VENUES: dict[str, type[ExecutionVenue]] = {
    cls.name: cls for cls in (BinanceExecution, KrakenExecution)
}


def build_executors(venues: list[str]) -> dict[str, ExecutionVenue]:
    """Instantiate executors from environment credentials.

    Raises if a requested venue has no execution support or no credentials —
    arming must be all-or-nothing, never silently partial.
    """
    executors = {}
    for name in venues:
        cls = EXECUTION_VENUES.get(name)
        if cls is None:
            raise ExecutionError(
                f"no execution support for venue {name!r};"
                f" supported: {sorted(EXECUTION_VENUES)}"
            )
        executors[name] = cls.from_env()
    return executors
