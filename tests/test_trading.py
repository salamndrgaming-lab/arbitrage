"""Trading layer tests: arming, risk gate, execution flow, circuit breaker.

Stub executors stand in for the authenticated venue APIs — tests must never
place real orders. Signing helpers are tested for determinism/shape only.
"""

import asyncio
import base64
import time

import httpx
import pytest

from arb.config import Config, TradingConfig
from arb.models import Opportunity, Quote
from arb.poller import Poller
from arb.store import Store
from arb.trading import ARMING_ENV, ARMING_PHRASE
from arb.trading.books import (
    BinanceBookFeed,
    KrakenBookFeed,
    LiveBooks,
    TopOfBook,
    build_feeds,
)
from arb.trading.execution import (
    BinanceExecution,
    ExecutionError,
    ExecutionVenue,
    KrakenExecution,
    OrderResult,
    PairRules,
    build_executors,
    ceil_to_step,
    floor_to_step,
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


# -- precision rules -------------------------------------------------------


def test_step_rounding_helpers():
    assert floor_to_step(0.123456, 0.001) == pytest.approx(0.123)
    assert ceil_to_step(100.001, 0.01) == pytest.approx(100.01)
    # Exact multiples stay put in both directions.
    assert floor_to_step(0.123, 0.001) == pytest.approx(0.123)
    assert ceil_to_step(0.123, 0.001) == pytest.approx(0.123)
    # Zero step means unconstrained.
    assert floor_to_step(0.123456, 0) == 0.123456
    assert ceil_to_step(0.123456, 0) == 0.123456


def mock_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_binance_pair_rules_parse_and_cache():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={"symbols": [{"filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.00001",
             "minQty": "0.0001"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            {"filterType": "NOTIONAL", "minNotional": "5"},
        ]}]})

    async def go():
        ex = BinanceExecution("k", "s")
        async with mock_client(handler) as client:
            rules = await ex.pair_rules(client, "BTC", "USDT")
            await ex.pair_rules(client, "BTC", "USDT")  # cached
        return rules

    rules = run(go())
    assert rules == PairRules(qty_step=0.00001, price_tick=0.01,
                              min_qty=0.0001, min_notional=5.0)
    assert len(calls) == 1


def test_kraken_pair_rules_parse():
    def handler(request):
        assert request.url.params["pair"] == "XBTUSD"
        return httpx.Response(200, json={"error": [], "result": {"XXBTZUSD": {
            "lot_decimals": 8, "pair_decimals": 1,
            "ordermin": "0.0001", "costmin": "0.5",
        }}})

    async def go():
        ex = KrakenExecution("k", base64.b64encode(b"s").decode())
        async with mock_client(handler) as client:
            return await ex.pair_rules(client, "BTC", "USD")

    rules = run(go())
    assert rules.qty_step == pytest.approx(1e-8)
    assert rules.price_tick == pytest.approx(0.1)
    assert rules.min_qty == pytest.approx(0.0001)
    assert rules.min_notional == pytest.approx(0.5)


def test_pair_rules_unknown_symbol_raises():
    def handler(request):
        return httpx.Response(200, json={"symbols": []})

    async def go():
        ex = BinanceExecution("k", "s")
        async with mock_client(handler) as client:
            await ex.pair_rules(client, "NOPE", "USDT")

    with pytest.raises(ExecutionError, match="unknown symbol"):
        run(go())


# -- live book feeds -------------------------------------------------------


def test_binance_book_feed_parses_bookticker():
    books = LiveBooks()
    feed = BinanceBookFeed(books, ["BTC"])
    assert "btcusdt@bookTicker" in feed.url()
    feed.handle({"stream": "btcusdt@bookTicker", "data": {
        "s": "BTCUSDT", "b": "100000.1", "B": "0.5", "a": "100000.9", "A": "0.7",
    }})
    top = books.top("binance", "BTC", "USDT")
    assert (top.bid, top.bid_qty, top.ask, top.ask_qty) == (
        100000.1, 0.5, 100000.9, 0.7)
    # Unknown symbols are ignored.
    feed.handle({"data": {"s": "DOGEUSDT", "b": "1", "B": "1", "a": "1", "A": "1"}})
    assert books.top("binance", "DOGE", "USDT") is None


def test_kraken_book_feed_parses_ticker():
    books = LiveBooks()
    feed = KrakenBookFeed(books, ["BTC", "ETH"])
    sub = feed.subscribe_payload()
    assert "BTC/USD" in sub and "ETH/USD" in sub
    feed.handle({"channel": "ticker", "type": "update", "data": [{
        "symbol": "BTC/USD", "bid": 99990.0, "bid_qty": 1.2,
        "ask": 100010.0, "ask_qty": 0.8,
    }]})
    top = books.top("kraken", "BTC", "USD")
    assert (top.bid, top.ask, top.ask_qty) == (99990.0, 100010.0, 0.8)
    # Non-ticker channels are ignored.
    feed.handle({"channel": "heartbeat"})


def test_build_feeds_covers_supported_venues():
    books = LiveBooks()
    feeds = build_feeds(books, ["kraken", "binance", "bitstamp"], ["BTC"])
    assert sorted(f.venue for f in feeds) == ["binance", "kraken"]


def test_live_books_freshness():
    books = LiveBooks()
    now = time.time()
    books.update("binance", "BTC", "USDT",
                 TopOfBook(1, 1, 2, 1, ts=now))
    books.update("kraken", "BTC", "USD",
                 TopOfBook(1, 1, 2, 1, ts=now - 60))
    assert books.fresh_count(max_age=3, now=now) == 1


def fresh_books(buy_ask=100010.0, buy_ask_qty=5.0,
                sell_bid=100900.0, sell_bid_qty=5.0, age=0.0):
    """Books for the make_trader scenario: buy kraken/USD, sell binance/USD."""
    books = LiveBooks()
    ts = time.time() - age
    books.update("kraken", "BTC", "USD",
                 TopOfBook(buy_ask - 5, 1.0, buy_ask, buy_ask_qty, ts))
    books.update("binance", "BTC", "USD",
                 TopOfBook(sell_bid, sell_bid_qty, sell_bid + 5, 1.0, ts))
    return books


def test_trader_caps_size_at_displayed_depth(tmp_path, monkeypatch):
    trader, store, executors = make_trader(
        tmp_path, monkeypatch,
        books=fresh_books(buy_ask_qty=0.0004, sell_bid_qty=5.0),
    )

    async def go():
        async with httpx.AsyncClient() as client:
            await trader.run_cycle(client)

    run(go())
    (side, base, quote, qty, price), = executors["kraken"].orders
    assert qty == pytest.approx(0.0004)  # displayed size, not the $100 cap
    assert price == pytest.approx(100010.0)  # live ask, not the REST quote
    (_, _, _, sqty, sprice), = executors["binance"].orders
    assert sqty == pytest.approx(0.0004)
    assert sprice == pytest.approx(100900.0)  # live bid
    assert store.recent_trades()[0]["status"] == "filled"
    store.close()


def test_trader_skips_when_live_spread_collapses(tmp_path, monkeypatch):
    # REST quotes still show 80 bps, but the live book has converged.
    # (BTC only: an asset with no book would fall back to REST and trade.)
    trader, store, executors = make_trader(
        tmp_path, monkeypatch, books=fresh_books(sell_bid=100020.0),
        assets=["BTC"],
    )

    async def go():
        async with httpx.AsyncClient() as client:
            await trader.run_cycle(client)

    run(go())
    assert executors["kraken"].orders == []
    assert executors["binance"].orders == []
    assert store.recent_trades() == []
    store.close()


def test_trader_falls_back_to_rest_when_books_stale(tmp_path, monkeypatch):
    # Books exist but are a minute old -> ignore them, trade on REST quotes.
    trader, store, executors = make_trader(
        tmp_path, monkeypatch, books=fresh_books(buy_ask=90000.0, age=60),
    )

    async def go():
        async with httpx.AsyncClient() as client:
            await trader.run_cycle(client)

    run(go())
    (_, _, _, qty, price), = executors["kraken"].orders
    assert price == pytest.approx(100010.0)  # REST ask, not the stale 90000
    assert store.recent_trades()[0]["status"] == "filled"
    store.close()


# -- trader flow -----------------------------------------------------------


class StubExecutor(ExecutionVenue):
    def __init__(self, name, balances=None, fail=False, fill_ratio=1.0,
                 market_fail=False, market_fill_ratio=1.0, rules=None):
        self.name = name
        self._balances = balances or {"USD": 1e6, "USDT": 1e6, "BTC": 10, "ETH": 100}
        self.fail = fail
        self.fill_ratio = fill_ratio
        self.market_fail = market_fail
        self.market_fill_ratio = market_fill_ratio
        self.rules = rules or PairRules()  # unconstrained by default
        self.orders = []
        self.market_orders = []

    async def pair_rules(self, client, base, quote):
        return self.rules

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
                spread_bps=80, books=None, **cfg_overrides):
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
    trader = Trader(cfg, store, executors=executors, poller=poller, books=books)
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


def test_trader_quantizes_to_venue_rules(tmp_path, monkeypatch):
    # Coarser lot step wins (both legs must carry equal qty); buy price
    # floors to its tick, sell price ceils to its tick.
    trader, store, executors = make_trader(
        tmp_path, monkeypatch,
        buy_venue=StubExecutor("kraken", rules=PairRules(
            qty_step=0.0001, price_tick=0.1, min_qty=0.0001, min_notional=0.5)),
        sell_venue=StubExecutor("binance", rules=PairRules(
            qty_step=0.00001, price_tick=0.01, min_qty=0.00001, min_notional=5)),
    )

    async def go():
        async with httpx.AsyncClient() as client:
            await trader.run_cycle(client)

    run(go())
    (side, base, quote, qty, price), = executors["kraken"].orders
    assert qty == pytest.approx(floor_to_step(qty, 0.0001))
    assert price == pytest.approx(floor_to_step(price, 0.1))
    (_, _, _, sqty, sprice), = executors["binance"].orders
    assert sqty == pytest.approx(qty)  # both legs share the quantized qty
    assert sprice == pytest.approx(ceil_to_step(sprice, 0.01))
    trades = store.recent_trades()
    assert trades[0]["status"] == "filled"
    assert trades[0]["qty"] == pytest.approx(qty)
    store.close()


def test_trader_skips_below_venue_minimum(tmp_path, monkeypatch):
    # Venue minimum above what the per-trade cap can buy -> no order fires.
    trader, store, executors = make_trader(
        tmp_path, monkeypatch,
        buy_venue=StubExecutor("kraken", rules=PairRules(min_qty=1.0)),
    )

    async def go():
        async with httpx.AsyncClient() as client:
            await trader.run_cycle(client)

    run(go())
    assert executors["kraken"].orders == []
    assert executors["binance"].orders == []
    assert store.recent_trades() == []
    store.close()


def test_trader_skips_when_rules_unavailable(tmp_path, monkeypatch):
    # Fail closed: if precision rules can't be fetched, don't guess.
    class NoRules(StubExecutor):
        async def pair_rules(self, client, base, quote):
            raise ExecutionError("exchangeInfo down")

    trader, store, executors = make_trader(
        tmp_path, monkeypatch, buy_venue=NoRules("kraken"),
    )

    async def go():
        async with httpx.AsyncClient() as client:
            await trader.run_cycle(client)

    run(go())
    assert executors["binance"].orders == []
    assert store.recent_trades() == []
    store.close()


def test_unwind_residual_below_minimum_is_dust(tmp_path, monkeypatch):
    # Sell leg misses 40%; the buy-venue minimum is larger than the
    # residual -> recorded as dust, no market order, breaker NOT tripped.
    # Cap of $2000 buys qty 0.02, clearing the 0.01 entry minimum; the
    # 40% residual (0.008) does not.
    trader, store, executors = make_trader(
        tmp_path, monkeypatch,
        buy_venue=StubExecutor("kraken", rules=PairRules(min_qty=0.01)),
        sell_venue=StubExecutor("binance", fill_ratio=0.6),
        max_trade_notional_usd=2000, max_daily_notional_usd=10_000,
    )

    async def go():
        async with httpx.AsyncClient() as client:
            await trader.run_cycle(client)

    run(go())
    trades = store.recent_trades()
    assert [t["status"] for t in trades] == ["unwind_dust", "partial"]
    assert executors["kraken"].market_orders == []
    assert not trader.risk.tripped
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
