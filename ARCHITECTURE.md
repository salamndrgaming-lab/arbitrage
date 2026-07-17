# Architecture

Live data only, end to end. There is no simulated or demo data path anywhere
in this codebase; every price comes from a venue API at request/poll time.

```
                    ┌───────────────────────────────────────────────┐
                    │              MARKET DATA LAYER                │
                    │  arb/exchanges.py — 12 keyless adapters       │
                    │  8 tradable exchanges + 4 reference feeds     │
                    └──────────────────┬────────────────────────────┘
                                       │ Quote(market, base, quote, bid, ask)
                    ┌──────────────────▼────────────────────────────┐
                    │                ENGINE (pure)                  │
                    │  arb/engine.py — fee-netted cross-venue       │
                    │  spreads; reference feeds never form an       │
                    │  arbitrage leg; markets never cross           │
                    └───────┬──────────────────────────┬────────────┘
                            │                          │
              ┌─────────────▼───────────┐   ┌──────────▼─────────────────┐
              │  OBSERVATION SURFACES   │   │   TRADING LAYER (opt-in)   │
              │  arb/server.py (poll +  │   │   arb/trading/*            │
              │   SQLite history + UI)  │   │   risk gate → dual-leg IOC │
              │  arb/webapp.py (Vercel, │   │   execution → audit trail  │
              │   on-demand, no DB)     │   │   NEVER runs on Vercel     │
              │  arb/cli.py (terminal)  │   └──────────┬─────────────────┘
              └─────────────┬───────────┘              │
                            │       ┌──────────────────▼──┐
                            └──────►│  SQLite (arb.sqlite3)│
                                    │  spreads / opps /    │
                                    │  trades (audit)      │
                                    └──────────────────────┘
```

## Components

| Component | File(s) | Role |
|---|---|---|
| Venue adapters | `arb/exchanges.py` | Normalize public tickers into `Quote`s; tradable vs reference distinction |
| Engine | `arb/engine.py` | Pure math: gross/net spreads, opportunity + best-spread detection |
| Poller | `arb/poller.py` | Concurrent fetch loop, per-venue health, reference-feed caching |
| Store | `arb/store.py` | SQLite: spread history, opportunity log, **trade audit trail** |
| Server | `arb/server.py` | Self-hosted dashboard + JSON API (background poller + history) |
| Serverless | `arb/webapp.py`, `api/index.py` | Vercel variant: on-demand scans, browser-side history |
| Risk gate | `arb/trading/risk.py` | Every-trade limit checks, kill switch, circuit breaker |
| Execution | `arb/trading/execution.py` | Authenticated Binance/Kraken IOC limit orders + balances |
| Trader | `arb/trading/trader.py` | Autonomous loop: data → gate → balances → dual-leg fire → audit |
| Entrypoints | `arb/cli.py`, `arb/trade.py` | Scanner CLI; trading `check` / `status` / `run` |

## Trading design

**Strategy shape.** Pre-positioned inventory on both venues: quote currency
(USD/USDT) on the buy venue, the base asset on the sell venue. A trade fires
both legs *concurrently* as immediate-or-cancel limit orders at the observed
prices — an IOC can fill at the quoted price or better, or cancel; it can
never chase a moving market. No funds are ever transferred between venues
mid-trade (transfer latency is the classic way cross-exchange arbitrage
loses money). Inventory rebalancing is a manual/roadmap concern.

**One trade per cycle, maximum.** The trader evaluates the ranked
opportunity list each poll cycle and fires at most one trade, then waits a
full cycle. Combined with per-asset cooldowns this bounds worst-case burst
behavior to something a human can watch.

## Guardrails (all enforced in code, all on by default)

| Guardrail | Default | Where |
|---|---|---|
| Triple arming: config flag + `ARB_TRADING_ARMED=I-ACCEPT-THE-RISK` + API keys | disarmed | `trader.arm_check` |
| Kill-switch file / `ARB_KILL_SWITCH` env — re-checked before every trade | `TRADING_KILL_SWITCH` | `risk.kill_switch_active` |
| Per-trade notional cap | $100 | `risk.evaluate` |
| Rolling-24h notional cap | $1,000 | `risk.evaluate` (reads DB, survives restarts) |
| Rolling-24h trade-count cap | 20 | `risk.evaluate` |
| Daily loss limit | $50 | `risk.evaluate` |
| Execution threshold above display threshold | 30 bps net | `risk.evaluate` |
| Stale-quote rejection | > 3 s old | `risk.evaluate` |
| Per-asset cooldown | 60 s | `risk.evaluate` |
| Venue / asset / market allowlists | binance+kraken, BTC+ETH, crypto | `risk.evaluate` |
| Balance pre-check on both legs (with fee headroom) | always | `trader._check_balances` |
| IOC-only orders (no resting orders, no market orders) | always | `execution.py` |
| Circuit breaker: N consecutive failures → disarm until restart | 3 | `risk.record_result` |
| Partial fill counts as a failure (one-sided exposure) | always | `trader._execute` |
| Full audit trail (every attempt, including failures) | always | `store.trades` |
| Trading code excluded from serverless deploys | always | `webapp.py` never imports `arb.trading` |
| Credentials only via environment, never config/logs | always | `execution.from_env` |

## Failure handling

- **One leg errors, one fills** → recorded as `partial`, counts toward the
  circuit breaker; the operator resolves the one-sided position manually.
  Automatic unwind (market-out the filled leg) is the top roadmap item.
- **Both legs error** → `failed`, circuit breaker increments, nothing moved.
- **Any uncaught exception in a cycle** → logged, counted as a failure.
- **Circuit breaker trips** → loop exits; restart is a deliberate human act.
- **Data feed degrades** → the venue drops out of the opportunity set;
  stale-quote and executable checks keep half-fresh spreads untradable.

## Deployment topology

- **Self-hosted (`uvicorn arb.server:app`)** — dashboard + history DB.
- **Trader (`python -m arb.trade run`)** — separate process, same DB, same
  machine. Deliberately not embedded in the web server: the dashboard can
  restart without touching trading, and vice versa.
- **Vercel (`api/index.py`)** — observation only. Serverless instances are
  ephemeral and anonymous-facing; they must never hold trading credentials.

## Roadmap (in order)

1. **Auto-unwind of partial fills** — market-out the filled leg immediately,
   bounding one-sided exposure to seconds.
2. **Venue precision filters** — pull `exchangeInfo` / `AssetPairs` lot/tick
   rules instead of coarse rounding (today a precision miss safely rejects).
3. **WebSocket order books** — depth-aware sizing and sub-second quotes to
   replace REST top-of-book polling.
4. **Inventory tracking + rebalancing alerts** — track per-venue inventory
   drift, alert (not act) when a transfer is worth it.
5. **FX/metals execution** — extend execution adapters to Kraken/Bitstamp
   fiat pairs and PAXG once crypto-leg behavior is proven.
6. **Notifications** — push/webhook on trade, partial, or breaker trip.
