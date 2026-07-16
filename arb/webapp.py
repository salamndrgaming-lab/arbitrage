"""Serverless app (Vercel and similar): on-demand scans, no background loop.

Each ``/api/scan`` request fetches live quotes from every venue, runs the
engine, and returns status + quotes + opportunities in one payload. There is
no database in this mode — the dashboard accumulates spread history in the
browser instead. A short minimum gap between scans keeps a warm instance
from hammering venue APIs when several clients poll at once; reference
feeds additionally keep their own ``min_interval`` caching.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import load_config
from .poller import Poller

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static"

cfg = load_config(ROOT / "config.yaml")
# Advertised to the dashboard as its refresh cadence: serverless scans are
# heavier than reading a poller's cache, so default to a gentler pace.
cfg.poll_interval = float(os.environ.get("ARB_SCAN_INTERVAL", "15"))

scanner = Poller(cfg)  # no store: serverless instances are ephemeral

MIN_SCAN_GAP = float(os.environ.get("ARB_MIN_SCAN_GAP", "3"))
_lock = asyncio.Lock()
_client: httpx.AsyncClient | None = None

app = FastAPI(title="Market Arbitrage Tracker")


def _snapshot() -> dict:
    st = scanner.snapshot()
    st["history_available"] = False
    return st


@app.get("/api/scan")
async def scan():
    global _client
    async with _lock:
        if _client is None:
            _client = httpx.AsyncClient(timeout=8)
        if scanner.last_cycle is None or time.time() - scanner.last_cycle >= MIN_SCAN_GAP:
            await scanner.run_cycle(_client)
    return {
        "status": _snapshot(),
        "opportunities": [o.to_dict() for o in scanner.opportunities],
        "best_spreads": {b: o.to_dict() for b, o in scanner.best.items()},
        "quotes": [q.to_dict() for qs in scanner.quotes.values() for q in qs],
    }


@app.get("/api/status")
def status():
    return _snapshot()


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
