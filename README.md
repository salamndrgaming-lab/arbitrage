# Market Arbitrage Tracker

Tracks prices across international venues in **three markets — crypto, FX and
precious metals** — and surfaces **cross-venue arbitrage opportunities**: buy
at the ask on one venue, sell at the bid on another, netting out taker fees
and a transfer allowance. Ships as a live web dashboard, a JSON API, and a
terminal scanner. Live data only; there is no simulated mode.

All market data comes from **public, keyless endpoints**: no accounts or API
keys needed.

## Venues

**Tradable exchanges** (order books; can appear as arbitrage legs):

| Venue | Region | Markets | Default taker fee |
|---|---|---|---|
| Binance | Global | crypto, FX (EUR/USDT), gold (PAXG) | 10 bps |
| Kraken | US | crypto, fiat FX (EUR, GBP, AUD /USD) | 26 bps |
| Coinbase | US | crypto | 60 bps |
| Bitstamp | EU | crypto, fiat FX (EUR, GBP /USD) | 40 bps |
| Bitfinex | BVI | crypto | 20 bps |
| KuCoin | Seychelles | crypto, gold (PAXG) | 10 bps |
| OKX | Asia | crypto | 10 bps |
| Gate.io | Asia | crypto, gold (PAXG) | 20 bps |

**Reference-rate feeds** (single published rate; used for price tracking and
divergence, **never** counted as an arbitrage leg):

| Feed | What it publishes |
|---|---|
| Frankfurter | ECB reference FX rates |
| open.er-api.com | aggregated FX rates |
| currency-api | FX plus XAU/XAG spot references |
| Stooq | delayed intraday FX and spot metals |

Tracked assets (configurable): BTC, ETH, SOL, XRP, LTC, DOGE, ADA · EUR, GBP,
AUD (vs USD) · XAU (gold), XAG (silver). On crypto venues gold trades as
**PAXG** (a 1 oz gold token), mapped to XAU — so tokenized gold on Binance can
be compared against spot references and against PAXG on KuCoin/Gate.io.

## Quick start

```bash
pip install -r requirements.txt

# Web dashboard + API at http://127.0.0.1:8000
uvicorn arb.server:app

# Terminal scanner (one-shot or continuous)
python -m arb.cli
python -m arb.cli --watch --market fx
```

## Deploying to Vercel

The repo is Vercel-ready: `api/index.py` exposes a serverless variant of the
app (`arb/webapp.py`) and `vercel.json` routes everything to it. Deploy with
`vercel deploy` (or connect the repo in the Vercel dashboard).

Serverless differences vs self-hosting:

- **No background poller** — each `/api/scan` request fetches venues live,
  with a small in-instance gap (`ARB_MIN_SCAN_GAP`, default 3 s) so warm
  instances don't hammer venue APIs, and a gentler dashboard refresh
  cadence (`ARB_SCAN_INTERVAL`, default 15 s).
- **No database** — spread history accumulates in the browser
  (session-scoped) instead of SQLite; the opportunity log endpoint isn't
  available.
- The function region is pinned to `fra1` (Frankfurt) in `vercel.json`
  because several exchanges geo-block US IPs, which is where default
  regions land.

## Dashboard

- **Stat tiles** — best executable net spread right now, opportunities above
  threshold, venue health.
- **Market & asset filters** — all markets, or crypto / FX / metals.
- **Live opportunities table** — buy/sell venue and price, gross vs net spread
  (bps), estimated profit per $10k notional. Tradable legs only.
- **Spread history chart** — best gross and net spread over time per asset,
  with crosshair tooltip (1–72 h windows).
- **Price matrix per market** — every venue's mid price; cheapest ask and best
  bid highlighted; reference feeds tagged `ref`.

## How spreads are computed

For a candidate round trip (buy 1 unit at `ask` on venue A, sell at `bid` on
venue B):

```
cost     = ask × (1 + taker_A)
proceeds = bid × (1 − taker_B) × (1 − transfer_haircut)
net_bps  = (proceeds / cost − 1) × 10 000
```

- Quotes are only compared **within the same market**, and only within a
  **quote-equivalence group** (default: USD ≈ USDT ≈ USDC). Opportunities
  whose legs use different quote currencies are flagged (`†`) since they
  carry stablecoin basis risk.
- An opportunity requires **both legs on tradable venues**. Spreads against
  reference feeds are recorded as divergence history, not opportunities.
- The **transfer haircut** (default 5 bps) is a flat allowance for withdrawal
  fees / transfer slippage; tune it per your reality.
- Obviously broken ticks (zero or wildly crossed prices) are dropped.
- Slow reference feeds declare a minimum refresh interval and are served from
  cache between fetches.

## Configuration

Everything lives in [`config.yaml`](config.yaml) (optional — sane defaults are
built in): poll interval, reporting threshold, markets and their assets,
per-venue enable flags and fee tiers, DB path, history retention. Environment
overrides: `ARB_DB=path.sqlite3`, `ARB_CONFIG=path.yaml`.

## API

| Endpoint | Returns |
|---|---|
| `GET /api/status` | poll stats, markets, per-venue health |
| `GET /api/quotes?asset=BTC&market=crypto` | latest normalized bid/ask per venue |
| `GET /api/opportunities?min_net_bps=10&market=fx` | current opportunities + best spread per asset |
| `GET /api/history?asset=XAU&hours=6` | best-spread time series (for charts) |
| `GET /api/opportunities/recent?hours=24` | persisted opportunity log |

History and the opportunity log persist in SQLite (`arb.sqlite3`), pruned to
`history_retention_hours`.

## Live autonomous trading (opt-in, guarded)

The `arb/trading/` package can execute detected opportunities for real —
dual-leg immediate-or-cancel limit orders on Binance and Kraken, using
pre-positioned balances on both venues. See [ARCHITECTURE.md](ARCHITECTURE.md)
for the full design and guardrail table.

**It is disarmed by default.** Arming requires *all three*:

1. `trading.enabled: true` in `config.yaml`
2. `ARB_TRADING_ARMED=I-ACCEPT-THE-RISK` in the environment
3. Venue credentials: `ARB_BINANCE_API_KEY`/`ARB_BINANCE_API_SECRET`,
   `ARB_KRAKEN_API_KEY`/`ARB_KRAKEN_API_SECRET`

```bash
python -m arb.trade check    # verify arming + credentials, print limits
python -m arb.trade run      # start the autonomous loop
python -m arb.trade status   # audit trail + daily stats
touch TRADING_KILL_SWITCH    # halt trading immediately, no deploy needed
```

Every trade passes a risk gate first: per-trade/daily notional caps, daily
loss limit, trade-count cap, per-asset cooldown, stale-quote rejection,
venue/asset allowlists, balance pre-checks on both legs, and a circuit
breaker that disarms after consecutive failures. Every attempt — filled,
partial, or failed — lands in the `trades` audit table (visible at
`/api/trades` on the self-hosted server). Trading never runs on the Vercel
deployment; it is a separate self-hosted process.

The dashboard's **Trading panel** shows armed/kill-switch state, limits,
24-hour stats and the latest audit rows, with one-click **kill switch**
engage (release is token-gated via `ARB_CONTROL_TOKEN`). On the Vercel
deployment the panel proxies to your self-hosted server: set
`ARB_CONTROL_URL` (and `ARB_CONTROL_TOKEN`) in the Vercel project's
environment variables; until then it reports "not connected".

Start with the default caps ($100/trade, $1,000/day) and exchange
sub-accounts holding only what you are prepared to lose. Cross-exchange
crypto arbitrage spreads are usually thinner than fees + slippage; expect
the risk gate to reject almost everything — that is it working.

## Tests

```bash
pytest
```

## Extending

- **New venue**: subclass `Adapter` in `arb/exchanges.py` (one `fetch`
  method), declare the markets it serves and whether it's tradable, register
  it in `ADAPTERS`, add a config entry with its fee.
- **New market type** (equities, prediction markets, …): add a market key in
  config and adapters that produce `Quote` objects for it — the engine only
  sees quotes. Note that equities lack free multi-venue feeds, which is why
  they're not included out of the box.

## Caveats

Paper spreads ≠ executable profit: real arbitrage adds slippage, order-book
depth limits, transfer latency (price moves while assets are in flight),
per-asset withdrawal fees, and USD↔USDT basis risk. PAXG carries its own
premium/discount to spot gold. FX venue coverage is thin (Kraken and Bitstamp
are the tradable fiat venues), and reference feeds are fixings, not tradable
prices. Some venues geo-block API access by IP (e.g. Binance from US
addresses) — those feeds simply show as degraded. **This tool observes and
records; it does not place orders.**
