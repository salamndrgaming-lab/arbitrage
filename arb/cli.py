"""Terminal scanner: one-shot or watch mode, no server needed.

Examples:
    python -m arb.cli --demo               # one scan against the simulated feed
    python -m arb.cli --watch               # live scan every poll interval
    python -m arb.cli --min-net-bps 5       # lower the reporting threshold
"""

from __future__ import annotations

import argparse
import asyncio
import time

import httpx

from .config import load_config
from .poller import Poller


def _fmt_price(p: float) -> str:
    return f"{p:,.6g}"


def render(poller: Poller) -> str:
    lines = []
    mode = "demo" if poller.demo_feed else "live"
    ok = sum(1 for s in poller.statuses.values() if s.ok)
    lines.append(
        f"[{time.strftime('%H:%M:%S')}] mode={mode}  "
        f"exchanges {ok}/{len(poller.statuses)} up  "
        f"min net spread {poller.cfg.min_net_bps:g} bps"
    )
    down = [s for s in poller.statuses.values() if not s.ok]
    for s in down:
        lines.append(f"  ! {s.name}: {s.error}")

    if poller.opportunities:
        lines.append("")
        lines.append(f"{'ASSET':<6} {'BUY @':<22} {'SELL @':<22} "
                     f"{'GROSS':>8} {'NET':>8}  {'PER $10K':>9}")
        for o in poller.opportunities[:15]:
            buy = f"{o.buy_exchange} {_fmt_price(o.buy_price)}"
            sell = f"{o.sell_exchange} {_fmt_price(o.sell_price)}"
            flag = "*" if o.cross_quote else " "
            lines.append(
                f"{o.base:<6} {buy:<22} {sell:<22} "
                f"{o.gross_bps:>7.1f}b {o.net_bps:>7.1f}b {o.net_bps:>8.2f}${flag}"
            )
        if any(o.cross_quote for o in poller.opportunities[:15]):
            lines.append("  * buy/sell legs use different quote currencies (USD vs USDT)")
    else:
        best = sorted(poller.best.values(), key=lambda o: o.net_bps, reverse=True)
        lines.append("no opportunities above threshold; closest spreads:")
        for o in best[:5]:
            lines.append(
                f"  {o.base:<6} {o.buy_exchange} -> {o.sell_exchange}  "
                f"net {o.net_bps:+.1f} bps"
            )
    return "\n".join(lines)


async def run(args: argparse.Namespace) -> None:
    cfg = load_config(args.config)
    if args.demo:
        cfg.mode = "demo"
    if args.min_net_bps is not None:
        cfg.min_net_bps = args.min_net_bps
    poller = Poller(cfg)  # no store: the CLI is a viewer, history lives in the server

    async with httpx.AsyncClient(timeout=10) as client:
        while True:
            await poller.run_cycle(None if poller.demo_feed else client)
            if args.watch:
                print("\033[2J\033[H", end="")  # clear screen
            print(render(poller))
            if not args.watch:
                break
            await asyncio.sleep(cfg.poll_interval)


def main() -> None:
    ap = argparse.ArgumentParser(description="Cross-exchange arbitrage scanner")
    ap.add_argument("--demo", action="store_true", help="use the simulated feed")
    ap.add_argument("--watch", action="store_true", help="rescan continuously")
    ap.add_argument("--min-net-bps", type=float, default=None,
                    help="override the reporting threshold")
    ap.add_argument("--config", default=None, help="path to config.yaml")
    args = ap.parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
