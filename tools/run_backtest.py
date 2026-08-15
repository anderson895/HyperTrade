"""Backtest the trend-following strategy on real Hyperliquid candles.

Dev tool, not shipped in the app. Run from the project root:

    .\\venv\\Scripts\\python.exe tools\\run_backtest.py
    .\\venv\\Scripts\\python.exe tools\\run_backtest.py --timeframes 1h 4h 1d

It exists to answer one question with evidence instead of opinion: do the optional
chop filters actually improve expectancy? Read the numbers before enabling one.

Caveat carried from src/backtest.py: stops are assumed to fill exactly at the stop
price, so every expectancy printed here is optimistic.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.backtest import run_backtest  # noqa: E402
from src.core.models import Timeframe  # noqa: E402
from src.data.hl_info import HyperliquidInfo  # noqa: E402
from src.strategy import TrendFollowing  # noqa: E402

#: How much history to pull per timeframe. Short timeframes need paging past the
#: 5000-candle cap; long ones are limited by how long BTC has traded on Hyperliquid.
CANDLE_COUNTS = {
    Timeframe.M5: 20_000,
    Timeframe.M15: 20_000,
    Timeframe.M30: 15_000,
    Timeframe.H1: 15_000,
    Timeframe.H4: 8_000,
    Timeframe.D1: 3_000,
    Timeframe.W1: 600,
}

CONFIGS: dict[str, dict] = {
    "no filter": {},
    "slope lb=20": {"slope_lookback": 20},
    "EMA200 trend": {"trend_filter_period": 200},
    "EMA200 + lb20": {"trend_filter_period": 200, "slope_lookback": 20},
}


async def fetch(coin: str, timeframes: list[Timeframe]) -> dict[Timeframe, list]:
    async with HyperliquidInfo() as info:
        out = {}
        for timeframe in timeframes:
            candles = await info.recent_candles(coin, timeframe, CANDLE_COUNTS[timeframe])
            span = ""
            if candles:
                span = f"{candles[0].open_time.date()} to {candles[-1].close_time.date()}"
            print(f"  {timeframe.value:>3s}: {len(candles):6,d} candles  {span}")
            out[timeframe] = candles
        return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--coin", default="BTC")
    parser.add_argument(
        "--timeframes",
        nargs="*",
        default=[timeframe.value for timeframe in Timeframe],
        help="e.g. 1h 4h 1d (default: all seven)",
    )
    parser.add_argument("--fee-bps", type=float, default=4.5)
    args = parser.parse_args()

    timeframes = [Timeframe(value) for value in args.timeframes]

    print(f"Fetching {args.coin} candles from Hyperliquid...")
    history = asyncio.run(fetch(args.coin, timeframes))

    print(f"\nTrend following, {args.fee_bps}bps round-trip fees, stop 2xATR, target 2R")
    print("Expectancy is R per trade, net of fees. Positive is profitable.\n")

    totals: dict[str, list[float]] = {label: [] for label in CONFIGS}

    for timeframe in timeframes:
        candles = history[timeframe]
        print(f"--- {timeframe.label} ({len(candles):,} candles) ---")
        for label, params in CONFIGS.items():
            strategy = TrendFollowing(**params)
            if len(candles) <= strategy.warmup_candles:
                print(f"  {label:15s} not enough history (needs {strategy.warmup_candles})")
                continue
            result = run_backtest(strategy, candles, fee_bps=args.fee_bps)
            print(f"  {label:15s} {result.summary()}")
            if result.count:
                totals[label].append(result.expectancy_r)
        print()

    print("--- average expectancy across timeframes ---")
    for label, values in totals.items():
        if values:
            average = sum(values) / len(values)
            print(f"  {label:15s} {average:+.3f}R over {len(values)} timeframes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
