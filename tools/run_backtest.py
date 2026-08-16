"""Backtest the shipped strategy on real Hyperliquid candles.

Dev tool, not shipped in the app. Run from the project root:

    .\\venv\\Scripts\\python.exe tools\\run_backtest.py
    .\\venv\\Scripts\\python.exe tools\\run_backtest.py --timeframes 1h 4h 1d

It exists to answer one question with evidence instead of opinion: does a change to
the settings actually improve expectancy? Read the numbers before making one.

Two things to read carefully, because both have already misled someone:

**Trade counts, not just expectancy.** A row of 2 trades and a row of 55 look alike
in this table and are not alike at all. Rows below `MEANINGFUL_TRADES` are marked.

**Every variant is measured on the same history.** Compare six and the best one is
partly luck; it is a starting point for a test, not a result.

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
from src.strategy import create  # noqa: E402

#: Below this, a row is noise dressed as evidence. Twenty trades of a 40% winner
#: swings between 4 and 12 wins on chance alone, which is the whole range between
#: "promising" and "hopeless".
MEANINGFUL_TRADES = 25

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

#: Variants to compare, per strategy. Each entry is a set of constructor overrides;
#: the point is to read the columns against each other, not to admire one number.
CONFIGS: dict[str, dict[str, dict]] = {
    # The knobs the user actually has in Settings, so the table answers "what should
    # I set these to" rather than some question nobody asked.
    "volume_rejection": {
        "spec 0.1% 2R": {},
        "buffer 0.4% 2R": {"stop_buffer": 0.004},
        "buffer 0.8% 2R": {"stop_buffer": 0.008},
        "spec 0.1% 1.5R": {"take_profit_rr": 1.5},
        "spec 0.1% 1R": {"take_profit_rr": 1.0},
        "buffer 0.8% 1R": {"stop_buffer": 0.008, "take_profit_rr": 1.0},
    },
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
    parser.add_argument(
        "--strategy",
        default="volume_rejection",
        choices=sorted(CONFIGS),
        help="which strategy's variants to compare",
    )
    args = parser.parse_args()

    timeframes = [Timeframe(value) for value in args.timeframes]
    variants = CONFIGS[args.strategy]

    print(f"Fetching {args.coin} candles from Hyperliquid...")
    history = asyncio.run(fetch(args.coin, timeframes))

    print(f"\n{args.strategy}, {args.fee_bps}bps round-trip fees")
    print("Expectancy is R per trade, net of fees. Positive is profitable.\n")

    #: label -> (total R across every trade, total trades). Summed, not averaged
    #: per timeframe: a weekly run of 2 trades once counted as much as a 30-minute
    #: run of 55, which turned a strategy that lost on every liquid timeframe into
    #: a positive-looking average.
    totals: dict[str, tuple[float, int]] = {label: (0.0, 0) for label in variants}

    for timeframe in timeframes:
        candles = history[timeframe]
        print(f"--- {timeframe.label} ({len(candles):,} candles) ---")
        for label, params in variants.items():
            strategy = create(args.strategy, **params)
            if len(candles) <= strategy.warmup_candles:
                print(f"  {label:15s} not enough history (needs {strategy.warmup_candles})")
                continue
            result = run_backtest(strategy, candles, fee_bps=args.fee_bps)
            marker = "" if result.count >= MEANINGFUL_TRADES else "   <- too few to read"
            print(f"  {label:15s} {result.summary()}{marker}")
            if result.count:
                carried_r, carried_n = totals[label]
                totals[label] = (
                    carried_r + result.expectancy_r * result.count,
                    carried_n + result.count,
                )
        print()

    print(f"--- expectancy per trade, pooled across timeframes ---")
    print(f"    Every trade counts once. A timeframe with {MEANINGFUL_TRADES} trades or")
    print(f"    fewer is marked above and carries correspondingly little weight here.\n")
    for label, (total_r, count) in totals.items():
        if count:
            print(f"  {label:15s} {total_r / count:+.3f}R over {count:,} trades")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
