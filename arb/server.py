"""FastAPI app: JSON API plus the static dashboard.

Run with:  uvicorn arb.server:app  (or `python -m arb.server`)
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .poller import Poller
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

cfg = load_config()
store = Store(cfg.db_path)
poller = Poller(cfg, store)


@asynccontextmanager
async def lifespan(app: FastAPI):
    stop = asyncio.Event()
    task = asyncio.create_task(poller.run_forever(stop))
    yield
    stop.set()
    await task
    store.close()


app = FastAPI(title="Market Arbitrage Tracker", lifespan=lifespan)


@app.get("/api/status")
def status():
    st = poller.snapshot()
    st["history_available"] = True
    return st


@app.get("/api/scan")
def scan():
    """Combined snapshot: one round-trip for the dashboard."""
    st = poller.snapshot()
    st["history_available"] = True
    return {
        "status": st,
        "opportunities": [o.to_dict() for o in poller.opportunities],
        "best_spreads": {b: o.to_dict() for b, o in poller.best.items()},
        "quotes": [q.to_dict() for qs in poller.quotes.values() for q in qs],
    }


@app.get("/api/quotes")
def quotes(asset: str | None = None, market: str | None = None):
    out = [q.to_dict() for qs in poller.quotes.values() for q in qs]
    if asset:
        out = [q for q in out if q["base"] == asset.upper()]
    if market:
        out = [q for q in out if q["market"] == market.lower()]
    return {"quotes": out}


@app.get("/api/opportunities")
def opportunities(
    min_net_bps: float | None = None,
    asset: str | None = None,
    market: str | None = None,
):
    opps = poller.opportunities
    if min_net_bps is not None:
        opps = [o for o in opps if o.net_bps >= min_net_bps]
    if asset:
        opps = [o for o in opps if o.base == asset.upper()]
    if market:
        opps = [o for o in opps if o.market == market.lower()]
    return {
        "opportunities": [o.to_dict() for o in opps],
        "best_spreads": {b: o.to_dict() for b, o in poller.best.items()},
    }


@app.get("/api/history")
def history(
    asset: str = Query(...),
    hours: float = Query(6.0, gt=0, le=168),
):
    asset = asset.upper()
    if asset not in cfg.all_assets:
        raise HTTPException(404, f"unknown asset {asset!r}; tracked: {cfg.all_assets}")
    return {"asset": asset, "points": store.spread_history(asset, hours)}


@app.get("/api/opportunities/recent")
def recent(hours: float = Query(24.0, gt=0, le=168), limit: int = Query(200, le=1000)):
    return {"opportunities": store.recent_opportunities(hours, limit)}


@app.get("/api/trades")
def trades(hours: float = Query(24.0, gt=0, le=720), limit: int = Query(200, le=1000)):
    """Audit trail written by the (separately run) trader process."""
    return {
        "trades": store.recent_trades(hours, limit),
        "stats_24h": store.trade_stats_since(time.time() - 24 * 3600),
    }


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def main():
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
