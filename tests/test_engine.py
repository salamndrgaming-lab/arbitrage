import math

from arb.config import Config
from arb.engine import best_spreads, evaluate_pair, find_opportunities
from arb.models import Quote


CFG = Config()
GROUP = CFG.quote_group


def q(exchange, base, quote, bid, ask):
    return Quote(exchange=exchange, base=base, quote=quote, bid=bid, ask=ask, ts=1.0)


def test_net_spread_math():
    buy = q("kucoin", "BTC", "USDT", 99_990, 100_000)
    sell = q("kraken", "BTC", "USD", 100_500, 100_510)
    fees = {"kucoin": 10, "kraken": 26}  # bps
    opp = evaluate_pair(buy, sell, fees, haircut_bps=5)

    cost = 100_000 * 1.0010
    proceeds = 100_500 * (1 - 0.0026) * (1 - 0.0005)
    expected_net = (proceeds / cost - 1) * 10_000
    assert math.isclose(opp.net_bps, expected_net, rel_tol=1e-9)
    assert math.isclose(opp.gross_bps, (100_500 / 100_000 - 1) * 10_000, rel_tol=1e-9)
    assert opp.cross_quote is True


def test_fees_can_kill_a_gross_spread():
    buy = q("a", "ETH", "USD", 1999, 2000)
    sell = q("b", "ETH", "USD", 2003, 2004)  # ~15 bps gross
    opp = evaluate_pair(buy, sell, {"a": 10, "b": 10}, haircut_bps=5)
    assert opp.gross_bps > 0
    assert opp.net_bps < 0  # 25 bps of costs eat a 15 bps spread


def test_find_opportunities_filters_and_sorts():
    quotes = [
        q("a", "BTC", "USD", 100_000, 100_010),
        q("b", "BTC", "USDT", 100_900, 100_910),   # ~89 bps above a's ask
        q("c", "BTC", "USD", 100_300, 100_310),    # ~29 bps above a's ask
        q("a", "ETH", "USD", 2000, 2001),
        q("b", "ETH", "USDT", 2000, 2001),         # flat, no opportunity
    ]
    fees = {"a": 10, "b": 10, "c": 10}
    opps = find_opportunities(quotes, fees, min_net_bps=10, haircut_bps=0, quote_group=GROUP)

    assert [o.base for o in opps] == ["BTC", "BTC"]
    assert opps[0].net_bps >= opps[1].net_bps
    assert opps[0].buy_exchange == "a" and opps[0].sell_exchange == "b"
    assert all(o.net_bps >= 10 for o in opps)


def test_quote_groups_separate_non_equivalent_quotes():
    # EUR is not in the USD equivalence group: no cross-comparison.
    quotes = [
        q("a", "BTC", "USD", 100_000, 100_010),
        q("b", "BTC", "EUR", 92_000, 92_010),
    ]
    opps = find_opportunities(quotes, {"a": 0, "b": 0}, 0, 0, GROUP)
    assert opps == []


def test_broken_ticks_are_dropped():
    quotes = [
        q("a", "BTC", "USD", 100_000, 100_010),
        q("b", "BTC", "USD", 0, 100_010),           # zero bid
        q("c", "BTC", "USD", 200_000, 100_010),     # crossed beyond sanity
    ]
    best = best_spreads(quotes, {}, 0, GROUP)
    assert best == {}  # only one valid quote left -> no pair


def test_best_spreads_reports_negative_bests():
    quotes = [
        q("a", "BTC", "USD", 100_000, 100_010),
        q("b", "BTC", "USD", 100_001, 100_011),
    ]
    best = best_spreads(quotes, {"a": 10, "b": 10}, 5, GROUP)
    assert "BTC" in best
    assert best["BTC"].net_bps < 0


def test_same_exchange_never_pairs():
    quotes = [
        q("a", "BTC", "USD", 100_000, 100_010),
        q("a", "BTC", "USDT", 101_000, 101_010),
    ]
    opps = find_opportunities(quotes, {"a": 0}, 0, 0, GROUP)
    assert opps == []
