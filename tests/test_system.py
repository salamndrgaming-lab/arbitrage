"""Poller / store / API tests using stub adapters (no network)."""

import asyncio

import httpx
import pytest

from arb.config import Config
from arb.exchanges import Adapter, ExchangeError
from arb.models import Quote
from arb.poller import Poller
from arb.store import Store


class StubVenue(Adapter):
    """Tradable venue returning fixed quotes; counts fetches."""

    markets = ("crypto", "fx")

    def __init__(self, name, price_offset=0.0):
        self.name = name
        self.offset = price_offset
        self.fetches = 0

    async def fetch(self, client, assets):
        self.fetches += 1
        out = []
        for market, names in assets.items():
            for a in names:
                base_px = 100_000.0 if market == "crypto" else 1.08
                px = base_px * (1 + self.offset / 10_000)
                out.append(Quote(self.name, a, "USD", px * 0.9999, px * 1.0001,
                                 market=market))
        return out


class StubReference(Adapter):
    markets = ("fx",)
    tradable = False
    min_interval = 3600.0

    def __init__(self, name="ref"):
        self.name = name
        self.fetches = 0

    async def fetch(self, client, assets):
        self.fetches += 1
        return [Quote(self.name, a, "USD", 1.09, 1.09, market="fx")
                for a in assets.get("fx", [])]


class StubBroken(Adapter):
    markets = ("crypto",)

    def __init__(self, name="broken"):
        self.name = name

    async def fetch(self, client, assets):
        raise ExchangeError(f"{self.name}: HTTP 451")


def make_cfg(tmp_path) -> Config:
    cfg = Config(poll_interval=0.01, min_net_bps=10)
    cfg.markets = {"crypto": ["BTC"], "fx": ["EUR"]}
    cfg.db_path = str(tmp_path / "test.sqlite3")
    return cfg


def run_cycles(poller, n):
    async def go():
        async with httpx.AsyncClient() as client:
            for _ in range(n):
                await poller.run_cycle(client)
    asyncio.run(go())


def test_poller_detects_spread_and_persists(tmp_path):
    cfg = make_cfg(tmp_path)
    store = Store(cfg.db_path)
    adapters = {
        "cheap": StubVenue("cheap", price_offset=0),
        "rich": StubVenue("rich", price_offset=80),   # 80 bps higher
        "ref": StubReference(),
    }
    poller = Poller(cfg, store, adapters=adapters)
    run_cycles(poller, 3)

    assert poller.cycles == 3
    assert all(s.ok for s in poller.statuses.values())
    assert poller.statuses["ref"].tradable is False

    # Opportunities exist for both markets, tradable legs only.
    assert poller.opportunities
    for o in poller.opportunities:
        assert o.buy_exchange == "cheap" and o.sell_exchange == "rich"
        assert o.executable
    markets = {o.market for o in poller.opportunities}
    assert markets == {"crypto", "fx"}

    hist = store.spread_history("BTC", hours=1)
    assert len(hist) == 3
    recent = store.recent_opportunities(hours=1)
    assert recent and {"market", "base", "net_bps"} <= set(recent[0])
    store.close()


def test_reference_feed_respects_min_interval(tmp_path):
    cfg = make_cfg(tmp_path)
    ref = StubReference()
    venue = StubVenue("v1")
    poller = Poller(cfg, adapters={"v1": venue, "ref": ref})
    run_cycles(poller, 5)

    assert venue.fetches == 5          # every cycle
    assert ref.fetches == 1            # cached within min_interval
    assert any(q.exchange == "ref" for qs in poller.quotes.values() for q in qs)


def test_failing_venue_degrades_not_fatal(tmp_path):
    cfg = make_cfg(tmp_path)
    poller = Poller(cfg, adapters={"ok": StubVenue("ok"), "broken": StubBroken()})
    run_cycles(poller, 2)

    assert poller.statuses["ok"].ok is True
    assert poller.statuses["broken"].ok is False
    assert "451" in poller.statuses["broken"].error
    snap = poller.snapshot()
    assert {e["name"]: e["ok"] for e in snap["exchanges"]} == {"ok": True, "broken": False}


def test_store_prune(tmp_path):
    from arb.models import Opportunity

    store = Store(str(tmp_path / "p.sqlite3"))
    old = Opportunity(
        base="BTC", buy_exchange="a", buy_quote="USD", buy_price=1.0,
        sell_exchange="b", sell_quote="USD", sell_price=1.1,
        gross_bps=100, net_bps=90, cross_quote=False, ts=1.0,
    )
    store.record_opportunities([old])
    store.prune(retention_hours=1)
    assert store.recent_opportunities(hours=1e6) == []
    store.close()


def test_api_endpoints(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ARB_DB", str(tmp_path / "api.sqlite3"))
    import importlib
    import arb.server as server
    importlib.reload(server)

    cfg = make_cfg(tmp_path)
    cfg.db_path = str(tmp_path / "api.sqlite3")
    server.poller = Poller(cfg, server.store, adapters={
        "cheap": StubVenue("cheap", 0),
        "rich": StubVenue("rich", 80),
        "ref": StubReference(),
    })
    server.cfg = cfg
    run_cycles(server.poller, 3)

    # TestClient triggers lifespan (poller task); endpoints read shared state.
    with TestClient(server.app) as client:
        st = client.get("/api/status").json()
        assert st["markets"] == {"crypto": ["BTC"], "fx": ["EUR"]}
        assert {e["name"] for e in st["exchanges"]} == {"cheap", "rich", "ref"}
        assert st["history_available"] is True

        scan = client.get("/api/scan").json()
        assert scan["status"]["history_available"] is True
        assert scan["opportunities"] and scan["quotes"]
        assert set(scan["best_spreads"]) == {"BTC", "EUR"}

        opps = client.get("/api/opportunities").json()
        assert opps["opportunities"]
        fx_only = client.get("/api/opportunities?market=fx").json()["opportunities"]
        assert fx_only and all(o["market"] == "fx" for o in fx_only)

        quotes = client.get("/api/quotes?asset=eur").json()["quotes"]
        assert quotes and all(q["base"] == "EUR" for q in quotes)

        hist = client.get("/api/history?asset=EUR&hours=1").json()
        assert hist["asset"] == "EUR" and len(hist["points"]) >= 3
        assert "executable" in hist["points"][0]

        assert client.get("/api/history?asset=NOPE").status_code == 404
        assert "<title>Market Arbitrage Tracker</title>" in client.get("/").text
    server.store.close()


def test_trading_control_endpoints(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ARB_DB", str(tmp_path / "ctl.sqlite3"))
    monkeypatch.delenv("ARB_CONTROL_TOKEN", raising=False)
    import importlib
    import arb.server as server
    importlib.reload(server)

    cfg = make_cfg(tmp_path)
    cfg.trading.kill_switch_file = str(tmp_path / "KILL")
    server.cfg = cfg
    server.poller = Poller(cfg, server.store, adapters={"v": StubVenue("v")})

    with TestClient(server.app) as client:
        st = client.get("/api/trading/status").json()
        assert st["connected"] is True
        assert st["configured"] is False  # trading disabled by default
        assert st["kill_switch"] is False
        assert "max_trade_notional_usd" in st["limits"]
        assert client.get("/api/trades").json()["trades"] == []

        # Engaging the kill switch needs no token — emergency stop.
        assert client.post("/api/trading/kill").json()["kill_switch"] is True
        assert (tmp_path / "KILL").exists()
        assert client.get("/api/trading/status").json()["kill_switch"] is True

        # Releasing is token-gated when a control token is configured.
        monkeypatch.setenv("ARB_CONTROL_TOKEN", "sekrit")
        assert client.post("/api/trading/resume").status_code == 403
        assert (tmp_path / "KILL").exists()
        ok = client.post("/api/trading/resume",
                         headers={"X-Control-Token": "sekrit"})
        assert ok.status_code == 200
        assert not (tmp_path / "KILL").exists()

        # Without a configured token, release is open (local single-user).
        monkeypatch.delenv("ARB_CONTROL_TOKEN")
        client.post("/api/trading/kill")
        assert client.post("/api/trading/resume").status_code == 200
    server.store.close()
