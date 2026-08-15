"""Bridges the async engine to the Qt widgets.

The widgets never touch the engine or the broker directly. They call the coroutines
here and render whatever `Snapshot` comes back, which keeps every await in one file
and the UI free of trading logic. The objects themselves are assembled by
`session.open_session`, the same call the console runner makes.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from ..broker.base import ManagedPosition
from ..broker.paper import PaperBroker
from ..config import AppSettings, save_settings
from ..core.models import AssetMeta, Candle, Timeframe, TradingMode
from ..engine import BotEngine
from ..errors import FEED_ERRORS, TRADING_ERRORS
from ..session import Session, open_session
from ..strategy import TrendFollowing

log = logging.getLogger(__name__)


@dataclass
class Snapshot:
    """Everything the dashboard draws, gathered in one pass."""

    ready: bool = False
    running: bool = False
    connected: bool = False
    mode: TradingMode = TradingMode.PAPER
    mark: float = 0.0
    equity: float = 0.0
    balance: float = 0.0
    withdrawable: float = 0.0
    margin_used: float = 0.0
    position: ManagedPosition | None = None
    error: str | None = None


class BotController(QObject):
    updated = Signal(object)  # Snapshot
    failed = Signal(str)
    settings_applied = Signal(object)  # AppSettings

    def __init__(self, conn: sqlite3.Connection, settings: AppSettings) -> None:
        super().__init__()
        self.conn = conn
        self.settings = settings
        self.session: Session | None = None
        self._error: str | None = None

    # --- what the widgets read -------------------------------------------

    @property
    def asset(self) -> AssetMeta | None:
        return self.session.asset if self.session else None

    @property
    def broker(self) -> PaperBroker | None:
        return self.session.broker if self.session else None

    @property
    def engine(self) -> BotEngine | None:
        return self.session.engine if self.session else None

    def strategy_parameters(self) -> dict:
        """What the running strategy is configured with, for the chart's overlay.

        Falls back to a fresh strategy's defaults before the session exists, so the
        dashboard can draw its legend the moment the window opens.
        """
        strategy = self.session.engine.strategy if self.session else TrendFollowing()
        return strategy.parameters()

    # --- lifecycle -------------------------------------------------------

    async def initialise(self) -> bool:
        """Connect, read the asset's constraints, and load the chart's history."""
        try:
            self.session = await open_session(self.conn, self.settings)
            # Loaded now rather than on START, so the chart is populated the moment
            # the window opens.
            await self.session.engine.load_history()
            self._error = None
            return True
        except FEED_ERRORS as exc:
            log.warning("could not reach Hyperliquid: %s", exc)
            self._fail(f"Could not reach Hyperliquid: {exc}")
            return False
        except RuntimeError as exc:
            # Raised by load_history when the timeframe has too little history.
            log.warning("could not load history: %s", exc)
            self._fail(str(exc))
            return False

    async def shutdown(self) -> None:
        if self.session is not None:
            await self.session.aclose()

    # --- actions ---------------------------------------------------------

    async def start(self) -> None:
        if self.session is None or self.session.engine.is_running:
            return
        try:
            await self.session.engine.start()
            self._error = None
        except TRADING_ERRORS as exc:
            log.warning("could not start the bot: %s", exc)
            self._fail(f"Could not start: {exc}")
        await self.refresh()

    async def stop(self) -> None:
        if self.session is not None:
            await self.session.engine.stop()
        await self.refresh()

    async def close_position(self) -> None:
        if self.session is None:
            return
        try:
            await self.session.engine.close_now()
        except TRADING_ERRORS as exc:
            log.warning("could not close the position: %s", exc)
            self._fail(f"Could not close the position: {exc}")
        await self.refresh()

    async def apply_settings(self, settings: AppSettings) -> None:
        """Persist and re-wire. Only reachable while the bot is stopped."""
        save_settings(self.conn, settings)
        self.settings = settings
        if self.session is not None:
            self.session.apply_settings(settings)
        log.info(
            "settings saved: %s %s, risk %g USDC, %dx %s",
            settings.coin, settings.timeframe.label, settings.risk_usdc,
            settings.leverage, settings.margin_mode.value,
        )
        for note in settings.advisories():
            log.warning(note)
        self.settings_applied.emit(settings)
        await self.refresh()

    async def reset_paper(self) -> None:
        if self.session is not None:
            self.session.broker.reset(self.settings.paper_starting_balance)
        await self.refresh()

    async def fetch_chart_candles(self, timeframe: Timeframe, count: int) -> list[Candle]:
        """History for a chart view other than the bot's own timeframe.

        Display only. The strategy is never given these — it would then be deciding
        on a resolution it was never measured on.
        """
        if self.session is None:
            return []
        return await self.session.info.recent_candles(self.settings.coin, timeframe, count)

    # --- polling ---------------------------------------------------------

    async def refresh(self) -> None:
        session = self.session
        snapshot = Snapshot(
            ready=session is not None,
            running=bool(session and session.engine.is_running),
            mode=session.broker.mode if session else TradingMode.PAPER,
            error=self._error,
        )
        if session is None:
            self.updated.emit(snapshot)
            return

        # Polled before the account is read, because a poll can settle a stop or a
        # target and the snapshot should show the result, not the position that just
        # closed. While the bot runs, its own loop does the polling.
        try:
            if not snapshot.running:
                await session.engine.poll()
            snapshot.mark = session.engine.last_mark
            snapshot.connected = True
            self._error = None
            snapshot.error = None
        except TRADING_ERRORS as exc:
            snapshot.connected = False
            snapshot.error = f"Price feed unavailable: {exc}"

        account = await session.broker.account_state()
        snapshot.equity = account.account_value
        snapshot.balance = session.broker.balance
        snapshot.withdrawable = account.withdrawable
        snapshot.margin_used = account.total_margin_used
        snapshot.position = await session.broker.managed_position()

        self.updated.emit(snapshot)

    def _fail(self, message: str) -> None:
        self._error = message
        self.failed.emit(message)
