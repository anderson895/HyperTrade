"""Position sizing — turns a risk budget into an order size, or an explicit refusal.

The two controls are routinely confused, so to be precise about what each one does:

  * **Risk per trade** decides the *size*, through the distance to the stop.
  * **Leverage** only caps how much *notional* the margin allows. It is a constraint,
    never a sizing input.

A trade that does not fit is REJECTED with a reason the user can read in the log.
It is never silently resized, and leverage is never raised to make it fit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

from .models import AssetMeta, MarginMode, Side
from .precision import floor_size, round_price


class RejectReason(Enum):
    INVALID_INPUT = "invalid_input"
    LEVERAGE_ABOVE_MAX = "leverage_above_max"
    STOP_ON_WRONG_SIDE = "stop_on_wrong_side"
    SIZE_ROUNDS_TO_ZERO = "size_rounds_to_zero"
    EXCEEDS_LEVERAGE_CAP = "exceeds_leverage_cap"
    STOP_BEYOND_LIQUIDATION = "stop_beyond_liquidation"


@dataclass(frozen=True)
class Rejection:
    """Why no order will be placed. A normal outcome, not an error."""

    ok: ClassVar[bool] = False
    reason: RejectReason
    detail: str

    def __str__(self) -> str:
        return f"{self.reason.value}: {self.detail}"


@dataclass(frozen=True)
class PositionPlan:
    """A fully specified entry, with its exits, ready to send to a broker."""

    ok: ClassVar[bool] = True
    side: Side
    size: float
    entry_price: float
    stop_price: float
    take_profit_price: float | None
    notional: float
    margin_required: float
    risk_usdc: float
    est_liquidation_price: float

    def __str__(self) -> str:
        tp = f"{self.take_profit_price:g}" if self.take_profit_price else "none"
        return (
            f"{self.side.value.upper()} {self.size:g} @ {self.entry_price:g} "
            f"(stop {self.stop_price:g}, tp {tp}, risk {self.risk_usdc:.2f} USDC, "
            f"notional {self.notional:.2f}, margin {self.margin_required:.2f}, "
            f"est. liq {self.est_liquidation_price:g})"
        )


def estimate_liquidation_price(
    entry_price: float,
    side: Side,
    leverage: int,
    asset: AssetMeta,
) -> float:
    """Approximate the isolated-margin liquidation price for a fresh position.

    A position is liquidated once its loss eats the initial margin down to the
    maintenance requirement, so the move it can absorb is
    ``1/leverage - maintenance_margin_fraction`` of the entry price.

    This is an *estimate* for pre-trade guarding only. The authoritative number is
    `liquidationPx` from `clearinghouseState` once the position exists. Under cross
    margin with a single position the real liquidation sits further away than this,
    so using the isolated figure keeps the guard on the conservative side.
    """
    absorbable = 1.0 / leverage - asset.maintenance_margin_fraction
    absorbable = max(absorbable, 0.0)
    return entry_price * (1.0 - side.sign * absorbable)


def minimum_leverage_for(
    risk_usdc: float, stop_distance: float, entry_price: float, equity_usdc: float
) -> int:
    """The lowest whole leverage at which this trade's notional fits the equity.

    Sizing comes from the stop distance, so a wide stop needs a large position, and
    that position may need more margin than the leverage allows. This says how much
    would be enough, instead of leaving the user to guess.
    """
    if stop_distance <= 0 or equity_usdc <= 0:
        return 1
    notional = (risk_usdc / stop_distance) * entry_price
    return max(1, math.ceil(notional / equity_usdc))


def plan_position(
    *,
    side: Side,
    entry_price: float,
    stop_price: float,
    risk_usdc: float,
    equity_usdc: float,
    leverage: int,
    asset: AssetMeta,
    take_profit_price: float | None = None,
    max_stop_to_liq_ratio: float = 0.75,
    clamp_to_leverage: bool = False,
) -> PositionPlan | Rejection:
    """Size a trade from the risk budget, or explain why it cannot be taken.

    `max_stop_to_liq_ratio` is how far toward liquidation the stop may sit. At the
    default 0.75 the stop must trigger well before the exchange closes the position
    for us — otherwise the stop is decorative and the real risk is the whole margin.

    `clamp_to_leverage` cuts the size down to what the leverage allows instead of
    refusing the trade. Off *here* and switched on by `AppSettings`, because the
    default belongs to the strategy being run and not to this function: a silently
    resized trade is not the trade the caller asked for, so the caller has to opt in
    and then say so. It exists because a tight stop makes the requested risk need
    more notional than the account can hold — a 0.18% stop and 3% risk needs 17x,
    whatever the balance. A strategy backtested that way measured the clamped size,
    not the requested one, which is why the shipped settings clamp: the
    specification sizes with `min(risk / stop_pct, equity * MAX_LEVERAGE)`.
    `PositionPlan.risk_usdc` always reports what is genuinely at stake.
    """
    if entry_price <= 0 or stop_price <= 0:
        return Rejection(
            RejectReason.INVALID_INPUT,
            f"prices must be positive (entry={entry_price}, stop={stop_price})",
        )
    if risk_usdc <= 0:
        return Rejection(RejectReason.INVALID_INPUT, f"risk must be positive, got {risk_usdc}")
    if equity_usdc <= 0:
        return Rejection(RejectReason.INVALID_INPUT, f"no equity available ({equity_usdc})")
    if leverage < 1:
        return Rejection(RejectReason.INVALID_INPUT, f"leverage must be >= 1, got {leverage}")
    if leverage > asset.max_leverage:
        return Rejection(
            RejectReason.LEVERAGE_ABOVE_MAX,
            f"{leverage}x exceeds the {asset.max_leverage}x maximum for {asset.name}",
        )

    # Round first, then measure: the exchange will trade the rounded prices, so the
    # risk is computed from those and not from the strategy's ideal numbers.
    entry = round_price(entry_price, asset.sz_decimals)
    stop = round_price(stop_price, asset.sz_decimals)
    take_profit = (
        round_price(take_profit_price, asset.sz_decimals)
        if take_profit_price is not None
        else None
    )

    # A long stops out below the entry, a short above it.
    if (stop - entry) * side.sign >= 0:
        return Rejection(
            RejectReason.STOP_ON_WRONG_SIDE,
            f"{side.value} stop at {stop:g} is not on the losing side of {entry:g}",
        )

    stop_distance = abs(entry - stop)
    size = floor_size(risk_usdc / stop_distance, asset.sz_decimals)
    if size <= 0:
        min_risk = stop_distance * 10**-asset.sz_decimals
        return Rejection(
            RejectReason.SIZE_ROUNDS_TO_ZERO,
            f"risk {risk_usdc:g} USDC over a {stop_distance:g} stop rounds to zero at "
            f"{asset.sz_decimals} decimals; needs at least {min_risk:.2f} USDC",
        )

    notional = size * entry
    max_notional = equity_usdc * leverage
    if notional > max_notional:
        if not clamp_to_leverage:
            return Rejection(
                RejectReason.EXCEEDS_LEVERAGE_CAP,
                f"needs {notional:.2f} USDC of notional but {leverage}x on "
                f"{equity_usdc:.2f} USDC equity allows only {max_notional:.2f}; "
                f"lower the risk per trade or the stop distance",
            )
        size = floor_size(max_notional / entry, asset.sz_decimals)
        if size <= 0:
            return Rejection(
                RejectReason.SIZE_ROUNDS_TO_ZERO,
                f"{leverage}x on {equity_usdc:.2f} USDC allows only "
                f"{max_notional:.2f} of notional, which is less than one "
                f"{10**-asset.sz_decimals:g} {asset.name} step at {entry:g}",
            )
        notional = size * entry

    liquidation = estimate_liquidation_price(entry, side, leverage, asset)
    liq_distance = abs(entry - liquidation)
    if stop_distance > liq_distance * max_stop_to_liq_ratio:
        return Rejection(
            RejectReason.STOP_BEYOND_LIQUIDATION,
            f"stop is {stop_distance:g} from entry but liquidation is only "
            f"{liq_distance:g} away at {leverage}x (est. {liquidation:g}); "
            f"the position would be liquidated before the stop triggers",
        )

    return PositionPlan(
        side=side,
        size=size,
        entry_price=entry,
        stop_price=stop,
        take_profit_price=take_profit,
        notional=notional,
        margin_required=notional / leverage,
        risk_usdc=size * stop_distance,
        est_liquidation_price=liquidation,
    )
