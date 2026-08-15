"""HyperTrade entry point.

    .\\venv\\Scripts\\python.exe -m src.main              # the desktop window
    .\\venv\\Scripts\\python.exe -m src.main --console    # headless, in the terminal

Paper mode only for now: live execution is not implemented, and choosing it stops
with a message rather than pretending to trade.
"""

from __future__ import annotations

# Running this file directly - `python src\main.py`, or `python main.py` from inside
# src\ - leaves it outside its package, and every relative import below fails with
# "attempted relative import with no known parent package". Rather than make that the
# user's problem, put the project root on the path and re-run properly as a module.
if __package__ in (None, ""):
    import pathlib
    import runpy
    import sys

    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
    runpy.run_module("src.main", run_name="__main__", alter_sys=True)
    raise SystemExit(0)

import argparse
import asyncio
import logging

from .engine import DEFAULT_POLL_SECONDS

log = logging.getLogger("hypertrade")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hypertrade",
        description="BTC/USD trading bot on Hyperliquid.",
    )
    parser.add_argument(
        "--console", action="store_true", help="run headless instead of opening the window"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")

    console = parser.add_argument_group("console mode")
    console.add_argument("--timeframe", choices=["5m", "15m", "30m", "1h", "4h", "1d", "1w"])
    console.add_argument("--risk", type=float, help="risk per trade in USDC")
    console.add_argument("--leverage", type=int)
    console.add_argument("--margin", choices=["cross", "isolated"])
    console.add_argument("--balance", type=float, help="paper starting balance in USDC")
    console.add_argument("--poll", type=float, default=DEFAULT_POLL_SECONDS)
    console.add_argument("--heartbeat", type=float, default=60.0, help="status line seconds")
    console.add_argument("--reset-paper", action="store_true", help="wipe the paper account")
    console.add_argument("--save", action="store_true", help="persist these options as settings")
    console.add_argument("--once", action="store_true", help="run a single tick and exit")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.console:
        from . import console

        try:
            return asyncio.run(console.run(args))
        except KeyboardInterrupt:
            log.info("interrupted")
            return 0

    from .ui.app import run_gui

    return run_gui(args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
