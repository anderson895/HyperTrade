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

#: Timeframes where the strategy as specified backtested at a clear loss on a
#: sample big enough to read (measured 2026-08-16, 4.5bps): 5m −0.316R, 15m
#: −0.448R, 30m −0.293R, 4h −0.399R. 15m stays the default because the spec names
#: it, so the warning fires on a fresh install — that is deliberate. Not listed:
#: 1h (−0.066R, near enough to zero to be undecided) and 1d (+0.484R on 12 trades,
#: too few to mean anything). Absence here is "not proven bad", not "good".
#: See the evidence table in README.md.
DISCOURAGED_TIMEFRAMES = frozenset(
    {Timeframe.M5, Timeframe.M15, Timeframe.M30, Timeframe.H4}
)


@dataclass
class AppSettings:
    """Everything the user can change in the Settings screen."""

    # --- account ---
    trading_mode: TradingMode = TradingMode.PAPER
    network: Network = Network.MAINNET
    account_address: str = ""  # the main wallet that holds the USDC

    # --- trading ---
    coin: str = "BTC"
    # 15m because the specification is called "The 15-Minute Volume Rejection
    # Strategy" and names 96 candles as 24 hours. It is also the timeframe that
    # backtests worst, so `advisories()` warns every time it is started — the
    # default matches what was asked for, and the warning carries what was
    # measured. Overriding it silently would leave the app trading something the
    # spec never described.
    timeframe: Timeframe = Timeframe.M15
    #: Registry key, not a class — a renamed strategy would orphan saved configs.
    strategy: str = "volume_rejection"
    risk_usdc: float = 5.0
    #: When above 0 this replaces `risk_usdc`: risk becomes this fraction of equity,
    #: recomputed per trade, so the stake grows and shrinks with the account.
    #: 3% is `ACCOUNT_RISK_PCT` from the specification.
    risk_pct: float = 0.03
    #: Cut the size down to what the leverage allows instead of refusing the trade.
    #: See `plan_position` — a tight stop can need more notional than any account
    #: can hold. On by default because the specification sizes the same way:
    #: `min(risk_amount / sl_dist_pct, equity * MAX_LEVERAGE)` is this clamp.
    #: Worth knowing what it means at 3% and 5x — a 0.38% stop wants 7.9x, so the
    #: clamp binds on most signals and the real risk taken is nearer 0.6% than 3%.
    #: The engine logs the reduction every time rather than letting it pass silently.
    clamp_size_to_leverage: bool = True
    #: `MAX_LEVERAGE` from the specification, hard-capped there at 5.
    leverage: int = 5
    margin_mode: MarginMode = MarginMode.ISOLATED
    max_concurrent_positions: int = 1
    #: Superseded by `daily_loss_limit_pct` and kept only so a config saved before
    #: it existed does not silently lose its limit — losing protection quietly is
    #: the one direction a safety setting must never move on its own.
    daily_loss_limit_usdc: float = 0.0  # 0 disables the limit
    #: The same circuit breaker as a fraction of equity, which is what the Settings
    #: form edits. A fixed USDC figure does not travel: 2.00 is a sensible breaker
    #: on a 99 USDC account and halts a 1,000 USDC one after three trades.
    #:
    #: Not in the specification — it adds nothing to the strategy and takes nothing
    #: away. It only refuses *new* entries once the day's realised losses pass the
    #: line; sizing, stops, targets and the signal itself are untouched, and an open
    #: position keeps the exits already lodged with the exchange. The one thing to
    #: know is that a backtest has no such limit, so a halted day is a day the
    #: backtest would have gone on trading.
    daily_loss_limit_pct: float = 0.02

    # --- exits ---
    #: Target as a multiple of the stop distance. 2.0 means a 1:2 risk to reward:
    #: stop 1,000 below a 60,000 entry puts the target at 62,000.
    take_profit_rr: float = 2.0
    #: Move the stop up behind the trade to lock in profit as it runs.
    #: `ENABLE_TRAILING_STOP = True` in the specification.
    trailing_enabled: bool = True
    #: Profit, in multiples of the stop distance, before trailing starts.
    #: `TRAILING_ACTIVATION_RR = 1.0` in the specification. Note that the reference
    #: script declares that constant and then never reads it — it trails from the
    #: first tick of profit. The written instruction is followed here rather than
    #: the code that contradicts it, and it is also the safer of the two: trailing
    #: from tick one tightens the stop into ordinary noise.
    trailing_activation_rr: float = 1.0
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
    #: filter. On by default: the specification says "places a Post-Only (ALO)
    #: limit order at the candle close", and it is the only entry style it names.
    post_only_entry: bool = True
    #: Candles a resting entry may wait before it is cancelled as stale.
    #: The specification says 30 minutes, which is 2 candles at its 15m timeframe.
    #: Counted in candles rather than minutes so it still means "two bars" if the
    #: timeframe is changed.
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

    def daily_loss_limit_for(self, equity_usdc: float) -> float:
        """How much may be lost today before entries stop. 0 means no limit.

        The percentage wins when it is set, so a legacy fixed limit stays in force
        only until one is chosen — never the other way round.
        """
        if self.daily_loss_limit_pct:
            return equity_usdc * self.daily_loss_limit_pct
        return self.daily_loss_limit_usdc

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
        if not 0 <= self.daily_loss_limit_pct <= 0.50:
            problems.append("Daily loss limit must be between 0% and 50% of equity.")
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
                f"(see README.md). Run it in Paper mode before risking real money."
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
