"""SQLite persistence for opportunities and spread history."""

from __future__ import annotations

import sqlite3
import threading
import time

from .models import Opportunity

SCHEMA = """
CREATE TABLE IF NOT EXISTS opportunities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    market TEXT NOT NULL DEFAULT 'crypto',
    base TEXT NOT NULL,
    buy_exchange TEXT NOT NULL,
    buy_quote TEXT NOT NULL,
    buy_price REAL NOT NULL,
    sell_exchange TEXT NOT NULL,
    sell_quote TEXT NOT NULL,
    sell_price REAL NOT NULL,
    gross_bps REAL NOT NULL,
    net_bps REAL NOT NULL,
    cross_quote INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_opp_ts ON opportunities (ts);
CREATE INDEX IF NOT EXISTS idx_opp_base_ts ON opportunities (base, ts);

CREATE TABLE IF NOT EXISTS spreads (
    ts REAL NOT NULL,
    market TEXT NOT NULL DEFAULT 'crypto',
    base TEXT NOT NULL,
    buy_exchange TEXT NOT NULL,
    sell_exchange TEXT NOT NULL,
    gross_bps REAL NOT NULL,
    net_bps REAL NOT NULL,
    executable INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_spread_base_ts ON spreads (base, ts);
"""

# Columns added since the first release; applied to pre-existing databases.
MIGRATIONS = [
    "ALTER TABLE opportunities ADD COLUMN market TEXT NOT NULL DEFAULT 'crypto'",
    "ALTER TABLE spreads ADD COLUMN market TEXT NOT NULL DEFAULT 'crypto'",
    "ALTER TABLE spreads ADD COLUMN executable INTEGER NOT NULL DEFAULT 1",
]


class Store:
    def __init__(self, path: str = "arb.sqlite3"):
        # The poller thread and API handlers share this store; serialize access.
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            for stmt in MIGRATIONS:
                try:
                    self._conn.execute(stmt)
                except sqlite3.OperationalError:
                    pass  # column already exists

    def record_opportunities(self, opps: list[Opportunity]) -> None:
        if not opps:
            return
        rows = [
            (o.ts, o.market, o.base, o.buy_exchange, o.buy_quote, o.buy_price,
             o.sell_exchange, o.sell_quote, o.sell_price,
             o.gross_bps, o.net_bps, int(o.cross_quote))
            for o in opps
        ]
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO opportunities (ts, market, base, buy_exchange,"
                " buy_quote, buy_price, sell_exchange, sell_quote, sell_price,"
                " gross_bps, net_bps, cross_quote) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )

    def record_spreads(self, best: dict[str, Opportunity]) -> None:
        if not best:
            return
        rows = [
            (o.ts, o.market, base, o.buy_exchange, o.sell_exchange,
             o.gross_bps, o.net_bps, int(o.executable))
            for base, o in best.items()
        ]
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO spreads (ts, market, base, buy_exchange,"
                " sell_exchange, gross_bps, net_bps, executable)"
                " VALUES (?,?,?,?,?,?,?,?)",
                rows,
            )

    def spread_history(self, base: str, hours: float = 6.0, limit: int = 2000) -> list[dict]:
        since = time.time() - hours * 3600
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, buy_exchange, sell_exchange, gross_bps, net_bps,"
                " executable FROM spreads WHERE base = ? AND ts >= ?"
                " ORDER BY ts DESC LIMIT ?",
                (base, since, limit),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def recent_opportunities(self, hours: float = 24.0, limit: int = 200) -> list[dict]:
        since = time.time() - hours * 3600
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM opportunities WHERE ts >= ?"
                " ORDER BY ts DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def prune(self, retention_hours: float) -> None:
        cutoff = time.time() - retention_hours * 3600
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM opportunities WHERE ts < ?", (cutoff,))
            self._conn.execute("DELETE FROM spreads WHERE ts < ?", (cutoff,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
