"""Paper trading — a simulated account driven by real Hyperliquid prices.

Fills are not free. Entries and market exits cross the spread by the configured
slippage and pay the taker fee, both times. A paper engine that filled everything at
the mid for nothing would show a profitable strategy that loses money live, which is
worse than having no paper mode at all.

Fill model:
  * **Entry** and **manual close** — market orders. They fill at the reference price
    moved against us by `slippage`, plus the taker fee.
  * **Stop loss** — a market trigger. It fills at the *current mark*, not at the stop
    price, so a gap through the stop costs what a gap really costs.
  * **Take profit** — a resting limit order. It fills at exactly the target price and
    gets no slippage benefit for overshooting.

Known limitation: liquidation is not simulated. Sizing already refuses any trade whose
stop sits past the estimated liquidation price, so in a continuous move the stop always
comes first. A gap past both would be modelled here as a larger-than-possible loss
rather than as a liquidation.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from ..core.models import (
    AccountState,
    AssetMeta,
    MarginMode,
    Position,
    Side,
    TradingMode,
)
from ..core.precision import round_price
from ..core.sizing import PositionPlan, estimate_liquidation_price
from ..store import record_fill
from .base import (
    Broker,
    BrokerError,
    Fill,
    FillReason,
    ManagedPosition,
    PendingEntry,
    now_ms,
)

log = logging.getLogger(__name__)

DEFAULT_FEE_BPS = 4.5  # Hyperliquid base-tier taker fee


@dataclass
class PaperState:
    """Everything the simulated account needs to survive a restart."""

    balance: float
    leverage: int = 1
    coin: str | None = None
    side: Side | None = None
    size: float = 0.0
    entry_price: float = 0.0
    stop_price: float = 0.0
    take_profit_price: float | None = None
    entry_time_ms: int = 0
    entry_fee: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.side is not None and self.size > 0

    def flatten(self) -> None:
        self.coin = None
        self.side = None
        self.size = 0.0
        self.entry_price = 0.0
        self.stop_price = 0.0
        self.take_profit_price = None
        self.entry_time_ms = 0
        self.entry_fee = 0.0


def load_paper_state(conn: sqlite3.Connection) -> PaperState | None:
    row = conn.execute("SELECT * FROM paper_state WHERE id = 1").fetchone()
    if row is None:
        return None
    return PaperState(
        balance=row["balance"],
        leverage=row["leverage"],
        coin=row["coin"],
        side=Side(row["side"]) if row["side"] else None,
        size=row["size"] or 0.0,
        entry_price=row["entry_price"] or 0.0,
        stop_price=row["stop_price"] or 0.0,
        take_profit_price=row["take_profit_price"],
        entry_time_ms=row["entry_time_ms"] or 0,
        entry_fee=row["entry_fee"] or 0.0,
    )


def save_paper_state(conn: sqlite3.Connection, state: PaperState) -> None:
    conn.execute(
        """
        INSERT INTO paper_state
            (id, balance, leverage, coin, side, size, entry_price, stop_price,
             take_profit_price, entry_time_ms, entry_fee)
        VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            balance = excluded.balance,
            leverage = excluded.leverage,
            coin = excluded.coin,
            side = excluded.side,
            size = excluded.size,
            entry_price = excluded.entry_price,
            stop_price = excluded.stop_price,
            take_profit_price = excluded.take_profit_price,
            entry_time_ms = excluded.entry_time_ms,
            entry_fee = excluded.entry_fee
        """,
        (
            state.balance,
            state.leverage,
            state.coin,
            state.side.value if state.side else None,
            state.size,
            state.entry_price,
            state.stop_price,
            state.take_profit_price,
            state.entry_time_ms,
            state.entry_fee,
        ),
    )
    conn.commit()


class PaperBroker(Broker):
    def __init__(
        self,
        coin: str,
        asset: AssetMeta,
        starting_balance: float,
        *,
        slippage: float = 0.01,
        fee_bps: float = DEFAULT_FEE_BPS,
        conn: sqlite3.Connection | None = None,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        if starting_balance <= 0:
            raise ValueError("paper starting balance must be positive")

        self.coin = coin
        self.asset = asset
        self.slippage = slippage
        self.fee_rate = fee_bps / 10_000.0
        self._conn = conn
        self._clock = clock
        self._last_mark = 0.0

        restored = load_paper_state(conn) if conn is not None else None
        if restored is not None:
            self._state = restored
            if restored.is_open:
                # Adopting the position rather than starting flat is what stops a
                # restart from losing track of a trade that is still running.
                log.info(
                    "resumed paper %s position: %g %s from %g (stop %g)",
                    restored.side.value, restored.size, restored.coin,
                    restored.entry_price, restored.stop_price,
                )
        else:
            self._state = PaperState(balance=starting_balance)
            self._persist()

        #: A resting maker entry. Deliberately not persisted: an order the exchange
        #: would have cancelled while the app was shut must not come back to life
        #: on restart, and in paper there is no exchange to ask.
        self._pending: PendingEntry | None = None

    # --- Broker ----------------------------------------------------------

    @property
    def mode(self) -> TradingMode:
        return TradingMode.PAPER

    @property
    def state(self) -> PaperState:
        return self._state

    @property
    def balance(self) -> float:
        """Realised cash. Excludes the open position's unrealised profit."""
        return self._state.balance

    async def set_leverage(self, leverage: int, margin_mode: MarginMode) -> None:
        if leverage > self.asset.max_leverage:
            raise BrokerError(
                f"{leverage}x exceeds the {self.asset.max_leverage}x maximum for {self.asset.name}"
            )
        self._state.leverage = leverage
        self._persist()

    async def account_state(self) -> AccountState:
        position = self._position()
        margin_used = position.margin_used if position else 0.0
        unrealised = position.unrealized_pnl if position else 0.0
        return AccountState(
            account_value=self._state.balance + unrealised,
            withdrawable=max(0.0, self._state.balance - margin_used),
            total_margin_used=margin_used,
            positions=(position,) if position else (),
        )

    async def managed_position(self) -> ManagedPosition | None:
        position = self._position()
        if position is None:
            return None
        return ManagedPosition(
            position=position,
            stop_price=self._state.stop_price,
            take_profit_price=self._state.take_profit_price,
            entry_time_ms=self._state.entry_time_ms,
        )

    def pending_entry(self) -> PendingEntry | None:
        return self._pending

    async def cancel_entry(self) -> bool:
        if self._pending is None:
            return False
        log.info("paper entry order cancelled at %g", self._pending.limit_price)
        self._pending = None
        return True

    async def open_position(
        self,
        plan: PositionPlan,
        reference_price: float,
        *,
        post_only: bool = False,
        expire_after_ms: int | None = None,
    ) -> Fill | None:
        if self._state.is_open:
            raise BrokerError(f"already holding a {self._state.side.value} position")
        if reference_price <= 0:
            raise BrokerError(f"invalid reference price {reference_price}")

        if post_only:
            if self._pending is not None:
                raise BrokerError("an entry order is already resting")
            now = self._clock()
            self._pending = PendingEntry(
                plan=plan,
                limit_price=plan.entry_price,
                placed_at_ms=now,
                expire_at_ms=now + (expire_after_ms or 0),
            )
            log.info(
                "PAPER entry resting: %s %g %s @ %g",
                plan.side.value, plan.size, self.coin, plan.entry_price,
            )
            return None

        price = self._market_price(reference_price, plan.side, is_entry=True)
        fee = price * plan.size * self.fee_rate

        state = self._state
        state.balance -= fee
        state.coin = self.coin
        state.side = plan.side
        state.size = plan.size
        state.entry_price = price
        state.stop_price = plan.stop_price
        state.take_profit_price = plan.take_profit_price
        state.entry_time_ms = self._clock()
        state.entry_fee = fee
        self._last_mark = price

        fill = Fill(
            time_ms=state.entry_time_ms,
            coin=self.coin,
            side=plan.side,
            size=plan.size,
            price=price,
            fee=fee,
            reason=FillReason.ENTRY,
        )
        self._commit(fill)
        log.info("PAPER %s", fill)
        return fill

    async def move_stop(self, new_stop: float) -> bool:
        """Nothing rests on an exchange here, so the stop is simply the new number.

        `sync` reads it on the next poll, which is the same order of events as live:
        the level changes first, and only a later price can trigger it.
        """
        if not self._state.is_open or new_stop <= 0:
            return False
        self._state.stop_price = new_stop
        self._persist()
        return True

    async def close_position(
        self, reference_price: float, reason: FillReason = FillReason.MANUAL_CLOSE
    ) -> Fill | None:
        if not self._state.is_open:
            return None
        price = self._market_price(reference_price, self._state.side, is_entry=False)
        return self._settle(price, reason)

    async def sync(self, mark_price: float) -> list[Fill]:
        if mark_price > 0:
            self._last_mark = mark_price

        entry = await self._settle_pending_entry(mark_price)
        state = self._state
        if not state.is_open or mark_price <= 0:
            return entry
        if entry:
            # Just entered on this very poll. The stop and target cannot also have
            # triggered against the same price without the entry being nonsense.
            return entry

        sign = state.side.sign

        # The stop is checked first. With a single mark price only one level can be
        # breached — a long's stop is below its entry and its target above — so the
        # order is not load-bearing here the way it is in the candle-based
        # backtester. It is kept the same so both agree by construction.
        if (mark_price - state.stop_price) * sign <= 0:
            # A market trigger fills at the current price, not at the stop, so a gap
            # through the level costs what a gap really costs.
            price = self._market_price(mark_price, state.side, is_entry=False)
            return [self._settle(price, FillReason.STOP_LOSS)]

        target = state.take_profit_price
        if target is not None and (mark_price - target) * sign >= 0:
            # A resting limit fills at its own price — no bonus for overshooting.
            return [self._settle(target, FillReason.TAKE_PROFIT)]

        return []

    async def _settle_pending_entry(self, mark_price: float) -> list[Fill]:
        """Fill or expire a resting entry.

        A maker order fills when the market comes to it — strictly through the
        limit, not merely touching it. Sitting at the touch is the back of the
        queue, and counting that as filled is the single easiest way to make a
        post-only backtest look better than the real thing.

        No fee is charged: Hyperliquid pays makers less than it charges takers, and
        modelling the rebate as zero keeps paper on the pessimistic side of live.
        """
        pending = self._pending
        if pending is None:
            return []

        now = self._clock()
        if mark_price > 0:
            filled = (
                mark_price < pending.limit_price
                if pending.plan.side.is_buy
                else mark_price > pending.limit_price
            )
            if filled:
                self._pending = None
                return [self._fill_entry(pending, pending.limit_price)]

        if pending.expired(now):
            self._pending = None
            log.info(
                "entry order expired unfilled at %g - the pullback never came",
                pending.limit_price,
            )
        return []

    def _fill_entry(self, pending: PendingEntry, price: float) -> Fill:
        plan = pending.plan
        state = self._state
        state.coin = self.coin
        state.side = plan.side
        state.size = plan.size
        state.entry_price = price
        state.stop_price = plan.stop_price
        state.take_profit_price = plan.take_profit_price
        state.entry_time_ms = self._clock()
        state.entry_fee = 0.0
        self._last_mark = price

        fill = Fill(
            time_ms=state.entry_time_ms,
            coin=self.coin,
            side=plan.side,
            size=plan.size,
            price=price,
            fee=0.0,
            reason=FillReason.ENTRY,
        )
        self._commit(fill)
        log.info("PAPER %s (maker)", fill)
        return fill

    # --- paper-only ------------------------------------------------------

    def reset(self, starting_balance: float) -> None:
        """Wipe the simulated account. Backs the 'Reset paper account' button."""
        if starting_balance <= 0:
            raise ValueError("paper starting balance must be positive")
        self._state = PaperState(balance=starting_balance)
        self._last_mark = 0.0
        self._persist()
        log.info("paper account reset to %.2f USDC", starting_balance)

    # --- internals -------------------------------------------------------

    def _market_price(self, reference: float, side: Side, *, is_entry: bool) -> float:
        """Cross the spread in whichever direction hurts.

        Entering a long buys, so it pays up; closing a long sells, so it takes less.
        """
        direction = side.sign if is_entry else -side.sign
        return round_price(reference * (1 + self.slippage * direction), self.asset.sz_decimals)

    def _settle(self, price: float, reason: FillReason) -> Fill:
        state = self._state
        gross = (price - state.entry_price) * state.size * state.side.sign
        fee = price * state.size * self.fee_rate
        # The entry fee belongs to this round trip too, so the reported result is
        # what the balance actually moved by across both legs.
        realised = gross - fee - state.entry_fee

        state.balance += gross - fee
        fill = Fill(
            time_ms=self._clock(),
            coin=self.coin,
            side=state.side,
            size=state.size,
            price=price,
            fee=fee,
            reason=reason,
            realised_pnl=realised,
        )
        state.flatten()
        self._commit(fill)
        log.info("PAPER %s | balance %.2f USDC", fill, state.balance)
        return fill

    def _position(self) -> Position | None:
        state = self._state
        if not state.is_open:
            return None
        mark = self._last_mark or state.entry_price
        notional = state.size * state.entry_price
        return Position(
            coin=state.coin,
            size=state.size * state.side.sign,
            entry_price=state.entry_price,
            liquidation_price=estimate_liquidation_price(
                state.entry_price, state.side, state.leverage, self.asset
            ),
            unrealized_pnl=(mark - state.entry_price) * state.size * state.side.sign,
            margin_used=notional / state.leverage,
            leverage=state.leverage,
        )

    def _persist(self) -> None:
        if self._conn is not None:
            save_paper_state(self._conn, self._state)

    def _commit(self, fill: Fill) -> None:
        self._persist()
        if self._conn is not None:
            record_fill(self._conn, TradingMode.PAPER, fill)
