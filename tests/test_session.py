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
    """Stands in for the read client.

    `perps`/`spot` set what the account holds; `agent_of` is the wallet Hyperliquid
    says the agent key may trade for, and None means it is not an agent at all.
    """

    def __init__(
        self,
        perps: float | None = None,
        spot: float = 0.0,
        agent_of: str | None = ADDRESS,
        expiries: dict[str, int] | None = None,
    ):
        self.perps = perps
        self.spot = spot
        self.agent_of = agent_of
        self.expiries = expiries or {}

    async def clearinghouse_state(self, address):
        if self.perps is None:
            raise ConnectionError("unreachable")
        return AccountState(
            account_value=self.perps, withdrawable=self.perps, total_margin_used=0.0
        )

    async def spot_usdc(self, address):
        return self.spot

    async def agent_owner(self, agent_address):
        return self.agent_of

    async def agent_expiries(self, address):
        return self.expiries

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


# --- choosing a strategy ---------------------------------------------------


async def test_the_chosen_strategy_is_the_one_that_runs(conn):
    """Regression: session.py hardcoded its strategy, so one could be written,
    registered and selected while the bot went on trading another. A live account
    was started on the wrong system before this was wired up."""
    from src.strategy import VolumeRejection

    session = await build(conn, AppSettings(strategy="volume_rejection"))

    assert isinstance(session.engine.strategy, VolumeRejection)
    assert session.engine.strategy.name == "volume_rejection"


async def test_the_take_profit_setting_reaches_the_strategy(conn):
    session = await build(
        conn, AppSettings(strategy="volume_rejection", take_profit_rr=3.0)
    )
    assert session.engine.strategy.take_profit_rr == 3.0


async def test_the_stop_buffer_reaches_the_strategy_that_has_one(conn):
    session = await build(
        conn, AppSettings(strategy="volume_rejection", stop_buffer_pct=0.005)
    )
    assert session.engine.strategy.stop_buffer == 0.005


async def test_only_parameters_the_strategy_declares_are_passed(conn):
    """A strategy that does not take one of the exit settings must still build.
    The alternative is a TypeError at startup every time a setting is added for
    one strategy and not another."""
    from src.strategy.base import Strategy, _REGISTRY

    class Spartan(Strategy):
        name = "spartan"
        display_name = "takes nothing"

        @property
        def warmup_candles(self) -> int:
            return 1

        def evaluate(self, candles):
            return None

    _REGISTRY["spartan"] = Spartan
    try:
        session = await build(conn, AppSettings(strategy="spartan"))
        assert isinstance(session.engine.strategy, Spartan)
    finally:
        del _REGISTRY["spartan"]


async def test_an_unknown_saved_strategy_falls_back_loudly(conn, caplog):
    """A config naming a strategy this build no longer has must not stop the app -
    but it must not quietly trade something the user did not choose either."""
    import logging

    from src.session import DEFAULT_STRATEGY

    with caplog.at_level(logging.ERROR):
        session = await build(conn, AppSettings(strategy="no_such_strategy"))

    assert session.engine.strategy.name == DEFAULT_STRATEGY
    assert "no_such_strategy" in caplog.text
    assert "falling back" in caplog.text


def test_an_unknown_strategy_is_refused_before_the_bot_starts():
    problems = AppSettings(strategy="no_such_strategy").validate()
    assert any("Unknown strategy" in problem for problem in problems)


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


async def test_a_key_that_is_not_an_approved_agent_is_refused(conn, monkeypatch):
    """Reading a balance needs only an address, so up to this check a wrong key
    looked identical to a right one - the account value came back, the connection
    read as healthy, and nothing touched the key until the first real order."""
    monkeypatch.setattr("src.secrets_store.load_agent_key", lambda: "0x" + "ab" * 32)

    session = await build(conn, live_settings(), FakeInfo(perps=500.0, agent_of=None))

    assert isinstance(session.broker, PaperBroker)
    assert "not an approved agent" in session.fell_back_to_paper


async def test_an_agent_for_a_different_account_is_refused(conn, monkeypatch):
    """A key that works, for someone else's wallet. Every order it signed would be
    rejected, and nothing before this said so."""
    monkeypatch.setattr("src.secrets_store.load_agent_key", lambda: "0x" + "ab" * 32)
    someone_else = "0x1111111111111111111111111111111111111111"

    session = await build(
        conn, live_settings(), FakeInfo(perps=500.0, agent_of=someone_else)
    )

    assert isinstance(session.broker, PaperBroker)
    assert "belongs to a different account" in session.fell_back_to_paper
    assert someone_else in session.fell_back_to_paper


async def test_a_lapsed_approval_is_refused(conn, monkeypatch):
    """An approval that has run out is a wrong key arriving on a schedule: the key
    is right, and it stops being able to order while everything else looks fine."""
    from eth_account import Account

    key = "0x" + "ab" * 32
    monkeypatch.setattr("src.secrets_store.load_agent_key", lambda: key)
    agent = Account.from_key(key).address.lower()

    session = await build(
        conn, live_settings(), FakeInfo(perps=500.0, expiries={agent: 1})
    )

    assert isinstance(session.broker, PaperBroker)
    assert "expired" in session.fell_back_to_paper


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
