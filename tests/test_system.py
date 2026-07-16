import asyncio

import pytest

from arb.config import Config, load_config
from arb.poller import Poller
from arb.sim import DemoFeed
from arb.store import Store


def demo_config(tmp_path) -> Config:
    cfg = Config(mode="demo", poll_interval=0.01, min_net_bps=10)
    cfg.db_path = str(tmp_path / "test.sqlite3")
    return cfg


def test_demo_feed_shape():
    feed = DemoFeed(["binance", "kraken"], ["BTC", "ETH"], seed=42)
    quotes = feed.tick()
    assert set(quotes) == {"binance", "kraken"}
    for qs in quotes.values():
        assert {q.base for q in qs} == {"BTC", "ETH"}
        for q in qs:
            assert 0 < q.bid < q.ask


def test_demo_feed_eventually_produces_dislocations():
    feed = DemoFeed(["a", "b", "c"], ["BTC"], seed=7)
    saw_dislocation = False
    for _ in range(200):
        feed.tick()
        if feed.dislocations:
            saw_dislocation = True
            break
    assert saw_dislocation


def test_poller_demo_cycle_and_store(tmp_path):
    cfg = demo_config(tmp_path)
    store = Store(cfg.db_path)
    poller = Poller(cfg, store)

    async def run():
        for _ in range(30):
            await poller.run_cycle()

    asyncio.run(run())

    assert poller.cycles == 30
    assert all(s.ok for s in poller.statuses.values())
    assert poller.best  # best spreads exist for every cycle
    snap = poller.snapshot()
    assert snap["mode"] == "demo"

    hist = store.spread_history("BTC", hours=1)
    assert len(hist) == 30
    assert {"ts", "net_bps", "gross_bps", "buy_exchange", "sell_exchange"} <= set(hist[0])
    store.close()


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
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    monkeypatch.setenv("ARB_MODE", "demo")
    monkeypatch.setenv("ARB_DB", str(tmp_path / "api.sqlite3"))
    import importlib
    import arb.server as server
    importlib.reload(server)

    async def seed():
        for _ in range(5):
            await server.poller.run_cycle()

    asyncio.run(seed())

    # TestClient triggers lifespan (poller task); endpoints read shared state.
    with TestClient(server.app) as client:
        st = client.get("/api/status").json()
        assert st["mode"] == "demo"
        assert len(st["exchanges"]) == 8

        opps = client.get("/api/opportunities").json()
        assert "opportunities" in opps and "best_spreads" in opps

        quotes = client.get("/api/quotes?asset=btc").json()["quotes"]
        assert quotes and all(q["base"] == "BTC" for q in quotes)

        hist = client.get("/api/history?asset=BTC&hours=1").json()
        assert hist["asset"] == "BTC" and len(hist["points"]) >= 5

        assert client.get("/api/history?asset=NOPE").status_code == 404
        assert "<title>Market Arbitrage Tracker</title>" in client.get("/").text
    server.store.close()
