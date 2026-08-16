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
from ..core.models import AssetMeta, Candle, Side, Timeframe, TradingMode
from ..core.sizing import RejectReason, Rejection, minimum_leverage_for, plan_position
from ..engine import BotEngine
from ..errors import FEED_ERRORS, TRADING_ERRORS
from ..session import Session, open_session
from ..store import import_exchange_fills

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


@dataclass
class Preview:
    """What the settings on screen would do, at the prices in front of us.

    Answers the question the Settings page cannot otherwise answer: will this
    actually trade? A risk and leverage pair that can never fit produces no trades
    at all, which looks exactly like a market that never signals.
    """

    price: float = 0.0
    equity: float = 0.0
    stop_distance: float = 0.0
    size: float = 0.0
    notional: float = 0.0
    margin: float = 0.0
    problem: str | None = None
    hint: str | None = None

    @property
    def ready(self) -> bool:
        return self.price > 0 and self.stop_distance > 0


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

    # --- lifecycle -------------------------------------------------------

    async def initialise(self) -> bool:
        """Connect, read the asset's constraints, and load the chart's history."""
        try:
            self.session = await open_session(self.conn, self.settings)
            # Loaded now rather than on START, so the chart is populated the moment
            # the window opens.
            await self.session.engine.load_history()
            self._error = None
            self._warn_if_live_refused()
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
            await self.session.apply_settings(settings)
            self._warn_if_live_refused()
        log.info(
            "settings saved: %s %s, %s, risk %s, %dx %s",
            settings.coin, settings.timeframe.label, settings.strategy,
            f"{settings.risk_pct:.2%} of equity" if settings.risk_pct
            else f"{settings.risk_usdc:g} USDC",
            settings.leverage, settings.margin_mode.value,
        )
        for note in settings.advisories():
            log.warning(note)
        self.settings_applied.emit(settings)
        await self.refresh()

    async def reset_paper(self) -> None:
        """Wipe the simulated account back to its starting balance.

        Guarded rather than trusted. `reset` exists only on the paper broker — a
        live account cannot be reset, and should not pretend to be — so in Live this
        raised `AttributeError: 'LiveBroker' object has no attribute 'reset'` from
        inside a fire-and-forget task, where the traceback goes to the log and the
        user sees nothing at all. Same shape as the missing `balance` that took the
        dashboard down: paper-only behaviour reached through the shared interface.
        """
        if self.session is None:
            return
        if not isinstance(self.session.broker, PaperBroker):
            self._fail(
                "Reset is for the simulated account only - a live balance is real "
                "money and is not resettable."
            )
            return
        self.session.broker.reset(self.settings.paper_starting_balance)
        await self.refresh()

    async def sync_trade_history(self) -> tuple[int, int]:
        """Pull the wallet's own fill history from Hyperliquid. Returns (new, seen).

        Live only, and not because of a rule — a paper account has no wallet to read.

        Two calls, not one: a fill says what happened, and the order behind it says
        why. Without the second, every imported exit would be labelled a guess.
        """
        if self.session is None:
            raise RuntimeError("not connected")
        if not self.settings.is_live:
            raise RuntimeError("Paper mode has no wallet to sync from")
        address = self.settings.account_address
        if not address:
            raise RuntimeError("no wallet address is configured")

        raw = await self.session.info.user_fills(address)
        order_types = await self.session.info.order_types(address)
        imported, seen = import_exchange_fills(
            self.conn, TradingMode.LIVE, raw, order_types
        )
        log.info("synced %d new fill(s) from %d in the exchange history", imported, seen)
        return imported, seen

    async def fetch_chart_candles(self, timeframe: Timeframe, count: int) -> list[Candle]:
        """History for a chart view other than the bot's own timeframe.

        Display only. The strategy is never given these — it would then be deciding
        on a resolution it was never measured on.
        """
        if self.session is None:
            return []
        return await self.session.info.recent_candles(self.settings.coin, timeframe, count)

    async def preview(self, settings: AppSettings) -> Preview:
        """Size a hypothetical trade under `settings`, using live prices.

        The timeframe decides the ATR and so the stop distance, so a timeframe the
        engine is not running gets its own candles rather than a stale answer.
        """
        if self.session is None:
            return Preview()

        engine = self.session.engine
        if settings.timeframe is self.settings.timeframe:
            candles = engine.candles
        else:
            fetched = await self.fetch_chart_candles(settings.timeframe, 80)
            candles = fetched[:-1] if len(fetched) > 1 else fetched
        if not candles:
            return Preview()

        distance = engine.strategy.typical_stop_distance(candles)
        account = await self.session.broker.account_state()
        price = engine.last_mark or candles[-1].close
        result = Preview(
            price=price, equity=account.account_value, stop_distance=distance or 0.0
        )
        if not distance:
            return result

        # Sized exactly as the engine would size it, including the percentage form
        # and the clamp. A preview that ignored either would answer a question
        # nobody asked — the point of this card is "will *these* settings trade".
        requested = settings.risk_for(account.account_value)
        plan = plan_position(
            side=Side.LONG,
            entry_price=price,
            stop_price=price - distance,
            risk_usdc=requested,
            equity_usdc=account.account_value,
            leverage=settings.leverage,
            asset=self.session.asset,
            clamp_to_leverage=settings.clamp_size_to_leverage,
        )
        if isinstance(plan, Rejection):
            result.problem = plan.reason.value.replace("_", " ")
            if plan.reason is RejectReason.EXCEEDS_LEVERAGE_CAP:
                needed = minimum_leverage_for(
                    requested, distance, price, account.account_value
                )
                fits = requested * settings.leverage / needed
                result.hint = f"needs {needed}x, or risk of about {fits:,.2f} USDC"
            return result

        result.size = plan.size
        result.notional = plan.notional
        result.margin = plan.margin_required
        # Clamping silently halves the stake, so the card that exists to say what
        # these settings do has to say that too.
        if plan.risk_usdc < requested * 0.99:
            result.hint = (
                f"capped by {settings.leverage}x: risking {plan.risk_usdc:,.2f} USDC, "
                f"not the {requested:,.2f} asked for"
            )
        return result

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

    def _warn_if_live_refused(self) -> None:
        """Live falling back to Paper must be impossible to miss.

        Silently trading a simulated account while the user believes it is real
        would be the worst failure this app could have.
        """
        reason = self.session.fell_back_to_paper if self.session else None
        if reason:
            self._fail(f"Live mode refused - running in PAPER instead: {reason}")

    def _fail(self, message: str) -> None:
        self._error = message
        self.failed.emit(message)
