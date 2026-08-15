"""The bot loop — prices in, decisions out.

Every tick does two things: let the broker settle any exit that has triggered, then,
if a new candle has closed, decide whether to enter. Entries are considered **only on
a closed candle**, never mid-candle, so what runs live is what the backtester
measured.

Data arrives by polling the REST info endpoint. A WebSocket feed is the eventual
plan, but the decision cadence here is one candle — five minutes at the very fastest —
so polling costs a couple of requests every few seconds and removes a reconnect state
machine from the critical path. Swapping it later does not change this file.
"""

from __future__ import annotations

import asyncio
import logging
import math
import sqlite3
import time
from datetime import datetime, timezone

from .broker.base import Broker, Fill
from .config import AppSettings
from .core.models import AssetMeta, Candle, Side, TradingMode
from .core.sizing import (
    PositionPlan,
    RejectReason,
    Rejection,
    minimum_leverage_for,
    plan_position,
)
from .data.hl_info import HyperliquidInfo
from .errors import TRADING_ERRORS
from .store import realised_since
from .strategy.base import Strategy

log = logging.getLogger(__name__)

DEFAULT_POLL_SECONDS = 2.0
#: Candles fetched per poll. Two would do; three tolerates a missed cycle.
CANDLE_PROBE = 3
#: Floor on how often the mid price is refetched, however often `poll` is called.
MARK_MIN_SECONDS = 1.0


def _utc_midnight_ms() -> int:
    now = datetime.now(timezone.utc)
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)


class BotEngine:
    """Drives one strategy against one broker on one timeframe."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        info: HyperliquidInfo,
        broker: Broker,
        strategy: Strategy,
        asset: AssetMeta,
        conn: sqlite3.Connection | None = None,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
        throttle_requests: bool = True,
    ) -> None:
        """`throttle_requests` floors how often `poll` refetches. On in the app, so a
        once-a-second UI refresh does not become sixty requests a minute; off in
        tests, so a poll always fetches."""
        self.settings = settings
        self.info = info
        self.broker = broker
        self.strategy = strategy
        self.asset = asset
        self.conn = conn
        self.poll_seconds = poll_seconds
        self.throttle_requests = throttle_requests

        self._candles: list[Candle] = []
        self._forming: Candle | None = None
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_mark = 0.0
        self._last_mark_at = 0.0
        self._last_candle_at = 0.0
        self._halted_for_the_day = False

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_mark(self) -> float:
        return self._last_mark

    @property
    def candles(self) -> list[Candle]:
        """Closed candles the strategy is working from. The UI charts these."""
        return list(self._candles)

    @property
    def forming_candle(self) -> Candle | None:
        """The candle still being built. For the chart only — never the strategy."""
        return self._forming

    # --- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        if self._running:
            return
        await self.prepare()
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="hypertrade-engine")

    async def stop(self) -> None:
        """Stop looking for entries.

        An open position is deliberately left alone, together with its stop and
        target. STOP means "stop trading", not "close my position at whatever the
        book happens to offer". Use `close_now` for that.
        """
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

        held = await self.broker.managed_position()
        if held is None:
            log.info("bot stopped")
        else:
            log.info(
                "bot stopped - the open %s position of %g %s is left in place, "
                "stop %g and target %s still stand. Use Close position to exit it.",
                held.position.side.value, held.position.abs_size, held.position.coin,
                held.stop_price,
                f"{held.take_profit_price:g}" if held.take_profit_price else "none",
            )

    async def close_now(self) -> Fill | None:
        """Flatten on the user's instruction — the separate, explicit action."""
        fill = await self.broker.close_position(self._last_mark or await self.info.mid_price(
            self.settings.coin
        ))
        if fill is not None:
            log.info("closed on request: %s", fill)
        return fill

    async def load_history(self) -> None:
        """Fill the candle buffer. Called at app start, before any bot is running.

        Separate from `prepare` so the chart has data from the moment the window
        opens rather than only after START.
        """
        timeframe = self.settings.timeframe
        needed = self.strategy.warmup_candles
        history = await self.info.recent_candles(self.settings.coin, timeframe, needed + 1)
        if len(history) < needed:
            raise RuntimeError(
                f"{self.settings.coin} has only {len(history)} {timeframe.value} candles "
                f"but {self.strategy.name} needs {needed}. Choose a faster timeframe."
            )

        # The final candle is still forming; the strategy only ever sees closed ones.
        self._candles = list(history[:-1])[-needed:]
        self._forming = history[-1]
        log.info(
            "%s %s | %s | %d candles loaded to %s",
            self.broker.mode.value.upper(), self.settings.coin, timeframe.label,
            len(self._candles),
            self._candles[-1].close_time.strftime("%Y-%m-%d %H:%M UTC"),
        )

    async def prepare(self) -> None:
        """Apply settings that must be in place before trading, and refresh history."""
        for note in self.settings.advisories():
            log.warning(note)

        await self.broker.set_leverage(self.settings.leverage, self.settings.margin_mode)
        await self.load_history()
        log.info(
            "risk %g USDC | %dx %s | strategy %s %s",
            self.settings.risk_usdc, self.settings.leverage,
            self.settings.margin_mode.value, self.strategy.name,
            self.strategy.parameters(),
        )
        await self.check_settings_can_trade()

    async def check_settings_can_trade(self) -> str | None:
        """Warn if the risk, leverage and timeframe can never produce a trade.

        Sizing comes from the stop distance, so a wide stop needs a large position,
        and that position may need more margin than the leverage allows. Every entry
        is then rejected — correctly, but a bot that rejects everything looks exactly
        like a market that never signals. This says so up front, and says what to
        change. Returns the warning, or None when a trade would fit.
        """
        distance = self.strategy.typical_stop_distance(self._candles)
        if not distance or not self._candles:
            return None

        entry = self._candles[-1].close
        account = await self.broker.account_state()
        plan = plan_position(
            side=Side.LONG,
            entry_price=entry,
            stop_price=entry - distance,
            risk_usdc=self.settings.risk_usdc,
            equity_usdc=account.account_value,
            leverage=self.settings.leverage,
            asset=self.asset,
        )
        if not isinstance(plan, Rejection):
            return None

        message = (
            f"these settings cannot trade right now: {plan}. "
            f"Stop distance is about {distance:,.0f} on {self.settings.timeframe.label}"
        )
        if plan.reason is RejectReason.EXCEEDS_LEVERAGE_CAP and account.account_value > 0:
            needed = minimum_leverage_for(
                self.settings.risk_usdc, distance, entry, account.account_value
            )
            message += (
                f" - raise leverage to {needed}x, or lower the risk to about "
                f"{self.settings.risk_usdc * self.settings.leverage / needed:,.2f} USDC"
            )
        log.warning("%s", message)
        return message

    # --- the loop --------------------------------------------------------

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except TRADING_ERRORS as exc:
                # A dropped connection is expected and usually gone by the next poll.
                log.warning("poll failed, retrying: %s", exc)
            except Exception:
                # Anything else is a bug. The loop still survives — a running bot
                # holding a position must not silently stop watching it — but this
                # is logged with a traceback and at ERROR, so it reaches the alert
                # banner instead of looking like a network blip.
                log.exception("unexpected error in the bot loop")
            await asyncio.sleep(self.poll_seconds)

    async def poll(self) -> bool:
        """Refresh the mark, let the broker settle any triggered exit, ingest candles.

        Safe to call whether or not the bot is running, and the UI calls it while
        stopped for two reasons. The chart needs a moving price, and — more
        importantly — a stopped bot may still be holding a position. STOP promises
        the stop and target still stand, and in Paper mode this call is what
        evaluates them. Skip it and the promise is a lie.

        Both fetches are rate-limited here, so an extra caller costs nothing.
        Returns True when a new candle has closed.
        """
        now = time.monotonic()

        if not self._last_mark or now - self._last_mark_at >= self._mark_min_seconds:
            self._last_mark = await self.info.mid_price(self.settings.coin)
            self._last_mark_at = now

        # Settled on every poll, not only when the price was refetched. The exit
        # check is local and cheap, and tying it to the fetch throttle would leave a
        # triggered stop sitting unsettled for as long as the throttle lasts.
        if self._last_mark:
            for fill in await self.broker.sync(self._last_mark):
                log.info("%s", fill)

        if now - self._last_candle_at >= self._candle_check_seconds:
            self._last_candle_at = now
            return await self._ingest_closed_candles()
        return False

    async def tick(self) -> None:
        if await self.poll():
            await self._on_candle_close()

    @property
    def _mark_min_seconds(self) -> float:
        return MARK_MIN_SECONDS if self.throttle_requests else 0.0

    @property
    def _candle_check_seconds(self) -> float:
        """Check for a closed candle often enough to act promptly, rarely enough
        not to hammer the endpoint: a twentieth of the timeframe, within bounds."""
        if not self.throttle_requests:
            return 0.0
        return min(30.0, max(5.0, self.settings.timeframe.seconds / 20))

    async def _ingest_closed_candles(self) -> bool:
        recent = await self.info.recent_candles(
            self.settings.coin, self.settings.timeframe, CANDLE_PROBE
        )
        if len(recent) < 2:
            return False

        # The last one is still forming. The strategy must never see it, but the
        # chart should — it is the candle the live price is building.
        self._forming = recent[-1]

        latest_seen = self._candles[-1].open_time_ms if self._candles else 0
        fresh = [c for c in recent[:-1] if c.open_time_ms > latest_seen]
        if not fresh:
            return False

        self._candles.extend(fresh)
        del self._candles[: -self.strategy.warmup_candles]
        return True

    async def _on_candle_close(self) -> None:
        candle = self._candles[-1]
        log.info(
            "%s candle closed at %g (%s)",
            self.settings.timeframe.label, candle.close,
            candle.close_time.strftime("%Y-%m-%d %H:%M UTC"),
        )

        blocker = await self._reason_not_to_trade()
        if blocker:
            log.info("no entry: %s", blocker)
            return

        signal = self.strategy.evaluate(self._candles)
        if signal is None:
            return
        log.info("signal - %s: %s", signal.side.value.upper(), signal.reason)

        account = await self.broker.account_state()
        plan = plan_position(
            side=signal.side,
            entry_price=signal.entry_price,
            stop_price=signal.stop_price,
            take_profit_price=signal.take_profit_price,
            risk_usdc=self.settings.risk_usdc,
            equity_usdc=account.account_value,
            leverage=self.settings.leverage,
            asset=self.asset,
        )
        if isinstance(plan, Rejection):
            # Never silently resized and never forced through — the user is told why.
            log.warning("trade rejected - %s", plan)
            return

        assert isinstance(plan, PositionPlan)
        log.info("entering: %s", plan)
        await self.broker.open_position(plan, self._last_mark or plan.entry_price)

    async def _reason_not_to_trade(self) -> str | None:
        if await self.broker.managed_position() is not None:
            return "already holding a position"
        if self.settings.economic_data_day_block:
            return "news blackout is on — economic data day"
        return self._daily_loss_blocker()

    def _daily_loss_blocker(self) -> str | None:
        limit = self.settings.daily_loss_limit_usdc
        if limit <= 0 or self.conn is None:
            return None

        realised = realised_since(self.conn, self.broker.mode, _utc_midnight_ms())
        if realised > -limit:
            self._halted_for_the_day = False
            return None

        if not self._halted_for_the_day:
            self._halted_for_the_day = True
            log.warning(
                "daily loss limit reached: %.2f USDC lost today against a %.2f limit. "
                "No more entries until 00:00 UTC.",
                -realised, limit,
            )
        return f"daily loss limit reached ({-realised:.2f} of {limit:.2f} USDC)"
