"""Headless runner — the same engine, driven from a terminal.

Useful when the window is in the way: over SSH, on a VPS, or when watching the raw
log during development.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from . import secrets_store
from .broker.paper import PaperBroker
from .config import AppSettings, load_settings, save_settings
from .core.models import MarginMode, Timeframe
from .db import connect
from .engine import BotEngine
from .logging_setup import setup_logging
from .paths import log_path
from .session import open_session
from .store import statistics

log = logging.getLogger("hypertrade")


def apply_overrides(settings: AppSettings, args: argparse.Namespace) -> AppSettings:
    if args.timeframe:
        settings.timeframe = Timeframe(args.timeframe)
    if args.risk is not None:
        settings.risk_usdc = args.risk
    if args.leverage is not None:
        settings.leverage = args.leverage
    if args.margin:
        settings.margin_mode = MarginMode(args.margin)
    if args.balance is not None:
        settings.paper_starting_balance = args.balance
    return settings


async def _heartbeat(engine: BotEngine, broker: PaperBroker, seconds: float) -> None:
    """Show signs of life between candle closes, which on 4h are hours apart."""
    while True:
        await asyncio.sleep(seconds)
        account = await broker.account_state()
        held = await broker.managed_position()
        if held is None:
            where = "flat"
        else:
            where = (
                f"{held.position.side.value} {held.position.abs_size:g} "
                f"@ {held.position.entry_price:g} "
                f"(stop {held.stop_price:g}, pnl {held.position.unrealized_pnl:+.2f})"
            )
        log.info(
            "mark %g | equity %.2f USDC | %s", engine.last_mark, account.account_value, where
        )


def _report(broker: PaperBroker, conn) -> None:
    stats = statistics(conn, broker.mode)
    log.info(
        "session over - balance %.2f USDC | %d closed trades, %d won (%.0f%%), "
        "net %+.2f USDC, fees %.2f",
        broker.balance, stats.closed_trades, stats.wins, 100 * stats.win_rate,
        stats.total_pnl, stats.total_fees,
    )


async def run(args: argparse.Namespace) -> int:
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    log.info("HyperTrade - logging to %s", log_path())

    conn = connect()
    try:
        settings = apply_overrides(load_settings(conn), args)
        if args.save:
            save_settings(conn, settings)

        problems = settings.validate(has_agent_key=secrets_store.has_agent_key())
        if problems:
            for problem in problems:
                log.error("%s", problem)
            return 1

        # Same assembly the desktop app uses, so the two cannot drift apart.
        session = await open_session(conn, settings, poll_seconds=args.poll)
        broker, engine = session.broker, session.engine
        try:
            if session.fell_back_to_paper:
                log.error(
                    "Live mode refused, running in PAPER instead: %s",
                    session.fell_back_to_paper,
                )
            elif settings.is_live:
                log.warning("LIVE MODE - orders will spend real USDC")
            if args.reset_paper:
                broker.reset(settings.paper_starting_balance)

            if args.once:
                await engine.prepare()
                await engine.tick()
                _report(broker, conn)
                return 0

            await engine.start()
            log.info("running - press Ctrl+C to stop")
            pulse = asyncio.create_task(_heartbeat(engine, broker, args.heartbeat))
            try:
                while engine.is_running:
                    await asyncio.sleep(0.5)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                pulse.cancel()
                _report(broker, conn)
        finally:
            await session.aclose()
        return 0
    finally:
        conn.close()
