# Market Arbitrage Tracker

Tracks crypto prices across **8 international exchanges** (US, EU, Asia, global)
and surfaces **cross-exchange arbitrage opportunities** — buy at the ask on one
venue, sell at the bid on another — netting out taker fees and a transfer
allowance. Ships as a live web dashboard, a JSON API, and a terminal scanner.

All market data comes from **public, keyless REST endpoints**: no accounts or
API keys needed.

| Exchange | Region | Quote | Default taker fee |
|---|---|---|---|
| Binance | Global | USDT | 10 bps |
| Kraken | US | USD | 26 bps |
| Coinbase | US | USD | 60 bps |
| Bitstamp | EU | USD | 40 bps |
| Bitfinex | BVI | USD | 20 bps |
| KuCoin | Seychelles | USDT | 10 bps |
| OKX | Asia | USDT | 10 bps |
| Gate.io | Asia | USDT | 20 bps |

Tracked assets (configurable): BTC, ETH, SOL, XRP, LTC, DOGE, ADA.

## Quick start

```bash
pip install -r requirements.txt

# Web dashboard + API at http://127.0.0.1:8000
uvicorn arb.server:app

# No network / just exploring? Run against the simulated feed:
ARB_MODE=demo uvicorn arb.server:app

# Terminal scanner (one-shot or continuous)
python -m arb.cli --demo
python -m arb.cli --watch
```

## Dashboard

- **Stat tiles** — best net spread right now, opportunities above threshold,
  exchange health.
- **Live opportunities table** — buy/sell venue and price, gross vs net spread
  (bps), estimated profit per $10k notional.
- **Spread history chart** — best gross and net spread over time per asset,
  with crosshair tooltip (1–72 h windows).
- **Price matrix** — every venue's mid price per asset; cheapest ask and best
  bid highlighted.

## How spreads are computed

For a candidate round trip (buy 1 unit at `ask` on venue A, sell at `bid` on
venue B):

```
cost     = ask × (1 + taker_A)
proceeds = bid × (1 − taker_B) × (1 − transfer_haircut)
net_bps  = (proceeds / cost − 1) × 10 000
```

- Quotes are only compared within a **quote-equivalence group** (default:
  USD ≈ USDT ≈ USDC). Opportunities whose legs use different quote currencies
  are flagged (`†`) since they carry stablecoin basis risk.
- The **transfer haircut** (default 5 bps) is a flat allowance for withdrawal
  fees / transfer slippage; tune it per your reality.
- Obviously broken ticks (zero or wildly crossed prices) are dropped.

## Configuration

Everything lives in [`config.yaml`](config.yaml) (optional — sane defaults are
built in): poll interval, reporting threshold, assets, per-exchange enable
flags and fee tiers, DB path, history retention. Environment overrides:
`ARB_MODE=demo|live`, `ARB_DB=path.sqlite3`, `ARB_CONFIG=path.yaml`.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/status` | mode, poll stats, per-exchange health |
| `GET /api/quotes?asset=BTC` | latest normalized bid/ask per venue |
| `GET /api/opportunities?min_net_bps=10` | current opportunities + best spread per asset |
| `GET /api/history?asset=BTC&hours=6` | best-spread time series (for charts) |
| `GET /api/opportunities/recent?hours=24` | persisted opportunity log |

History and the opportunity log persist in SQLite (`arb.sqlite3`), pruned to
`history_retention_hours`.

## Tests

```bash
pytest
```

## Extending

- **New exchange**: subclass `Adapter` in `arb/exchanges.py` (one `fetch`
  method), register it in `ADAPTERS`, add a config entry with its fee.
- **Other market types** (FX, equities, prediction markets): the engine only
  sees `Quote` objects — any feed that produces them plugs in.

## Caveats

Paper spreads ≠ executable profit: real arbitrage adds slippage, order-book
depth limits, transfer latency (price moves while coins are in flight),
per-asset withdrawal fees, and USD↔USDT basis risk. Some venues geo-block API
access by IP (e.g. Binance from US addresses) — those feeds simply show as
degraded. **This tool observes and records; it does not place orders.**
