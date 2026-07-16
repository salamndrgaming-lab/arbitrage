"""Adapter parsing tests against canned API payloads (no network)."""

import asyncio

import pytest

from arb.exchanges import (
    ADAPTERS,
    Binance,
    CurrencyAPI,
    ExchangeError,
    Frankfurter,
    Kraken,
    OpenERAPI,
    Stooq,
    build_adapters,
)


def fetch(adapter, assets, payloads):
    """Run adapter.fetch with _get_json returning queued payloads."""
    queue = list(payloads)

    async def fake_get_json(client, url, **kwargs):
        return queue.pop(0)

    adapter._get_json = fake_get_json
    return asyncio.run(adapter.fetch(None, assets))


def test_binance_filters_all_markets():
    payload = [
        {"symbol": "BTCUSDT", "bidPrice": "95000.1", "askPrice": "95000.9"},
        {"symbol": "EURUSDT", "bidPrice": "1.0800", "askPrice": "1.0802"},
        {"symbol": "PAXGUSDT", "bidPrice": "2650.0", "askPrice": "2651.0"},
        {"symbol": "SHIBUSDT", "bidPrice": "0.1", "askPrice": "0.2"},  # not tracked
    ]
    quotes = fetch(Binance(), {"crypto": ["BTC"], "fx": ["EUR"], "metals": ["XAU", "XAG"]},
                   [payload])
    by_base = {q.base: q for q in quotes}
    assert set(by_base) == {"BTC", "EUR", "XAU"}  # XAG has no token, SHIB not tracked
    assert by_base["EUR"].market == "fx"
    assert by_base["XAU"].market == "metals"
    assert by_base["XAU"].bid == 2650.0


def test_kraken_maps_padded_keys_and_fiat_pairs():
    crypto_payload = {"error": [], "result": {
        "XXBTZUSD": {"b": ["95000.1", "1"], "a": ["95001.2", "1"]},
    }}
    fx_payload = {"error": [], "result": {
        "ZEURZUSD": {"b": ["1.0799", "1"], "a": ["1.0801", "1"]},
        "AUDUSD": {"b": ["0.6700", "1"], "a": ["0.6702", "1"]},
    }}
    quotes = fetch(Kraken(), {"crypto": ["BTC"], "fx": ["EUR", "AUD"]},
                   [crypto_payload, fx_payload])
    by_base = {q.base: q for q in quotes}
    assert by_base["BTC"].bid == 95000.1
    assert by_base["EUR"].market == "fx"
    assert by_base["AUD"].ask == 0.6702


def test_kraken_partial_failure_keeps_other_market():
    crypto_payload = {"error": [], "result": {
        "XXBTZUSD": {"b": ["95000.1", "1"], "a": ["95001.2", "1"]},
    }}
    fx_payload = {"error": ["EQuery:Unknown asset pair"], "result": {}}
    quotes = fetch(Kraken(), {"crypto": ["BTC"], "fx": ["EUR"]},
                   [crypto_payload, fx_payload])
    assert [q.base for q in quotes] == ["BTC"]


def test_usd_rate_feeds_invert_rates():
    frank = fetch(Frankfurter(), {"fx": ["EUR", "GBP"]},
                  [{"base": "USD", "rates": {"EUR": 0.9259, "GBP": 0.7874}}])
    eur = next(q for q in frank if q.base == "EUR")
    assert eur.bid == eur.ask == pytest.approx(1 / 0.9259)
    assert eur.market == "fx" and eur.quote == "USD"

    er = fetch(OpenERAPI(), {"fx": ["EUR"]},
               [{"result": "success", "rates": {"EUR": 0.9259}}])
    assert er[0].mid == pytest.approx(1 / 0.9259)

    cur = fetch(CurrencyAPI(), {"fx": ["EUR"], "metals": ["XAU", "XAG"]},
                [{"usd": {"eur": 0.9259, "xau": 0.000305, "xag": 0.0265}}])
    by_base = {q.base: q for q in cur}
    assert by_base["XAU"].mid == pytest.approx(1 / 0.000305)
    assert by_base["XAU"].market == "metals"


def test_openerapi_error_response_raises():
    with pytest.raises(ExchangeError):
        fetch(OpenERAPI(), {"fx": ["EUR"]},
              [{"result": "error", "error-type": "rate-limited"}])


def test_stooq_csv_parse():
    csv = (
        "Symbol,Date,Time,Open,High,Low,Close,Volume\n"
        "EURUSD,2026-07-16,15:55:33,1.0790,1.0820,1.0785,1.0812,0\n"
        "XAUUSD,2026-07-16,15:55:30,2648,2662,2645,2655.5,0\n"
        "XAGUSD,N/D,N/D,N/D,N/D,N/D,N/D,N/D\n"
    )
    hit = {"eurusd": ("EUR", "fx"), "xauusd": ("XAU", "metals"), "xagusd": ("XAG", "metals")}
    quotes = Stooq().parse_csv(csv, hit)
    by_base = {q.base: q for q in quotes}
    assert set(by_base) == {"EUR", "XAU"}  # N/D row skipped
    assert by_base["XAU"].mid == 2655.5
    assert by_base["EUR"].market == "fx"

    with pytest.raises(ExchangeError):
        Stooq().parse_csv("Symbol,Date\n", hit)


def test_reference_feeds_are_not_tradable():
    for name in ("frankfurter", "openerapi", "currencyapi", "stooq"):
        assert ADAPTERS[name].tradable is False
    for name in ("binance", "kraken", "bitstamp"):
        assert ADAPTERS[name].tradable is True


def test_build_adapters_rejects_unknown():
    with pytest.raises(ValueError):
        build_adapters(["binance", "nope"])
