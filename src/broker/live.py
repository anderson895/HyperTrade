"""Live execution on Hyperliquid — real orders, real USDC.

Reads and writes are deliberately split. Account state, fills and resting orders are
read through the project's own async httpx client, because they are polled every
second and blocking the Qt loop for each one would freeze the window. Orders and
leverage go through the official SDK, which signs them; that signing is not something
to reimplement, so those calls are handed to a worker thread instead.

Order shapes, and why:

* **Entry** — an IOC limit priced through the book by the slippage allowance.
  Hyperliquid has no market order type; this is what one is.
* **Stop** — a reduce-only *market* trigger. Getting out matters more than the price.
* **Target** — a reduce-only *limit* trigger at exactly the target. This mirrors what
  the paper broker and the backtester assume, so live results stay comparable with
  the numbers the strategy was judged on.
* Both exits are sent together under `positionTpsl` grouping, which ties them to the
  position: when one fills or the position closes, the exchange cancels the other.
  Sent separately, a leftover trigger could later open a brand-new position.

The exchange owns the truth. This class caches what it opened only so it can tell a
stop from a target when a fill appears; everything the UI shows is read back.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass

from ..core.models import (
    AccountState,
    AssetMeta,
    Fill,
    FillReason,
    MarginMode,
    Position,
    Side,
    TradingMode,
)
from ..core.precision import round_price, round_size, slippage_price
from ..core.sizing import PositionPlan
from ..data.hl_info import HyperliquidInfo
from ..store import record_fill
from .base import Broker, BrokerError, ManagedPosition, PendingEntry, now_ms

log = logging.getLogger(__name__)

#: How far a stop's market trigger may cross the book once it fires.
STOP_SLIPPAGE = 0.02


@dataclass
class _Bracket:
    """The exits placed for a position, so a later fill can be attributed."""

    side: Side
    entry_price: float
    stop_price: float
    take_profit_price: float | None
    entry_time_ms: int
    stop_oid: int | None = None
    take_profit_oid: int | None = None


def _resting_price(order: dict) -> float:
    return float(order.get("triggerPx") or order.get("limitPx") or 0)


class LiveBroker(Broker):
    def __init__(
        self,
        coin: str,
        asset: AssetMeta,
        *,
        exchange,
        info: HyperliquidInfo,
        account_address: str,
        slippage: float = 0.01,
        conn: sqlite3.Connection | None = None,
        clock: Callable[[], int] = now_ms,
    ) -> None:
        self.coin = coin
        self.asset = asset
        self.account_address = account_address
        self.slippage = slippage
        self._exchange = exchange
        self._info = info
        self._conn = conn
        self._clock = clock
        self._bracket: _Bracket | None = None
        self._seen_flat = True
        self._balance = 0.0
        #: A maker entry resting on the book, waiting for price to come back.
        self._pending: PendingEntry | None = None

    # --- Broker ----------------------------------------------------------

    @property
    def mode(self) -> TradingMode:
        return TradingMode.LIVE

    @property
    def balance(self) -> float:
        """Realised cash as of the last `account_state()` call.

        Synchronous, to match the paper broker — but paper reads local state and
        this has to ask the exchange, so the figure is cached from the last time it
        did. Every caller reads it immediately after `account_state()`: the UI once
        a second, the console once at shutdown.
        """
        return self._balance

    async def account_state(self) -> AccountState:
        state = await self._info.clearinghouse_state(self.account_address)
        # Equity less what the open position has not yet made or lost, which is what
        # PaperBroker.balance means. Hyperliquid reports account value including
        # unrealised profit, so it is taken back out here rather than the two modes
        # quietly reporting different things under the same name.
        self._balance = state.account_value - sum(
            position.unrealized_pnl for position in state.positions
        )
        return state

    async def managed_position(self) -> ManagedPosition | None:
        position = await self._position()
        if position is None:
            return None

        stop, target = self._bracket_levels()
        if stop is None and target is None:
            # Nothing cached — a restart, most likely. The exchange still holds the
            # resting exits, so read them back rather than assuming there are none.
            stop, target = await self._read_resting_exits(position.side)

        return ManagedPosition(
            position=position,
            stop_price=stop,
            take_profit_price=target,
            entry_time_ms=self._bracket.entry_time_ms if self._bracket else 0,
        )

    async def set_leverage(self, leverage: int, margin_mode: MarginMode) -> None:
        if leverage > self.asset.max_leverage:
            raise BrokerError(
                f"{leverage}x exceeds the {self.asset.max_leverage}x maximum for {self.asset.name}"
            )
        result = await asyncio.to_thread(
            self._exchange.update_leverage, leverage, self.coin, margin_mode.is_cross
        )
        self._require_ok(result, f"set leverage to {leverage}x")
        log.info("leverage set to %dx %s", leverage, margin_mode.value)

    def pending_entry(self) -> PendingEntry | None:
        return self._pending

    async def cancel_entry(self) -> bool:
        if self._pending is None:
            return False
        pending, self._pending = self._pending, None
        if pending.oid is None:
            return True
        try:
            await asyncio.to_thread(self._exchange.cancel, self.coin, pending.oid)
            log.info("entry order %s cancelled at %g", pending.oid, pending.limit_price)
        except Exception as exc:  # noqa: BLE001 — reported, not fatal
            # It may have filled in the meantime, in which case `sync` will find the
            # position and attach exits to it. Either way the bot must not keep
            # treating the order as still resting.
            log.warning("could not cancel entry order %s: %s", pending.oid, exc)
        return True

    async def open_position(
        self,
        plan: PositionPlan,
        reference_price: float,
        *,
        post_only: bool = False,
        expire_after_ms: int | None = None,
    ) -> Fill | None:
        if await self._position() is not None:
            raise BrokerError("already holding a position on the exchange")
        if reference_price <= 0:
            raise BrokerError(f"invalid reference price {reference_price}")

        if post_only:
            return await self._rest_entry(plan, expire_after_ms)

        limit_price = slippage_price(
            reference_price, plan.side.is_buy, self.slippage, self.asset.sz_decimals
        )
        result = await asyncio.to_thread(
            self._exchange.order,
            self.coin,
            plan.side.is_buy,
            plan.size,
            limit_price,
            {"limit": {"tif": "Ioc"}},
        )
        filled = self._filled(result, f"enter {plan.side.value} {plan.size:g} {self.coin}")
        # The order response carries price and size but no fee; that only appears in
        # the fill record. Left unread, every entry would be logged as free.
        filled["fee"] = await self._fee_for(filled["oid"])

        self._bracket = _Bracket(
            side=plan.side,
            entry_price=filled["price"],
            stop_price=plan.stop_price,
            take_profit_price=plan.take_profit_price,
            entry_time_ms=self._clock(),
        )
        self._seen_flat = False

        fill = Fill(
            time_ms=self._bracket.entry_time_ms,
            coin=self.coin,
            side=plan.side,
            size=filled["size"],
            price=filled["price"],
            fee=filled["fee"],
            reason=FillReason.ENTRY,
        )
        self._commit(fill)
        log.info("LIVE %s", fill)

        await self._place_exits(plan, filled["size"])
        return fill

    async def _settle_pending_entry(self, position: Position | None) -> list[Fill]:
        """Has the resting entry filled, or should it be cancelled?

        The exchange is the authority on both. A position appearing while an entry
        rests is that entry having filled — nothing else places one.
        """
        pending = self._pending
        if pending is None:
            return []

        if position is not None:
            self._pending = None
            self._bracket = _Bracket(
                side=pending.plan.side,
                entry_price=position.entry_price,
                stop_price=pending.plan.stop_price,
                take_profit_price=pending.plan.take_profit_price,
                entry_time_ms=self._clock(),
            )
            self._seen_flat = False
            fill = Fill(
                time_ms=self._bracket.entry_time_ms,
                coin=self.coin,
                side=pending.plan.side,
                size=position.abs_size,
                price=position.entry_price,
                fee=0.0,  # the maker fee lands in userFills, not the position
                reason=FillReason.ENTRY,
            )
            self._commit(fill)
            log.info("LIVE %s (maker)", fill)
            # Only now, with a position to reduce: exits placed against a resting
            # order would be reduce-only against nothing.
            await self._place_exits(pending.plan, position.abs_size)
            return [fill]

        if pending.expired(self._clock()):
            log.info(
                "entry order expired unfilled at %g - the pullback never came",
                pending.limit_price,
            )
            await self.cancel_entry()
        return []

    async def _rest_entry(
        self, plan: PositionPlan, expire_after_ms: int | None
    ) -> None:
        """Place an ALO limit that the exchange will only accept as a maker.

        Post-only means the exchange rejects the order outright rather than let it
        cross the spread. That rejection is the correct outcome, not an error: it
        means price has already moved through the level and the pullback this trade
        was waiting for is not going to happen from here.
        """
        if self._pending is not None:
            raise BrokerError("an entry order is already resting")

        limit = round_price(plan.entry_price, self.asset.sz_decimals)
        result = await asyncio.to_thread(
            self._exchange.order,
            self.coin,
            plan.side.is_buy,
            round_size(plan.size, self.asset.sz_decimals),
            limit,
            {"limit": {"tif": "Alo"}},
        )
        statuses = self._statuses(result, f"rest {plan.side.value} entry at {limit:g}")
        status = statuses[0] if statuses else {}

        if isinstance(status, dict) and "resting" in status:
            now = self._clock()
            self._pending = PendingEntry(
                plan=plan,
                limit_price=limit,
                placed_at_ms=now,
                expire_at_ms=now + (expire_after_ms or 0),
                oid=status["resting"].get("oid"),
            )
            log.info(
                "LIVE entry resting: %s %g %s @ %g (oid %s)",
                plan.side.value, plan.size, self.coin, limit, self._pending.oid,
            )
            return None

        if isinstance(status, dict) and "filled" in status:
            # An ALO that fills immediately should be impossible, but the venue is
            # the authority on that. Treat it as a normal entry rather than losing
            # track of a position that now exists.
            log.warning("post-only entry filled immediately at %g", limit)
            return await self._adopt_immediate_fill(plan, status["filled"])

        log.info(
            "post-only entry refused at %g (%s) - price has already left the level",
            limit, status.get("error", status) if isinstance(status, dict) else status,
        )
        return None

    async def _adopt_immediate_fill(self, plan: PositionPlan, filled: dict) -> Fill:
        price = float(filled["avgPx"])
        size = float(filled["totalSz"])
        self._bracket = _Bracket(
            side=plan.side,
            entry_price=price,
            stop_price=plan.stop_price,
            take_profit_price=plan.take_profit_price,
            entry_time_ms=self._clock(),
        )
        self._seen_flat = False
        fill = Fill(
            time_ms=self._bracket.entry_time_ms,
            coin=self.coin,
            side=plan.side,
            size=size,
            price=price,
            fee=await self._fee_for(filled.get("oid")),
            reason=FillReason.ENTRY,
        )
        self._commit(fill)
        log.info("LIVE %s", fill)
        await self._place_exits(plan, size)
        return fill

    async def close_position(
        self, reference_price: float, reason: FillReason = FillReason.MANUAL_CLOSE
    ) -> Fill | None:
        position = await self._position()
        if position is None:
            return None

        limit_price = slippage_price(
            reference_price,
            not position.side.is_buy,  # closing a long means selling
            self.slippage,
            self.asset.sz_decimals,
        )
        result = await asyncio.to_thread(
            self._exchange.order,
            self.coin,
            not position.side.is_buy,
            round_size(position.abs_size, self.asset.sz_decimals),
            limit_price,
            {"limit": {"tif": "Ioc"}},
            True,  # reduce_only
        )
        filled = self._filled(result, f"close {position.side.value} {self.coin}")
        filled["fee"] = await self._fee_for(filled["oid"])

        fill = self._settle(
            price=filled["price"],
            size=filled["size"],
            fee=filled["fee"],
            side=position.side,
            entry_price=position.entry_price,
            reason=reason,
        )
        self._bracket = None
        self._seen_flat = True
        return fill

    async def sync(self, mark_price: float) -> list[Fill]:
        """Notice that the exchange closed the position on its own.

        Stops and targets rest on the exchange and fire without this process being
        involved — possibly while the app was shut. The only way to learn what
        happened, and at what price, is to see the position gone and read the fill.
        """
        position = await self._position()

        entry = await self._settle_pending_entry(position)
        if entry:
            return entry

        if position is not None:
            self._seen_flat = False
            return []
        if self._seen_flat:
            return []

        self._seen_flat = True
        bracket, self._bracket = self._bracket, None
        if bracket is None:
            return []

        closing = await self._closing_fill(bracket)
        if closing is None:
            log.warning(
                "position closed on the exchange but no matching fill was found; "
                "check the Hyperliquid UI for what happened"
            )
            return []
        return [closing]

    async def move_stop(self, new_stop: float) -> bool:
        """Replace the resting stop with one at `new_stop`.

        Placed **before** the old one is cancelled. For the moment between, the
        position carries two stops, which costs nothing — whichever triggers first
        closes it and the other is reduce-only against a flat account. Cancelling
        first would leave a window with none at all, and that window is exactly when
        a fast move would arrive.

        A failed cancel is therefore a warning, not an error: the new stop is
        already live, and the stale one sits further away doing no harm.
        """
        position = await self._position()
        if position is None or self._bracket is None or new_stop <= 0:
            return False

        trigger = round_price(new_stop, self.asset.sz_decimals)
        exit_is_buy = not self._bracket.side.is_buy
        size = round_size(position.abs_size, self.asset.sz_decimals)

        result = await asyncio.to_thread(
            self._exchange.order,
            self.coin,
            exit_is_buy,
            size,
            slippage_price(trigger, exit_is_buy, STOP_SLIPPAGE, self.asset.sz_decimals),
            {"trigger": {"triggerPx": trigger, "isMarket": True, "tpsl": "sl"}},
            True,  # reduce_only
        )
        statuses = self._statuses(result, f"move the stop to {trigger:g}")
        oid = next(
            (s.get("resting", {}).get("oid") for s in statuses
             if isinstance(s, dict) and "resting" in s),
            None,
        )

        stale_oid = self._bracket.stop_oid
        self._bracket.stop_price = trigger
        self._bracket.stop_oid = oid

        if stale_oid is not None:
            try:
                await asyncio.to_thread(self._exchange.cancel, self.coin, stale_oid)
            except Exception as exc:  # noqa: BLE001 — the new stop is already live
                log.warning(
                    "could not cancel the previous stop %s (%s); it rests further "
                    "away and will not fire first", stale_oid, exc,
                )

        log.info("trailing stop moved to %g", trigger)
        return True

    # --- placing the exits ------------------------------------------------

    async def _place_exits(self, plan: PositionPlan, size: float) -> None:
        """Attach the stop and target to the position.

        A failure here is loud: the position is open and unprotected, and the user
        has to decide whether to close it by hand.
        """
        exit_is_buy = not plan.side.is_buy
        orders = [
            {
                "coin": self.coin,
                "is_buy": exit_is_buy,
                "sz": size,
                # A market trigger still carries a limit price as its worst
                # acceptable fill; priced through the stop so it gets out.
                "limit_px": slippage_price(
                    plan.stop_price, exit_is_buy, STOP_SLIPPAGE, self.asset.sz_decimals
                ),
                "order_type": {
                    "trigger": {
                        "triggerPx": round_price(plan.stop_price, self.asset.sz_decimals),
                        "isMarket": True,
                        "tpsl": "sl",
                    }
                },
                "reduce_only": True,
            }
        ]
        if plan.take_profit_price:
            target = round_price(plan.take_profit_price, self.asset.sz_decimals)
            orders.append(
                {
                    "coin": self.coin,
                    "is_buy": exit_is_buy,
                    "sz": size,
                    "limit_px": target,
                    "order_type": {
                        "trigger": {"triggerPx": target, "isMarket": False, "tpsl": "tp"}
                    },
                    "reduce_only": True,
                }
            )

        try:
            result = await asyncio.to_thread(
                self._exchange.bulk_orders, orders, None, "positionTpsl"
            )
            self._record_exit_oids(result)
        except Exception as exc:  # noqa: BLE001 — re-raised with what it means
            raise BrokerError(
                f"POSITION IS OPEN AND UNPROTECTED: the stop and target could not be "
                f"placed ({exc}). Close it by hand on Hyperliquid, or use Close position."
            ) from exc
        log.info(
            "exits placed: stop %g, target %s",
            plan.stop_price,
            f"{plan.take_profit_price:g}" if plan.take_profit_price else "none",
        )

    def _record_exit_oids(self, result: dict) -> None:
        """Remember which order is which, so a fill can be named stop or target."""
        statuses = self._statuses(result, "place the stop and target")
        if self._bracket is None:
            return
        oids = [
            status.get("resting", {}).get("oid")
            for status in statuses
            if isinstance(status, dict) and "resting" in status
        ]
        if oids:
            self._bracket.stop_oid = oids[0]
        if len(oids) > 1:
            self._bracket.take_profit_oid = oids[1]

    # --- reading back -----------------------------------------------------

    async def _position(self) -> Position | None:
        state = await self._info.clearinghouse_state(self.account_address)
        return state.position_for(self.coin)

    def _bracket_levels(self) -> tuple[float | None, float | None]:
        if self._bracket is None:
            return None, None
        return self._bracket.stop_price, self._bracket.take_profit_price

    async def _read_resting_exits(self, side: Side) -> tuple[float | None, float | None]:
        """Recover the stop and target from the exchange's resting trigger orders."""
        orders = [
            order
            for order in await self._info.open_orders(self.account_address)
            if order.get("coin") == self.coin and order.get("isTrigger")
        ]
        if not orders:
            return None, None

        prices = sorted(_resting_price(order) for order in orders)
        # A long's stop sits below its target; a short's above.
        if side is Side.LONG:
            return prices[0], prices[-1] if len(prices) > 1 else None
        return prices[-1], prices[0] if len(prices) > 1 else None

    async def _closing_fill(self, bracket: _Bracket) -> Fill | None:
        """Find the fill that closed the position and name it."""
        fills = [
            entry
            for entry in await self._info.user_fills(self.account_address)
            if entry.get("coin") == self.coin
            and int(entry.get("time", 0)) >= bracket.entry_time_ms
            and str(entry.get("dir", "")).startswith("Close")
        ]
        if not fills:
            return None

        price = 0.0
        size = 0.0
        fee = 0.0
        realised = 0.0
        oids = set()
        for entry in fills:
            quantity = abs(float(entry.get("sz", 0) or 0))
            price += float(entry.get("px", 0) or 0) * quantity
            size += quantity
            fee += float(entry.get("fee", 0) or 0)
            realised += float(entry.get("closedPnl", 0) or 0)
            oids.add(entry.get("oid"))
        if size <= 0:
            return None

        if bracket.stop_oid in oids:
            reason = FillReason.STOP_LOSS
        elif bracket.take_profit_oid in oids:
            reason = FillReason.TAKE_PROFIT
        else:
            # Closed by something this bot did not place: a manual exit on the
            # exchange, or a liquidation. Named for what is known, not guessed.
            reason = FillReason.MANUAL_CLOSE

        fill = Fill(
            time_ms=max(int(entry.get("time", 0)) for entry in fills),
            coin=self.coin,
            side=bracket.side,
            size=size,
            price=price / size,
            fee=fee,
            reason=reason,
            realised_pnl=realised - fee,
        )
        self._commit(fill)
        log.info("LIVE %s", fill)
        return fill

    # --- helpers ----------------------------------------------------------

    def _settle(
        self,
        *,
        price: float,
        size: float,
        fee: float,
        side: Side,
        entry_price: float,
        reason: FillReason,
    ) -> Fill:
        gross = (price - entry_price) * size * side.sign
        fill = Fill(
            time_ms=self._clock(),
            coin=self.coin,
            side=side,
            size=size,
            price=price,
            fee=fee,
            reason=reason,
            realised_pnl=gross - fee,
        )
        self._commit(fill)
        log.info("LIVE %s", fill)
        return fill

    def _statuses(self, result: object, what: str) -> list[dict]:
        """Unwrap the SDK's nested response, raising on anything but success."""
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise BrokerError(f"could not {what}: {result}")
        data = result.get("response", {}).get("data", {})
        statuses = data.get("statuses", []) if isinstance(data, dict) else []
        for status in statuses:
            if isinstance(status, dict) and "error" in status:
                raise BrokerError(f"could not {what}: {status['error']}")
        return statuses

    def _require_ok(self, result: object, what: str) -> None:
        if not isinstance(result, dict) or result.get("status") != "ok":
            raise BrokerError(f"could not {what}: {result}")

    def _filled(self, result: object, what: str) -> dict:
        """Pull the executed price, size and fee out of an order response.

        An IOC order that crossed nothing comes back resting or cancelled rather
        than filled, and that has to be an error: the caller is about to record a
        position that does not exist.
        """
        statuses = self._statuses(result, what)
        size = 0.0
        notional = 0.0
        oid = None
        for status in statuses:
            filled = status.get("filled") if isinstance(status, dict) else None
            if not filled:
                continue
            quantity = abs(float(filled.get("totalSz", 0) or 0))
            size += quantity
            notional += float(filled.get("avgPx", 0) or 0) * quantity
            oid = filled.get("oid", oid)

        if size <= 0:
            raise BrokerError(f"could not {what}: the order did not fill ({statuses})")
        return {"size": size, "price": notional / size, "fee": 0.0, "oid": oid}

    async def _fee_for(self, oid: int | None) -> float:
        """The fee actually charged, matched back by order id.

        Returns 0 when the fill has not appeared yet rather than blocking; the cost
        is then understated for that one trade, which is logged.
        """
        if oid is None:
            return 0.0
        for entry in await self._info.user_fills(self.account_address):
            if entry.get("oid") == oid:
                return float(entry.get("fee", 0) or 0)
        log.debug("no fill record yet for order %s; fee recorded as 0", oid)
        return 0.0

    def _commit(self, fill: Fill) -> None:
        if self._conn is not None:
            record_fill(self._conn, TradingMode.LIVE, fill)
