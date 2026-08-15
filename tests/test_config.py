"""Settings persistence and the checks that gate the START button."""

import pytest

from src.config import AppSettings, load_settings, save_settings
from src.core.models import MarginMode, Network, Timeframe, TradingMode
from src.db import connect

LIVE_ADDRESS = "0x5eA3e82B3605201d09b349789feD24E30D76c41b"


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


def test_defaults_are_the_safe_ones():
    settings = AppSettings()
    assert settings.trading_mode is TradingMode.PAPER
    assert settings.margin_mode is MarginMode.ISOLATED
    assert settings.max_concurrent_positions == 1
    assert settings.news_blackout_enabled


def test_settings_survive_a_save_and_load(conn):
    original = AppSettings(
        trading_mode=TradingMode.LIVE,
        network=Network.TESTNET,
        account_address=LIVE_ADDRESS,
        timeframe=Timeframe.M15,
        risk_usdc=12.5,
        leverage=5,
        margin_mode=MarginMode.CROSS,
        paper_starting_balance=2_500.0,
        news_blackout_enabled=False,
    )
    save_settings(conn, original)
    assert load_settings(conn) == original


def test_saving_twice_updates_rather_than_duplicates(conn):
    save_settings(conn, AppSettings(risk_usdc=5.0))
    save_settings(conn, AppSettings(risk_usdc=20.0))
    assert load_settings(conn).risk_usdc == 20.0
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == len(
        AppSettings.__dataclass_fields__
    )


def test_a_corrupt_row_falls_back_to_its_default(conn):
    """One bad value must not stop the app from opening."""
    save_settings(conn, AppSettings(risk_usdc=20.0))
    conn.execute("UPDATE settings SET value = 'not-a-number' WHERE key = 'risk_usdc'")
    conn.execute("UPDATE settings SET value = 'martian' WHERE key = 'timeframe'")
    conn.commit()

    settings = load_settings(conn)
    assert settings.risk_usdc == AppSettings().risk_usdc
    assert settings.timeframe is AppSettings().timeframe


def test_empty_database_gives_defaults(conn):
    assert load_settings(conn) == AppSettings()


# --- validation -----------------------------------------------------------


def test_default_paper_settings_are_startable():
    assert AppSettings().validate() == []


@pytest.mark.parametrize(
    "overrides",
    [
        {"risk_usdc": 0},
        {"leverage": 0},
        {"slippage": 0.5},
        {"slippage": 0},
        {"news_blackout_before_min": -1},
        {"max_concurrent_positions": 0},
        {"paper_starting_balance": 0},
    ],
)
def test_invalid_settings_are_reported(overrides):
    assert AppSettings(**overrides).validate()


def test_live_mode_requires_an_address_and_a_key():
    settings = AppSettings(trading_mode=TradingMode.LIVE)
    problems = settings.validate(has_agent_key=False)
    assert any("wallet address" in problem for problem in problems)
    assert any("agent" in problem for problem in problems)


def test_a_private_key_in_the_address_field_is_named_for_what_it_is():
    """This field is persisted to SQLite in plain text, so a key must never reach
    it. The two get swapped when they arrive in the same note."""
    settings = AppSettings(
        trading_mode=TradingMode.LIVE, account_address="0x" + "ab" * 32
    )
    problems = settings.validate(has_agent_key=True)

    assert any("is a private key, not a wallet address" in problem for problem in problems)
    assert any("saved to disk in plain text" in problem for problem in problems)


def test_live_mode_rejects_a_malformed_address():
    settings = AppSettings(trading_mode=TradingMode.LIVE, account_address="0xnope")
    assert any("wallet address" in problem for problem in settings.validate(has_agent_key=True))


def test_live_mode_is_startable_once_configured():
    settings = AppSettings(trading_mode=TradingMode.LIVE, account_address=LIVE_ADDRESS)
    assert settings.validate(has_agent_key=True) == []


# --- advisories -----------------------------------------------------------


@pytest.mark.parametrize("timeframe", [Timeframe.M5, Timeframe.M15])
def test_loss_making_timeframes_are_flagged(timeframe):
    """5m and 15m backtested at -0.9R and -0.4R; the user gets told, not blocked."""
    settings = AppSettings(timeframe=timeframe)
    assert settings.advisories()
    assert settings.validate() == []  # a warning, not a blocker


def test_the_default_timeframe_is_not_flagged():
    assert AppSettings().timeframe is Timeframe.H4
    assert AppSettings().advisories() == []
