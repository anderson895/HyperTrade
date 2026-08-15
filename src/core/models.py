"""Domain types shared across HyperTrade."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum


class Side(Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        """+1 for a long, -1 for a short, so price math can avoid branching."""
        return 1 if self is Side.LONG else -1

    @property
    def is_buy(self) -> bool:
        return self is Side.LONG

    @property
    def opposite(self) -> Side:
        return Side.SHORT if self is Side.LONG else Side.LONG


class Timeframe(Enum):
    """The seven timeframes offered in the UI.

    The value is the Hyperliquid candle interval string. Every app timeframe maps
    1:1 onto a native interval, so candles are never aggregated client-side.
    """

    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"
    W1 = "1w"

    @property
    def seconds(self) -> int:
        return _TIMEFRAME_SECONDS[self]

    @property
    def label(self) -> str:
        return _TIMEFRAME_LABELS[self]

    @classmethod
    def from_interval(cls, interval: str) -> Timeframe:
        return cls(interval)


_TIMEFRAME_SECONDS: dict[Timeframe, int] = {
    Timeframe.M5: 5 * 60,
    Timeframe.M15: 15 * 60,
    Timeframe.M30: 30 * 60,
    Timeframe.H1: 60 * 60,
    Timeframe.H4: 4 * 60 * 60,
    Timeframe.D1: 24 * 60 * 60,
    Timeframe.W1: 7 * 24 * 60 * 60,
}

_TIMEFRAME_LABELS: dict[Timeframe, str] = {
    Timeframe.M5: "5 mins",
    Timeframe.M15: "15 mins",
    Timeframe.M30: "30 mins",
    Timeframe.H1: "1 hour",
    Timeframe.H4: "4 hours",
    Timeframe.D1: "Daily",
    Timeframe.W1: "Weekly",
}


class TradingMode(Enum):
    PAPER = "paper"
    LIVE = "live"


class Network(Enum):
    MAINNET = "mainnet"
    TESTNET = "testnet"

    @property
    def api_url(self) -> str:
        return (
            "https://api.hyperliquid.xyz"
            if self is Network.MAINNET
            else "https://api.hyperliquid-testnet.xyz"
        )

    @property
    def ws_url(self) -> str:
        return (
            "wss://api.hyperliquid.xyz/ws"
            if self is Network.MAINNET
            else "wss://api.hyperliquid-testnet.xyz/ws"
        )


class MarginMode(Enum):
    CROSS = "cross"
    ISOLATED = "isolated"

    @property
    def is_cross(self) -> bool:
        return self is MarginMode.CROSS


@dataclass(frozen=True)
class AssetMeta:
    """Per-asset exchange constraints, read from the `meta` info response.

    `sz_decimals` and `max_leverage` are never hardcoded — Hyperliquid changes them,
    and a stale copy means silently rejected orders or an over-leveraged position.
    """

    name: str
    asset_index: int
    sz_decimals: int
    max_leverage: int

    @property
    def maintenance_margin_fraction(self) -> float:
        """Maintenance margin is half the initial margin required at max leverage."""
        return 1.0 / (2.0 * self.max_leverage)


class FillReason(Enum):
    ENTRY = "entry"
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    MANUAL_CLOSE = "manual_close"

    @property
    def is_exit(self) -> bool:
        return self is not FillReason.ENTRY


@dataclass(frozen=True)
class Fill:
    """One executed trade. Entries and exits are both fills.

    Lives here rather than in `broker` so persistence can depend on it without
    depending on the brokers themselves.
    """

    time_ms: int
    coin: str
    #: The side of the *position*, not the order direction — an exit of a long is a
    #: sell but is recorded here as LONG, so a round trip reads as one trade.
    side: Side
    size: float
    price: float
    fee: float
    reason: FillReason
    #: Realised profit net of this fill's fee. None for entries.
    realised_pnl: float | None = None

    def __str__(self) -> str:
        pnl = "" if self.realised_pnl is None else f", pnl {self.realised_pnl:+.2f} USDC"
        return (
            f"{self.reason.value} {self.side.value} {self.size:g} {self.coin} "
            f"@ {self.price:g} (fee {self.fee:.4f}{pnl})"
        )


@dataclass(frozen=True)
class Signal:
    """A strategy's decision to enter, with its exits already worked out.

    Prices here are the strategy's ideal levels; sizing rounds them to the
    exchange's grid before anything is ordered. `reason` is written to the log so a
    user can see why the bot acted.
    """

    side: Side
    entry_price: float
    stop_price: float
    take_profit_price: float | None
    reason: str


@dataclass(frozen=True)
class Position:
    """An open perp position, as reported by `clearinghouseState` or the paper ledger."""

    coin: str
    size: float  # signed — positive is long, negative is short
    entry_price: float
    liquidation_price: float | None
    unrealized_pnl: float
    margin_used: float
    leverage: int

    @property
    def side(self) -> Side:
        return Side.LONG if self.size > 0 else Side.SHORT

    @property
    def abs_size(self) -> float:
        return abs(self.size)

    @property
    def notional(self) -> float:
        return self.abs_size * self.entry_price


@dataclass(frozen=True)
class AccountState:
    """Account-level margin snapshot. `positions` holds only non-flat coins."""

    account_value: float
    withdrawable: float
    total_margin_used: float
    positions: tuple[Position, ...] = ()

    def position_for(self, coin: str) -> Position | None:
        return next((p for p in self.positions if p.coin == coin), None)


@dataclass(frozen=True)
class Candle:
    """One OHLCV bar as returned by `candleSnapshot` or the `candle` subscription."""

    open_time_ms: int
    close_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    trades: int

    @classmethod
    def from_hl(cls, raw: dict) -> Candle:
        return cls(
            open_time_ms=int(raw["t"]),
            close_time_ms=int(raw["T"]),
            open=float(raw["o"]),
            high=float(raw["h"]),
            low=float(raw["l"]),
            close=float(raw["c"]),
            volume=float(raw["v"]),
            trades=int(raw.get("n", 0)),
        )

    @property
    def open_time(self) -> datetime:
        return datetime.fromtimestamp(self.open_time_ms / 1000, tz=timezone.utc)

    @property
    def close_time(self) -> datetime:
        return datetime.fromtimestamp(self.close_time_ms / 1000, tz=timezone.utc)

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @property
    def range(self) -> float:
        return self.high - self.low
