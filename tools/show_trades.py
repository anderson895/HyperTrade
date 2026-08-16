"""Replay the strategy over real candles and print every trade it would have taken.

Dev tool, not shipped in the app. Run from the project root:

    .\\venv\\Scripts\\python.exe tools\\show_trades.py
    .\\venv\\Scripts\\python.exe tools\\show_trades.py --timeframe 15m --candles 5000

`run_backtest.py` answers "is this profitable". This answers a different question —
*what does the bot actually do* — which is the one you have when the market is dead
and nothing has triggered for hours. It prints the entry, the stop, the target, what
was hit and how long it took, one line per trade.

Same replay engine as the backtest, so what you see here is what that table counted.
Its caveat carries over: stops are assumed to fill exactly at the stop price, so the
losses printed are the best case, never the worst.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest import ExitReason, run_backtest  # noqa: E402
from src.config import AppSettings  # noqa: E402
from src.core.models import Side, Timeframe  # noqa: E402
from src.data.hl_info import HyperliquidInfo  # noqa: E402
from src.strategy import create  # noqa: E402

MARK = {
    ExitReason.TAKE_PROFIT: "TARGET",
    ExitReason.STOP: "stop",
    ExitReason.END_OF_DATA: "still open at the end",
}


async def fetch(coin: str, timeframe: Timeframe, count: int):
    async with HyperliquidInfo() as info:
        return await info.recent_candles(coin, timeframe, count)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coin", default="BTC")
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--candles", type=int, default=5000)
    parser.add_argument("--fee-bps", type=float, default=4.5)
    parser.add_argument("--last", type=int, default=15, help="how many trades to show")
    parser.add_argument(
        "--equity", type=float, default=99.58, help="account size, to price R in USDC"
    )
    args = parser.parse_args()

    timeframe = Timeframe(args.timeframe)
    settings = AppSettings()
    strategy = create(
        settings.strategy,
        stop_buffer=settings.stop_buffer_pct,
        take_profit_rr=settings.take_profit_rr,
    )

    print(f"Fetching {args.coin} {timeframe.label} candles...")
    candles = asyncio.run(fetch(args.coin, timeframe, args.candles))
    if len(candles) <= strategy.warmup_candles:
        print(f"not enough history: {len(candles)} candles, needs {strategy.warmup_candles}")
        return 1

    result = run_backtest(strategy, candles, fee_bps=args.fee_bps)
    span = f"{candles[0].open_time:%Y-%m-%d %H:%M} to {candles[-1].close_time:%Y-%m-%d %H:%M} UTC"
    print(f"{len(candles):,} candles, {span}\n")

    if not result.trades:
        print("no trades in this window")
        return 0

    shown = result.trades[-args.last:]
    print(f"the last {len(shown)} of {result.count} trades this strategy would have taken:\n")
    header = (
        f"{'entered':>16s} {'side':>5s} {'entry':>9s} {'stop':>9s} {'target':>9s} "
        f"{'exit':>9s} {'result':>21s} {'R':>7s} {'held':>6s}"
    )
    print(header)
    print("-" * len(header))

    for trade in shown:
        entered = candles[trade.entry_index].close_time
        target = f"{trade.take_profit_price:,.0f}" if trade.take_profit_price else "-"
        held = trade.bars_held * timeframe.seconds // 60
        held_text = f"{held}m" if held < 120 else f"{held // 60}h"
        print(
            f"{entered:%Y-%m-%d %H:%M} "
            f"{'SHORT' if trade.side is Side.SHORT else 'LONG':>5s} "
            f"{trade.entry_price:9,.0f} {trade.stop_price:9,.0f} {target:>9s} "
            f"{trade.exit_price:9,.0f} {MARK[trade.exit_reason]:>21s} "
            f"{trade.r_multiple:+7.2f} {held_text:>6s}"
        )

    # R is the stake, so it is what the numbers above are worth on a real account.
    # The clamp is why that stake is not the 3% in Settings — see README.
    per_r = args.equity * 0.0054
    print(
        f"\nAt this account's clamped stake (about {per_r:,.2f} USDC per R), the "
        f"{len(shown)} trades above come to {sum(t.r_multiple for t in shown) * per_r:+,.2f} USDC."
    )
    print(
        f"All {result.count} trades in this window: {result.summary()}\n"
        f"Stops are modelled filling exactly at the stop price, so this is the "
        f"optimistic reading."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
