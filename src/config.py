"""User settings: the typed record, its validation, and its persistence.

Secrets are deliberately absent — the agent private key lives in Windows Credential
Manager (see `secrets_store`), never in this table.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, fields, replace
from enum import Enum
from typing import Any, get_type_hints

from .core.models import MarginMode, Network, Timeframe, TradingMode
from .strategy import available

ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
PRIVATE_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")

#: Timeframes that backtested at a clear loss for the shipped strategy
#: (−0.905R and −0.395R per trade; see the evidence table in SKILL.md). They stay
#: selectable because the spec asks for all seven, but the user is warned first.
DISCOURAGED_TIMEFRAMES = frozenset({Timeframe.M5, Timeframe.M15})


@dataclass
class AppSettings:
    """Everything the user can change in the Settings screen."""

    # --- account ---
    trading_mode: TradingMode = TradingMode.PAPER
    network: Network = Network.MAINNET
    account_address: str = ""  # the main wallet that holds the USDC

    # --- trading ---
    coin: str = "BTC"
    # 4h backtested strongest of the seven, on the most history. See SKILL.md.
    timeframe: Timeframe = Timeframe.H4
    #: Registry key, not a class — a renamed strategy would orphan saved configs.
    strategy: str = "trend_following"
    risk_usdc: float = 5.0
    #: When above 0 this replaces `risk_usdc`: risk becomes this fraction of equity,
    #: recomputed per trade, so the stake grows and shrinks with the account.
    risk_pct: float = 0.0
    #: Cut the size down to what the leverage allows instead of refusing the trade.
    #: See `plan_position` — a tight stop can need more notional than any account
    #: can hold, and a strategy backtested with a clamp measured the clamped size.
    clamp_size_to_leverage: bool = False
    leverage: int = 2
    margin_mode: MarginMode = MarginMode.ISOLATED
    max_concurrent_positions: int = 1
    daily_loss_limit_usdc: float = 0.0  # 0 disables the limit

    # --- exits ---
    #: Target as a multiple of the stop distance. 2.0 means a 1:2 risk to reward:
    #: stop 1,000 below a 60,000 entry puts the target at 62,000.
    take_profit_rr: float = 2.0
    #: Move the stop up behind the trade to lock in profit as it runs.
    #: Off by default on purpose. A saved config predates this field and would
    #: silently inherit whatever it defaults to — and switching it on would change
    #: the exits of a strategy that was backtested without it.
    trailing_enabled: bool = False
    #: Profit, in multiples of the stop distance, before trailing starts. 0 means
    #: as soon as the trailing stop would sit better than the original — which is
    #: what the reference implementation actually did, whatever its config said.
    trailing_activation_rr: float = 0.0
    #: How far the trailing stop sits behind the best price reached.
    trailing_distance_pct: float = 0.004
    #: How far past the rejection wick the volume-rejection stop sits.
    stop_buffer_pct: float = 0.001

    # --- paper ---
    paper_starting_balance: float = 1_000.0

    # --- safety ---
    news_blackout_enabled: bool = True
    news_blackout_before_min: int = 30
    #: Symmetric with the window before. It was 15 on the reasoning that liquidity
    #: comes back faster than it withdraws, which is true but was a guess; the
    #: strategy this app now runs was backtested against 30 either side, and
    #: matching the thing that was measured beats matching an argument.
    news_blackout_after_min: int = 30
    economic_data_day_block: bool = False

    # --- execution ---
    slippage: float = 0.01  # IOC limit orders are priced this far through the book
    #: Rest a maker order at the signal price instead of crossing the spread. Pays
    #: the maker fee and only trades when price comes back — which is itself a
    #: filter, and one a backtest built on maker fills has already counted.
    post_only_entry: bool = False
    #: Candles a resting entry may wait before it is cancelled as stale.
    entry_expiry_candles: int = 2

    @property
    def is_live(self) -> bool:
        return self.trading_mode is TradingMode.LIVE

    def risk_for(self, equity_usdc: float) -> float:
        """What to stake on the next trade, given what the account is worth now.

        A percentage compounds both ways: the stake grows with a winning account and
        shrinks with a losing one, which is the point of setting it that way.
        """
        return equity_usdc * self.risk_pct if self.risk_pct else self.risk_usdc

    def validate(self, *, has_agent_key: bool = False) -> list[str]:
        """Problems that must be fixed before the bot can start, in plain language.

        `has_agent_key` is passed in rather than read here so this stays pure and
        testable — no keyring, no I/O.
        """
        problems: list[str] = []

        if self.risk_pct:
            if not 0 < self.risk_pct <= 0.25:
                problems.append("Risk per trade must be between 0% and 25% of equity.")
        elif self.risk_usdc <= 0:
            problems.append("Risk per trade must be greater than 0 USDC.")
        if self.leverage < 1:
            problems.append("Leverage must be at least 1x.")
        if self.max_concurrent_positions < 1:
            problems.append("Max concurrent positions must be at least 1.")
        if self.daily_loss_limit_usdc < 0:
            problems.append("Daily loss limit cannot be negative.")
        if not 0 < self.slippage <= 0.05:
            problems.append("Slippage must be between 0% and 5%.")
        if self.news_blackout_before_min < 0 or self.news_blackout_after_min < 0:
            problems.append("News blackout minutes cannot be negative.")

        if self.strategy not in available():
            problems.append(
                f"Unknown strategy {self.strategy!r}. Available: "
                f"{', '.join(sorted(available()))}."
            )
        if self.take_profit_rr <= 0:
            problems.append("Take profit must be greater than 0R.")
        if self.trailing_activation_rr < 0:
            problems.append("Trailing activation cannot be negative.")
        if not 0 < self.trailing_distance_pct <= 0.20:
            problems.append("Trailing distance must be between 0% and 20%.")
        if not 0 <= self.stop_buffer_pct <= 0.20:
            problems.append("Stop buffer must be between 0% and 20%.")
        if self.entry_expiry_candles < 1:
            problems.append("Entry expiry must be at least 1 candle.")

        if self.is_live:
            if PRIVATE_KEY_RE.match(self.account_address or ""):
                # This field is persisted to SQLite in plain text. A key must never
                # reach it, and the two are easy to confuse when they arrive in the
                # same note.
                problems.append(
                    "That is a private key, not a wallet address. Never paste a "
                    "private key here - this field is saved to disk in plain text. "
                    "The address is 42 characters; the key goes in the API Wallet "
                    "field below, which stores it in Windows Credential Manager."
                )
            elif not ADDRESS_RE.match(self.account_address):
                problems.append(
                    "Live mode needs your main wallet address (0x + 40 hex characters)."
                )
            if not has_agent_key:
                problems.append(
                    "Live mode needs an API wallet (agent) key. Approve one in Settings."
                )
        elif self.paper_starting_balance <= 0:
            problems.append("Paper starting balance must be greater than 0 USDC.")

        return problems

    def advisories(self) -> list[str]:
        """Non-blocking warnings to show before starting.

        Separate from `validate` on purpose: these do not stop the bot, because it is
        the user's money and their call. They just make sure the choice is informed.
        """
        notes: list[str] = []
        if self.timeframe in DISCOURAGED_TIMEFRAMES:
            notes.append(
                f"{self.timeframe.label} backtested at a loss for this strategy "
                f"(see SKILL.md). Run it in Paper mode before risking real money."
            )
        return notes


# --- persistence ----------------------------------------------------------


def _serialise(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def _deserialise(raw: str, target: type) -> Any:
    if isinstance(target, type) and issubclass(target, Enum):
        return target(raw)
    if target is bool:
        return raw == "1"
    if target is int:
        return int(raw)
    if target is float:
        return float(raw)
    return raw


def load_settings(conn: sqlite3.Connection) -> AppSettings:
    """Read settings, falling back to the default for anything missing or corrupt.

    A single unreadable row must not stop the app from opening, so bad values are
    dropped back to their default rather than raised.
    """
    stored = {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM settings")}
    hints = get_type_hints(AppSettings)

    values: dict[str, Any] = {}
    for field in fields(AppSettings):
        if field.name not in stored:
            continue
        try:
            values[field.name] = _deserialise(stored[field.name], hints[field.name])
        except (ValueError, KeyError):
            continue  # keep the default

    return replace(AppSettings(), **values)


def save_settings(conn: sqlite3.Connection, settings: AppSettings) -> None:
    conn.executemany(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        [
            (field.name, _serialise(getattr(settings, field.name)))
            for field in fields(settings)
        ],
    )
    conn.commit()
