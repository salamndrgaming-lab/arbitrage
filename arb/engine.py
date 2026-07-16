"""Arbitrage detection: compare quotes for the same asset across venues.

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

Quotes are grouped by (market, base asset, quote-currency group), so an
asset never pairs across markets. Reference-rate feeds (``tradable`` set
excludes them) contribute to spread tracking but never form opportunities.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Callable, Iterable

from .models import Opportunity, Quote


def _group_quotes(
    quotes: Iterable[Quote], quote_group: Callable[[str], str]
) -> dict[tuple[str, str, str], list[Quote]]:
    """Group quotes by (market, base asset, quote-currency equivalence group)."""
    groups: dict[tuple[str, str, str], list[Quote]] = defaultdict(list)
    for q in quotes:
        if q.bid <= 0 or q.ask <= 0 or q.bid > q.ask * 1.5:
            continue  # drop obviously broken ticks
        groups[(q.market, q.base, quote_group(q.quote))].append(q)
    return groups


def evaluate_pair(
    buy: Quote,
    sell: Quote,
    fees_bps: dict[str, float],
    haircut_bps: float,
    tradable: set[str] | None = None,
) -> Opportunity:
    """Compute the opportunity for buying on ``buy`` and selling on ``sell``."""
    fee_buy = fees_bps.get(buy.exchange, 0.0) / 10_000
    fee_sell = fees_bps.get(sell.exchange, 0.0) / 10_000
    haircut = haircut_bps / 10_000

    cost = buy.ask * (1 + fee_buy)
    proceeds = sell.bid * (1 - fee_sell) * (1 - haircut)
    net_bps = (proceeds / cost - 1) * 10_000
    gross_bps = (sell.bid / buy.ask - 1) * 10_000

    executable = tradable is None or (
        buy.exchange in tradable and sell.exchange in tradable
    )
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
        market=buy.market,
        executable=executable,
    )


def find_opportunities(
    quotes: Iterable[Quote],
    fees_bps: dict[str, float],
    min_net_bps: float,
    haircut_bps: float,
    quote_group: Callable[[str], str],
    tradable: set[str] | None = None,
) -> list[Opportunity]:
    """Executable cross-venue pairs whose net spread clears ``min_net_bps``.

    When ``tradable`` is given, both legs must be tradable venues — a spread
    against a reference-rate feed is a data point, not an opportunity.
    Sorted by net spread, best first.
    """
    found: list[Opportunity] = []
    for group in _group_quotes(quotes, quote_group).values():
        for buy in group:
            if tradable is not None and buy.exchange not in tradable:
                continue
            for sell in group:
                if buy.exchange == sell.exchange:
                    continue
                if tradable is not None and sell.exchange not in tradable:
                    continue
                opp = evaluate_pair(buy, sell, fees_bps, haircut_bps, tradable)
                if opp.net_bps >= min_net_bps:
                    found.append(opp)
    found.sort(key=lambda o: o.net_bps, reverse=True)
    return found


def best_spreads(
    quotes: Iterable[Quote],
    fees_bps: dict[str, float],
    haircut_bps: float,
    quote_group: Callable[[str], str],
    tradable: set[str] | None = None,
) -> dict[str, Opportunity]:
    """Best (possibly negative) pair per base asset — used for spread history.

    Considers all venues including reference feeds (divergence against a
    reference rate is worth charting); ``executable`` on the result says
    whether both legs are tradable. Negative best-spreads matter: they show
    how far the market is from an opportunity.
    """
    best: dict[str, Opportunity] = {}
    for (_, base, _), group in _group_quotes(quotes, quote_group).items():
        for buy in group:
            for sell in group:
                if buy.exchange == sell.exchange:
                    continue
                opp = evaluate_pair(buy, sell, fees_bps, haircut_bps, tradable)
                if base not in best or opp.net_bps > best[base].net_bps:
                    best[base] = opp
    return best
