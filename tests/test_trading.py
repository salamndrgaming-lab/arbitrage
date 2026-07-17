"""Trading layer tests: arming, risk gate, execution flow, circuit breaker.

Stub executors stand in for the authenticated venue APIs — tests must never
place real orders. Signing helpers are tested for determinism/shape only.
"""

import asyncio
import time

import httpx
import pytest

from arb.config import Config, TradingConfig
from arb.models import Opportunity, Quote
from arb.poller import Poller
from arb.store import Store
from arb.trading import ARMING_ENV, ARMING_PHRASE
from arb.trading.execution import (
    BinanceExecution,
    ExecutionError,
    ExecutionVenue,
    KrakenExecution,
    OrderResult,
    build_executors,
)
from arb.trading.risk import RiskManager
from arb.trading.trader import NotArmedError, Trader, arm_check
from test_system import StubVenue


def make_opp(net_bps=50.0, base="BTC", buy="kraken", sell="binance",
             ts=None, executable=True, market="crypto"):
    return Opportunity(
        base=base, buy_exchange=buy, buy_quote="USD", buy_price=100_000.0,
        sell_exchange=sell, sell_quote="USDT", sell_price=100_600.0,
        gross_bps=60.0, net_bps=net_bps, cross_quote=True,
        ts=ts if ts is not None else time.time(), market=market,
        executable=executable,
    )


def trading_cfg(tmp_path, **overrides) -> Config:
    cfg = Config(poll_interval=0.01)
    cfg.markets = {"crypto": ["BTC", "ETH"]}
    cfg.db_path = str(tmp_path / "t.sqlite3")
    defaults = dict(
        enabled=True, venues=["kraken", "binance"], assets=["BTC", "ETH"],
        min_execute_bps=30, max_trade_notional_usd=100,
        max_daily_notional_usd=1000, max_trades_per_day=20,
        max_daily_loss_usd=50, cooldown_seconds=60, max_quote_age_seconds=3,
        max_consecutive_failures=3,
        kill_switch_file=str(tmp_path / "KILL"),
    )
    defaults.update(overrides)
    cfg.trading = TradingConfig(**defaults)
    return cfg


# -- arming ----------------------------------------------------------------


def test_arm_check_requires_config_and_env(tmp_path, monkeypatch):
    cfg = trading_cfg(tmp_path)
    monkeypatch.delenv(ARMING_ENV, raising=False)
    with pytest.raises(NotArmedError, match=ARMING_ENV):
        arm_check(cfg)

    monkeypatch.setenv(ARMING_ENV, ARMING_PHRASE)
    arm_check(cfg)  # passes

    cfg.trading.enabled = False
    with pytest.raises(NotArmedError, match="enabled"):
        arm_check(cfg)


def test_build_executors_requires_credentials(monkeypatch):
    for var in ("ARB_BINANCE_API_KEY", "ARB_BINANCE_API_SECRET"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(ExecutionError, match="ARB_BINANCE_API_KEY"):
        build_executors(["binance"])
    with pytest.raises(ExecutionError, match="no execution support"):
        build_executors(["coinbase"])


# -- risk gate -------------------------------------------------------------


@pytest.fixture
def risk(tmp_path):
    cfg = trading_cfg(tmp_path)
    store = Store(cfg.db_path)
    yield RiskManager(cfg.trading, store), store, cfg
    store.close()


def test_risk_allows_good_trade(risk):
    rm, _, _ = risk
    assert rm.evaluate(make_opp(), 100).allowed


def test_risk_rejections(risk):
    rm, _, _ = risk
    assert "threshold" in rm.evaluate(make_opp(net_bps=10), 100).reason
    assert "allowlist" in rm.evaluate(make_opp(base="DOGE"), 100).reason
    assert "venue" in rm.evaluate(make_opp(buy="gateio"), 100).reason
    assert "not executable" in rm.evaluate(make_opp(executable=False), 100).reason
    assert "market" in rm.evaluate(make_opp(market="fx"), 100).reason
    assert "too old" in rm.evaluate(make_opp(ts=time.time() - 10), 100).reason
    assert "per-trade cap" in rm.evaluate(make_opp(), 500).reason


def test_risk_cooldown_and_kill_switch(risk, tmp_path, monkeypatch):
    rm, _, cfg = risk
    rm.note_trade("BTC")
    assert "cooldown" in rm.evaluate(make_opp(), 100).reason

    (tmp_path / "KILL").write_text("stop")
    assert "kill switch" in rm.evaluate(make_opp(base="ETH"), 100).reason
    (tmp_path / "KILL").unlink()

    monkeypatch.setenv("ARB_KILL_SWITCH", "1")
    assert rm.kill_switch_active()


def test_risk_daily_caps_from_store(risk):
    rm, store, cfg = risk
    # Burn the daily notional budget with recorded trades.
    for _ in range(10):
        store.record_trade(make_opp(), 0.001, 100, "filled", "test")
    d = rm.evaluate(make_opp(), 100)
    assert not d.allowed and "daily notional" in d.reason

    # Trade count cap.
    cfg.trading.max_daily_notional_usd = 1e9
    for _ in range(11):
        store.record_trade(make_opp(), 0.001, 1, "filled", "test")
    assert "count cap" in rm.evaluate(make_opp(), 1).reason


def test_circuit_breaker(risk):
    rm, _, _ = risk
    for _ in range(3):
        rm.record_result(False)
    assert rm.tripped
    assert "circuit breaker" in rm.evaluate(make_opp(), 100).reason
    rm.record_result(True)
    assert rm.tripped  # stays tripped until restart


# -- signing helpers -------------------------------------------------------


def test_binance_signature_shape():
    ex = BinanceExecution("key", "secret")
    params = ex._signed({"symbol": "BTCUSDT"})
    assert "signature" in params and len(params["signature"]) == 64
    assert params["symbol"] == "BTCUSDT" and "timestamp" in params


def test_kraken_signature_shape():
    import base64
    secret = base64.b64encode(b"super-secret-bytes").decode()
    ex = KrakenExecution("key", secret)
    signed = ex._sign("/0/private/Balance", {})
    assert signed["headers"]["API-Key"] == "key"
    assert base64.b64decode(signed["headers"]["API-Sign"])  # valid b64
    assert "nonce" in signed["data"]


# -- trader flow -----------------------------------------------------------


class StubExecutor(ExecutionVenue):
    def __init__(self, name, balances=None, fail=False, fill_ratio=1.0,
                 market_fail=False, market_fill_ratio=1.0):
        self.name = name
        self._balances = balances or {"USD": 1e6, "USDT": 1e6, "BTC": 10, "ETH": 100}
        self.fail = fail
        self.fill_ratio = fill_ratio
        self.market_fail = market_fail
        self.market_fill_ratio = market_fill_ratio
        self.orders = []
        self.market_orders = []

    async def balances(self, client):
        return self._balances

    async def place_ioc_limit(self, client, base, quote, side, qty, price):
        if self.fail:
            raise ExecutionError(f"{self.name}: rejected")
        self.orders.append((side, base, quote, qty, price))
        return OrderResult(self.name, side, f"{base}{quote}", qty,
                           qty * self.fill_ratio, price, "oid-1", "FILLED")

    async def place_market(self, client, base, quote, side, qty):
        if self.market_fail:
            raise ExecutionError(f"{self.name}: market rejected")
        self.market_orders.append((side, base, quote, qty))
        return OrderResult(self.name, side, f"{base}{quote}", qty,
                           qty * self.market_fill_ratio, 100_000.0, "oid-m", "FILLED")


def make_trader(tmp_path, monkeypatch, buy_venue=None, sell_venue=None,
                spread_bps=80, **cfg_overrides):
    monkeypatch.setenv(ARMING_ENV, ARMING_PHRASE)
    cfg = trading_cfg(tmp_path, min_execute_bps=30, cooldown_seconds=0,
                      **cfg_overrides)
    store = Store(cfg.db_path)
    # Market data: kraken cheap, binance rich -> buy kraken, sell binance.
    poller = Poller(cfg, store, adapters={
        "kraken": StubVenue("kraken", 0),
        "binance": StubVenue("binance", spread_bps),
    })
    executors = {
        "kraken": buy_venue or StubExecutor("kraken"),
        "binance": sell_venue or StubExecutor("binance"),
    }
    trader = Trader(cfg, store, executors=executors, poller=poller)
    return trader, store, executors


def run(coro):
    return asyncio.run(coro)


def test_trader_executes_and_audits(tmp_path, monkeypatch):
    trader, store, executors = make_trader(tmp_path, monkeypatch)

    async def go():
        async with httpx.AsyncClient() as client:
            await trader.run_cycle(client)

    run(go())
    # One buy on kraken, one sell on binance, one audited trade.
    assert len(executors["kraken"].orders) == 1
    assert executors["kraken"].orders[0][0] == "buy"
    assert len(executors["binance"].orders) == 1
    assert executors["binance"].orders[0][0] == "sell"
    trades = store.recent_trades()
    assert len(trades) == 1 and trades[0]["status"] == "filled"
    assert trades[0]["notional_usd"] <= trader.cfg.trading.max_trade_notional_usd
    store.close()


def test_trader_partial_counts_as_failure_and_unwinds(tmp_path, monkeypatch):
    # Sell leg fills only 40% -> we bought more than we sold; the excess
    # base on the buy venue (kraken) must be market-sold back immediately.
    trader, store, executors = make_trader(
        tmp_path, monkeypatch,
        sell_venue=StubExecutor("binance", fill_ratio=0.4),
    )

    async def go():
        async with httpx.AsyncClient() as client:
            await trader.run_cycle(client)

    run(go())
    trades = store.recent_trades()  # newest first
    assert [t["status"] for t in trades] == ["unwound", "partial"]
    assert trader.risk.consecutive_failures == 1
    assert not trader.risk.tripped

    (side, base, quote, uq), = executors["kraken"].market_orders
    assert (side, base) == ("sell", "BTC")
    assert uq == pytest.approx(trades[1]["qty"] * 0.6)
    assert executors["binance"].market_orders == []
    store.close()


def test_trader_unwinds_when_one_leg_errors(tmp_path, monkeypatch):
    # Buy leg rejected, sell leg fully filled -> we are short base on the
    # sell venue (binance); buy the full qty back there at market.
    trader, store, executors = make_trader(
        tmp_path, monkeypatch,
        buy_venue=StubExecutor("kraken", fail=True),
    )

    async def go():
        async with httpx.AsyncClient() as client:
            await trader.run_cycle(client)

    run(go())
    trades = store.recent_trades()
    assert [t["status"] for t in trades] == ["unwound", "partial"]
    (side, base, quote, uq), = executors["binance"].market_orders
    assert (side, base) == ("buy", "BTC")
    assert uq == pytest.approx(trades[1]["qty"])
    assert executors["kraken"].market_orders == []
    store.close()


def test_failed_unwind_trips_breaker(tmp_path, monkeypatch):
    trader, store, executors = make_trader(
        tmp_path, monkeypatch,
        sell_venue=StubExecutor("binance", fill_ratio=0.4),
    )
    executors["kraken"].market_fail = True

    async def go():
        async with httpx.AsyncClient() as client:
            await trader.run_cycle(client)

    run(go())
    trades = store.recent_trades()
    assert [t["status"] for t in trades] == ["unwind_failed", "partial"]
    assert trader.risk.tripped  # naked exposure -> immediate halt
    store.close()


def test_unwind_can_be_disabled(tmp_path, monkeypatch):
    trader, store, executors = make_trader(
        tmp_path, monkeypatch, unwind_partials=False,
        sell_venue=StubExecutor("binance", fill_ratio=0.4),
    )

    async def go():
        async with httpx.AsyncClient() as client:
            await trader.run_cycle(client)

    run(go())
    assert [t["status"] for t in store.recent_trades()] == ["partial"]
    assert executors["kraken"].market_orders == []
    assert executors["binance"].market_orders == []
    store.close()


def test_no_unwind_when_legs_match(tmp_path, monkeypatch):
    # Both legs partially fill by the same ratio: inventory is flat, so a
    # partial is recorded (breaker counts it) but nothing is unwound.
    trader, store, executors = make_trader(
        tmp_path, monkeypatch,
        buy_venue=StubExecutor("kraken", fill_ratio=0.5),
        sell_venue=StubExecutor("binance", fill_ratio=0.5),
    )

    async def go():
        async with httpx.AsyncClient() as client:
            await trader.run_cycle(client)

    run(go())
    assert [t["status"] for t in store.recent_trades()] == ["partial"]
    assert executors["kraken"].market_orders == []
    assert executors["binance"].market_orders == []
    assert trader.risk.consecutive_failures == 1
    store.close()


def test_trader_circuit_breaker_stops_trading(tmp_path, monkeypatch):
    trader, store, executors = make_trader(
        tmp_path, monkeypatch,
        buy_venue=StubExecutor("kraken", fail=True),
    )

    async def go():
        async with httpx.AsyncClient() as client:
            for _ in range(6):
                await trader.run_cycle(client)

    run(go())
    assert trader.risk.tripped
    # Exactly max_consecutive_failures attempts (each: one partial + its
    # unwind of the filled sell leg), then no more.
    trades = store.recent_trades()
    n = trader.cfg.trading.max_consecutive_failures
    assert len(trades) == 2 * n
    assert sum(t["status"] == "partial" for t in trades) == n
    assert sum(t["status"] == "unwound" for t in trades) == n
    store.close()


def test_trader_skips_when_balance_insufficient(tmp_path, monkeypatch):
    trader, store, executors = make_trader(
        tmp_path, monkeypatch,
        sell_venue=StubExecutor("binance", balances={"USDT": 1e6, "BTC": 0.0}),
    )

    async def go():
        async with httpx.AsyncClient() as client:
            await trader.run_cycle(client)

    run(go())
    assert store.recent_trades() == []  # nothing fired, nothing audited
    assert executors["kraken"].orders == []
    store.close()


def test_trader_refuses_without_arming(tmp_path, monkeypatch):
    monkeypatch.delenv(ARMING_ENV, raising=False)
    cfg = trading_cfg(tmp_path)
    store = Store(cfg.db_path)
    with pytest.raises(NotArmedError):
        Trader(cfg, store, executors={}, poller=Poller(cfg, store, adapters={}))
    store.close()
