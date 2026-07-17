"""Serverless app tests (arb.webapp) using stub adapters."""

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

import arb.webapp as webapp
from arb.poller import Poller
from test_system import StubReference, StubVenue, make_cfg


@pytest.fixture
def client(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    cfg.poll_interval = 15
    monkeypatch.setattr(webapp, "scanner", Poller(cfg, adapters={
        "cheap": StubVenue("cheap", 0),
        "rich": StubVenue("rich", 80),
        "ref": StubReference(),
    }))
    monkeypatch.setattr(webapp, "MIN_SCAN_GAP", 3.0)
    with TestClient(webapp.app) as c:
        yield c


def test_scan_fetches_and_returns_everything(client):
    data = client.get("/api/scan").json()
    st = data["status"]
    assert st["history_available"] is False
    assert st["poll_interval"] == 15
    assert st["cycles"] == 1
    assert data["opportunities"]
    assert all(o["executable"] for o in data["opportunities"])
    assert set(data["best_spreads"]) == {"BTC", "EUR"}
    assert data["quotes"]


def test_scan_gap_reuses_last_cycle(client):
    client.get("/api/scan")
    data = client.get("/api/scan").json()  # immediately again: within MIN_SCAN_GAP
    assert data["status"]["cycles"] == 1
    assert data["quotes"]  # still served from the last cycle


def test_index_and_static(client):
    assert "<title>Market Arbitrage Tracker</title>" in client.get("/").text
    assert client.get("/api/status").json()["history_available"] is False


def test_trading_proxy_unconfigured(client, monkeypatch):
    monkeypatch.setattr(webapp, "CONTROL_URL", "")
    d = client.get("/api/trading/status").json()
    assert d["connected"] is False and "ARB_CONTROL_URL" in d["reason"]
    assert client.post("/api/trading/kill").status_code == 503
    assert client.post("/api/trading/resume").status_code == 503


def test_trading_proxy_configured(client, monkeypatch):
    monkeypatch.setattr(webapp, "CONTROL_URL", "http://control.example")
    calls = []

    async def fake_control(method, path, client_token=None):
        calls.append((method, path, client_token))
        if path.endswith("/status"):
            return 200, {"configured": True, "kill_switch": False}
        return 200, {"kill_switch": path.endswith("/kill")}

    monkeypatch.setattr(webapp, "_control_request", fake_control)

    d = client.get("/api/trading/status").json()
    assert d["connected"] is True and d["configured"] is True

    assert client.post("/api/trading/kill").json()["kill_switch"] is True
    r = client.post("/api/trading/resume", headers={"X-Control-Token": "tok"})
    assert r.json()["kill_switch"] is False
    # The client-supplied token is forwarded to the control endpoint.
    assert ("POST", "/api/trading/resume", "tok") in calls
