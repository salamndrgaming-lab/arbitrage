"""Live trading entrypoint:  python -m arb.trade <command>

Commands:
    check    verify arming preconditions and credentials, then exit
    status   print risk limits and last-24h trade stats, then exit
    run      start the autonomous trading loop (requires full arming)

Arming requires ALL of:
    - trading.enabled: true in config.yaml
    - ARB_TRADING_ARMED=I-ACCEPT-THE-RISK in the environment
    - ARB_<VENUE>_API_KEY / ARB_<VENUE>_API_SECRET for every trading venue

Stop a running trader at any time by creating the kill-switch file
(default: TRADING_KILL_SWITCH in the working directory) or setting
ARB_KILL_SWITCH=1 — it is re-checked before every trade.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys

from .config import load_config
from .store import Store
from .trading.execution import ExecutionError, build_executors
from .trading.trader import NotArmedError, Trader, arm_check

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


def cmd_check(cfg) -> int:
    try:
        arm_check(cfg)
        executors = build_executors(cfg.trading.venues)
    except (NotArmedError, ExecutionError) as exc:
        print(f"NOT ARMED: {exc}")
        return 1
    print(f"armed: venues={list(executors)} assets={cfg.trading.assets}")
    print("limits:", json.dumps({
        "min_execute_bps": cfg.trading.min_execute_bps,
        "max_trade_notional_usd": cfg.trading.max_trade_notional_usd,
        "max_daily_notional_usd": cfg.trading.max_daily_notional_usd,
        "max_trades_per_day": cfg.trading.max_trades_per_day,
        "max_daily_loss_usd": cfg.trading.max_daily_loss_usd,
    }, indent=2))
    return 0


def cmd_status(cfg) -> int:
    store = Store(cfg.db_path)
    try:
        stats = store.trade_stats_since(0)
        recent = store.recent_trades(hours=24)
        print(f"all-time: {stats['count']} trades, ${stats['notional']:.2f} notional,"
              f" est. realized ${stats['realized_pnl']:.2f}")
        print(f"last 24h: {len(recent)} trades")
        for t in recent[:10]:
            print(f"  [{t['status']:>7}] {t['base']:<5} "
                  f"{t['buy_exchange']}->{t['sell_exchange']} "
                  f"${t['notional_usd']:.0f} @ {t['expected_net_bps']:.1f} bps  "
                  f"{t['detail'][:60]}")
    finally:
        store.close()
    return 0


def cmd_run(cfg) -> int:
    store = Store(cfg.db_path)
    try:
        trader = Trader(cfg, store)
    except (NotArmedError, ExecutionError) as exc:
        print(f"refusing to start: {exc}")
        store.close()
        return 1
    try:
        asyncio.run(trader.run_forever())
    except KeyboardInterrupt:
        pass
    finally:
        store.close()
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="Live autonomous arbitrage trading")
    ap.add_argument("command", choices=["check", "status", "run"])
    ap.add_argument("--config", default=None, help="path to config.yaml")
    args = ap.parse_args()
    cfg = load_config(args.config)
    sys.exit({"check": cmd_check, "status": cmd_status, "run": cmd_run}[args.command](cfg))


if __name__ == "__main__":
    main()
