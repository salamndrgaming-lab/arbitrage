"""Live top-of-book feeds over exchange WebSockets.

The REST poller gives ~poll-interval-old quotes; these feeds keep a
sub-second best bid/ask (with displayed size) for the pairs the trader can
execute. The trader uses them to (a) re-verify the spread with live prices
immediately before firing and (b) cap order size at the displayed
top-of-book quantity — never take more than the book shows.

Message parsing is separated from transport (``handle`` takes a decoded
dict) so it is testable without a network. The feeds are advisory: if a
socket is down or a book is stale the trader falls back to the REST
quotes, which remain guarded by the stale-quote check.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass

log = logging.getLogger("arb.books")

# Quote currency each execution venue trades against (spot).
VENUE_WS_QUOTES = {"binance": "USDT", "kraken": "USD"}


@dataclass(frozen=True)
class TopOfBook:
    bid: float
    bid_qty: float
    ask: float
    ask_qty: float
    ts: float


class LiveBooks:
    """Latest top-of-book per (venue, base, quote). Single-writer per key
    (each feed owns its venue), read from the trader's event loop."""

    def __init__(self) -> None:
        self._books: dict[tuple[str, str, str], TopOfBook] = {}

    def update(self, venue: str, base: str, quote: str, top: TopOfBook) -> None:
        self._books[(venue, base.upper(), quote.upper())] = top

    def top(self, venue: str, base: str, quote: str) -> TopOfBook | None:
        return self._books.get((venue, base.upper(), quote.upper()))

    def fresh_count(self, max_age: float, now: float | None = None) -> int:
        now = now or time.time()
        return sum(1 for t in self._books.values() if now - t.ts <= max_age)


class BookFeed:
    """Base: subclasses define the socket URL, subscription and parsing."""

    venue = "base"

    def __init__(self, books: LiveBooks, bases: list[str]):
        self.books = books
        self.bases = [b.upper() for b in bases]
        self.quote = VENUE_WS_QUOTES[self.venue]

    def url(self) -> str:
        raise NotImplementedError

    def subscribe_payload(self) -> str | None:
        """JSON to send after connecting, or None if the URL subscribes."""
        return None

    def handle(self, msg: dict) -> None:
        raise NotImplementedError

    async def run(self, stop: asyncio.Event) -> None:
        """Connect/reconnect loop. Requires the ``websockets`` package."""
        import websockets  # deferred: only the armed trader needs it

        backoff = 1.0
        while not stop.is_set():
            try:
                async with websockets.connect(self.url(), open_timeout=10) as ws:
                    payload = self.subscribe_payload()
                    if payload:
                        await ws.send(payload)
                    log.info("%s book feed connected (%s)", self.venue, self.bases)
                    backoff = 1.0
                    while not stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                        try:
                            self.handle(json.loads(raw))
                        except (ValueError, KeyError, TypeError) as exc:
                            log.debug("%s feed: bad message: %s", self.venue, exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("%s book feed dropped (%s); reconnecting in %.0fs",
                            self.venue, exc, backoff)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)


class BinanceBookFeed(BookFeed):
    """``<symbol>@bookTicker`` streams: best bid/ask with displayed size."""

    venue = "binance"
    WS_BASE = "wss://stream.binance.com:9443"

    def url(self) -> str:
        streams = "/".join(f"{b.lower()}{self.quote.lower()}@bookTicker"
                           for b in self.bases)
        return f"{self.WS_BASE}/stream?streams={streams}"

    def handle(self, msg: dict) -> None:
        data = msg.get("data", msg)
        symbol = data.get("s", "")
        if not symbol.endswith(self.quote):
            return
        base = symbol[: -len(self.quote)]
        if base not in self.bases:
            return
        self.books.update(self.venue, base, self.quote, TopOfBook(
            bid=float(data["b"]), bid_qty=float(data["B"]),
            ask=float(data["a"]), ask_qty=float(data["A"]),
            ts=time.time(),
        ))


class KrakenBookFeed(BookFeed):
    """Kraken WS v2 ``ticker`` channel: best bid/ask with displayed size."""

    venue = "kraken"
    WS_URL = "wss://ws.kraken.com/v2"

    def url(self) -> str:
        return self.WS_URL

    def subscribe_payload(self) -> str:
        return json.dumps({
            "method": "subscribe",
            "params": {
                "channel": "ticker",
                "symbol": [f"{b}/{self.quote}" for b in self.bases],
            },
        })

    def handle(self, msg: dict) -> None:
        if msg.get("channel") != "ticker":
            return
        for entry in msg.get("data", []):
            symbol = entry.get("symbol", "")
            base, _, quote = symbol.partition("/")
            if quote != self.quote or base not in self.bases:
                continue
            self.books.update(self.venue, base, self.quote, TopOfBook(
                bid=float(entry["bid"]), bid_qty=float(entry["bid_qty"]),
                ask=float(entry["ask"]), ask_qty=float(entry["ask_qty"]),
                ts=time.time(),
            ))


BOOK_FEEDS: dict[str, type[BookFeed]] = {
    cls.venue: cls for cls in (BinanceBookFeed, KrakenBookFeed)
}


def build_feeds(books: LiveBooks, venues: list[str],
                bases: list[str]) -> list[BookFeed]:
    """Feeds for every execution venue that has WS support; venues without
    support simply keep using REST quotes (the feeds are advisory)."""
    return [BOOK_FEEDS[v](books, bases) for v in venues if v in BOOK_FEEDS]
