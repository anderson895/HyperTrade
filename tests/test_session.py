"""Session assembly, and Live failing closed.

Silently trading a simulated account while the user believes it is real would be the
worst failure this app could have, so the fallback is asserted from several angles.
"""

import pytest

from src.broker.paper import PaperBroker
from src.config import AppSettings
from src.core.models import AccountState, AssetMeta, Network, TradingMode
from src.db import connect
from src.session import Session

BTC = AssetMeta(name="BTC", asset_index=0, sz_decimals=5, max_leverage=40)
ADDRESS = "0x5eA3e82B3605201d09b349789feD24E30D76c41b"


class FakeInfo:
    """Stands in for the read client. `perps`/`spot` set what the account holds."""

    def __init__(self, perps: float | None = None, spot: float = 0.0):
        self.perps = perps
        self.spot = spot

    async def clearinghouse_state(self, address):
        if self.perps is None:
            raise ConnectionError("unreachable")
        return AccountState(
            account_value=self.perps, withdrawable=self.perps, total_margin_used=0.0
        )

    async def spot_usdc(self, address):
        return self.spot

    async def aclose(self):
        pass


@pytest.fixture
def conn():
    connection = connect(":memory:")
    yield connection
    connection.close()


async def build(conn, settings, info: FakeInfo | None = None) -> Session:
    session = Session(conn, settings, info or FakeInfo(), BTC)
    await session.wire()
    return session


def live_settings() -> AppSettings:
    return AppSettings(trading_mode=TradingMode.LIVE, account_address=ADDRESS)


async def test_paper_settings_produce_a_paper_broker(conn):
    session = await build(conn, AppSettings())

    assert isinstance(session.broker, PaperBroker)
    assert session.broker.mode is TradingMode.PAPER
    assert session.fell_back_to_paper is None


async def test_live_without_a_key_falls_back_to_paper(conn, monkeypatch):
    monkeypatch.setattr("src.secrets_store.load_agent_key", lambda: None)
    settings = AppSettings(trading_mode=TradingMode.LIVE, account_address=ADDRESS)

    session = await build(conn, settings)

    assert isinstance(session.broker, PaperBroker)
    assert "no API wallet key" in session.fell_back_to_paper


async def test_live_that_cannot_reach_the_exchange_falls_back(conn, monkeypatch):
    """A half-built live broker is worse than none: nothing has been ordered yet,
    and this way nothing will be."""
    monkeypatch.setattr("src.secrets_store.load_agent_key", lambda: "0x" + "ab" * 32)

    session = await build(conn, live_settings(), FakeInfo(perps=None))

    assert isinstance(session.broker, PaperBroker)
    assert session.fell_back_to_paper


async def test_money_in_the_spot_wallet_is_named_as_the_reason(conn, monkeypatch):
    """Hyperliquid keeps spot and perps apart and only perps margins a trade. Found
    the hard way: a funded account whose bot would have rejected every trade."""
    monkeypatch.setattr("src.secrets_store.load_agent_key", lambda: "0x" + "ab" * 32)

    session = await build(conn, live_settings(), FakeInfo(perps=0.0, spot=99.72))

    assert isinstance(session.broker, PaperBroker)
    assert "99.72 USDC is sitting in spot" in session.fell_back_to_paper
    assert "transfer it to Perps" in session.fell_back_to_paper


async def test_an_empty_account_is_refused_rather_than_left_to_fail_per_trade(conn, monkeypatch):
    monkeypatch.setattr("src.secrets_store.load_agent_key", lambda: "0x" + "ab" * 32)

    session = await build(conn, live_settings(), FakeInfo(perps=0.0, spot=0.0))

    assert isinstance(session.broker, PaperBroker)
    assert "holds no USDC" in session.fell_back_to_paper


async def test_the_engine_gets_whatever_broker_the_session_built(conn):
    session = await build(conn, AppSettings())
    assert session.engine.broker is session.broker


async def test_rewiring_keeps_the_connection_and_the_asset(conn):
    session = await build(conn, AppSettings(risk_usdc=5.0))
    info, asset = session.info, session.asset

    await session.apply_settings(AppSettings(risk_usdc=25.0))

    assert session.info is info
    assert session.asset is asset
    assert session.engine.settings.risk_usdc == 25.0


async def test_a_paper_position_survives_a_settings_change(conn):
    """Re-wiring builds a new broker, which must reload rather than start fresh."""
    session = await build(conn, AppSettings(paper_starting_balance=1_000.0))
    session.broker.reset(750.0)

    await session.apply_settings(AppSettings(paper_starting_balance=1_000.0, leverage=3))

    assert session.broker.balance == pytest.approx(750.0)


async def test_the_network_choice_reaches_the_session(conn):
    session = await build(conn, AppSettings(network=Network.TESTNET))
    assert session.settings.network is Network.TESTNET
