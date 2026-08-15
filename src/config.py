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
    risk_usdc: float = 5.0
    leverage: int = 2
    margin_mode: MarginMode = MarginMode.ISOLATED
    max_concurrent_positions: int = 1
    daily_loss_limit_usdc: float = 0.0  # 0 disables the limit

    # --- paper ---
    paper_starting_balance: float = 1_000.0

    # --- safety ---
    news_blackout_enabled: bool = True
    news_blackout_before_min: int = 30
    news_blackout_after_min: int = 15
    economic_data_day_block: bool = False

    # --- execution ---
    slippage: float = 0.01  # IOC limit orders are priced this far through the book

    @property
    def is_live(self) -> bool:
        return self.trading_mode is TradingMode.LIVE

    def validate(self, *, has_agent_key: bool = False) -> list[str]:
        """Problems that must be fixed before the bot can start, in plain language.

        `has_agent_key` is passed in rather than read here so this stays pure and
        testable — no keyring, no I/O.
        """
        problems: list[str] = []

        if self.risk_usdc <= 0:
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
