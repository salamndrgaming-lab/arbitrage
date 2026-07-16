"""Arbitrage detection: compare quotes for the same asset across exchanges.

The engine is pure — it takes quotes plus fee/haircut parameters and returns
opportunities. All costs are expressed in basis points (1 bps = 0.01%).

Net spread for buying 1 unit at ``ask`` on venue A and selling it at ``bid``
on venue B:

    cost     = ask * (1 + taker_A)
    proceeds = bid * (1 - taker_B) * (1 - haircut)
    net_bps  = (proceeds / cost - 1) * 10_000

The haircut is a flat allowance for moving the asset between venues
(withdrawal fee, slippage while in transit). It is deliberately crude —
real transfer costs vary by asset and network — but keeps paper spreads
honest enough to rank.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Callable, Iterable

from .models import Opportunity, Quote


def _group_quotes(
    quotes: Iterable[Quote], quote_group: Callable[[str], str]
) -> dict[tuple[str, str], list[Quote]]:
    """Group quotes by (base asset, quote-currency equivalence group)."""
    groups: dict[tuple[str, str], list[Quote]] = defaultdict(list)
    for q in quotes:
        if q.bid <= 0 or q.ask <= 0 or q.bid > q.ask * 1.5:
            continue  # drop obviously broken ticks
        groups[(q.base, quote_group(q.quote))].append(q)
    return groups


def evaluate_pair(
    buy: Quote,
    sell: Quote,
    fees_bps: dict[str, float],
    haircut_bps: float,
) -> Opportunity:
    """Compute the opportunity for buying on ``buy`` and selling on ``sell``."""
    fee_buy = fees_bps.get(buy.exchange, 0.0) / 10_000
    fee_sell = fees_bps.get(sell.exchange, 0.0) / 10_000
    haircut = haircut_bps / 10_000

    cost = buy.ask * (1 + fee_buy)
    proceeds = sell.bid * (1 - fee_sell) * (1 - haircut)
    net_bps = (proceeds / cost - 1) * 10_000
    gross_bps = (sell.bid / buy.ask - 1) * 10_000

    return Opportunity(
        base=buy.base,
        buy_exchange=buy.exchange,
        buy_quote=buy.quote,
        buy_price=buy.ask,
        sell_exchange=sell.exchange,
        sell_quote=sell.quote,
        sell_price=sell.bid,
        gross_bps=gross_bps,
        net_bps=net_bps,
        cross_quote=buy.quote != sell.quote,
        ts=max(buy.ts, sell.ts),
    )


def find_opportunities(
    quotes: Iterable[Quote],
    fees_bps: dict[str, float],
    min_net_bps: float,
    haircut_bps: float,
    quote_group: Callable[[str], str],
) -> list[Opportunity]:
    """All cross-exchange pairs whose net spread clears ``min_net_bps``.

    Sorted by net spread, best first.
    """
    found: list[Opportunity] = []
    for group in _group_quotes(quotes, quote_group).values():
        for buy in group:
            for sell in group:
                if buy.exchange == sell.exchange:
                    continue
                opp = evaluate_pair(buy, sell, fees_bps, haircut_bps)
                if opp.net_bps >= min_net_bps:
                    found.append(opp)
    found.sort(key=lambda o: o.net_bps, reverse=True)
    return found


def best_spreads(
    quotes: Iterable[Quote],
    fees_bps: dict[str, float],
    haircut_bps: float,
    quote_group: Callable[[str], str],
) -> dict[str, Opportunity]:
    """Best (possibly negative) pair per base asset — used for spread history.

    Negative best-spreads matter: they show how far the market is from an
    opportunity, which is what the dashboard's history chart plots.
    """
    best: dict[str, Opportunity] = {}
    for (base, _), group in _group_quotes(quotes, quote_group).items():
        for buy in group:
            for sell in group:
                if buy.exchange == sell.exchange:
                    continue
                opp = evaluate_pair(buy, sell, fees_bps, haircut_bps)
                if base not in best or opp.net_bps > best[base].net_bps:
                    best[base] = opp
    return best
