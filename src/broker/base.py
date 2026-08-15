"""What the engine may ask of an execution venue.

Paper and Live implement this same interface, and the strategy and engine never learn
which one they hold. That is the whole point: if Paper mode ran through a different
code path, a good paper result would say nothing about the live one.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

from ..core.models import (
    AccountState,
    Fill,
    FillReason,
    MarginMode,
    Position,
    TradingMode,
)
from ..core.sizing import PositionPlan

# Re-exported so callers can keep importing them from the broker package.
__all__ = ["Broker", "BrokerError", "Fill", "FillReason", "ManagedPosition", "now_ms"]


class BrokerError(RuntimeError):
    """The venue refused or could not complete the request."""


@dataclass(frozen=True)
class ManagedPosition:
    """An open position together with the exits the bot placed for it."""

    position: Position
    stop_price: float | None
    take_profit_price: float | None
    entry_time_ms: int


def now_ms() -> int:
    return int(time.time() * 1000)


class Broker(ABC):
    """A venue the engine can trade through."""

    @property
    @abstractmethod
    def mode(self) -> TradingMode:
        """Which mode this broker represents, for logs and the UI badge."""

    @property
    @abstractmethod
    def balance(self) -> float:
        """Realised cash, excluding an open position's unrealised profit.

        Declared here because it was not: only the paper broker had it, and both
        the dashboard refresh and the console's closing summary read it off
        whichever broker they were given. In Live that raised `AttributeError`
        once a second and the UI stopped updating entirely — the first thing to go
        wrong the first time live mode was ever switched on.
        """

    @abstractmethod
    async def account_state(self) -> AccountState:
        """Equity, margin, and open positions."""

    @abstractmethod
    async def managed_position(self) -> ManagedPosition | None:
        """The open position and its resting exits, or None when flat."""

    @abstractmethod
    async def set_leverage(self, leverage: int, margin_mode: MarginMode) -> None:
        """Apply the leverage setting before the first order."""

    @abstractmethod
    async def open_position(self, plan: PositionPlan, reference_price: float) -> Fill:
        """Enter, and arrange the stop and take profit.

        `reference_price` is the price to work from — the top of the book on the
        side being crossed when it is known, otherwise the mid. Both implementations
        need it: Live prices its IOC limit through the book from here, and Paper
        simulates its fill from here.
        """

    @abstractmethod
    async def close_position(
        self, reference_price: float, reason: FillReason = FillReason.MANUAL_CLOSE
    ) -> Fill | None:
        """Flatten now. Returns None when already flat."""

    @abstractmethod
    async def sync(self, mark_price: float) -> list[Fill]:
        """Reconcile with the venue and report fills since the last call.

        Called on every price update. Paper evaluates its own stop and target here;
        Live checks whether the exchange has triggered the resting orders.
        """
