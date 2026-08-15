"""The strategy contract and the registry behind the Settings dropdown.

A strategy is a pure function of closed candles: same candles in, same signal out.
No I/O, no clock, no broker — that is what makes a backtest trustworthy and what
lets Paper mode mean something.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any, ClassVar

from ..core.models import Candle, Signal


class Strategy(ABC):
    #: Stable identifier, persisted in settings. Renaming one breaks saved configs.
    name: ClassVar[str]
    #: Shown in the Settings dropdown.
    display_name: ClassVar[str]

    @property
    @abstractmethod
    def warmup_candles(self) -> int:
        """Closed candles to load before the first evaluation can be trusted."""

    @abstractmethod
    def evaluate(self, candles: Sequence[Candle]) -> Signal | None:
        """Decide on the LAST candle in `candles`, which must already be closed.

        Returns `None` when there is no trade — the common case by far.
        """

    def parameters(self) -> dict[str, Any]:
        """Current settings, for the log line written when the bot starts."""
        return {}

    def typical_stop_distance(self, candles: Sequence[Candle]) -> float | None:
        """Roughly how far this strategy's stop would sit from entry, right now.

        Used before the bot starts, to check the risk and leverage settings can
        actually produce a trade. Without it, settings that can never fit look
        exactly like a market that never signals: silence.

        Returns None when the strategy cannot say, in which case the check is
        skipped rather than guessed at.
        """
        return None


_REGISTRY: dict[str, type[Strategy]] = {}


def register(strategy: type[Strategy]) -> type[Strategy]:
    """Class decorator that adds a strategy to the registry."""
    if strategy.name in _REGISTRY:
        raise ValueError(f"a strategy named {strategy.name!r} is already registered")
    _REGISTRY[strategy.name] = strategy
    return strategy


def available() -> dict[str, type[Strategy]]:
    return dict(_REGISTRY)


def create(name: str, **params: Any) -> Strategy:
    if name not in _REGISTRY:
        raise KeyError(f"unknown strategy {name!r}; available: {sorted(_REGISTRY)}")
    return _REGISTRY[name](**params)
