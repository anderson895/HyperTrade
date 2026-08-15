"""Live checks against the real Hyperliquid API.

Run with `pytest -m network`; excluded from the default run so the suite stays
offline-safe. These exist to prove the project's founding assumption — that
Hyperliquid is reachable from here with no VPN and no custom DNS resolver — and to
catch the day the exchange changes szDecimals or maxLeverage under us.
"""

import pytest

from src.core.models import Network, Timeframe
from src.data.hl_info import HyperliquidInfo

pytestmark = pytest.mark.network


@pytest.fixture
async def info():
    async with HyperliquidInfo(Network.MAINNET) as client:
        yield client


async def test_btc_meta_is_sane(info):
    btc = await info.asset_meta("BTC")
    assert btc.name == "BTC"
    assert 0 <= btc.sz_decimals <= 8
    assert btc.max_leverage >= 1
    # Maintenance margin is half the initial margin at max leverage.
    assert btc.maintenance_margin_fraction == pytest.approx(1 / (2 * btc.max_leverage))


async def test_mid_price_is_plausible(info):
    assert await info.mid_price("BTC") > 1_000


@pytest.mark.parametrize("timeframe", list(Timeframe))
async def test_every_ui_timeframe_returns_candles(info, timeframe):
    """All seven UI timeframes map onto native intervals — nothing is aggregated."""
    candles = await info.recent_candles("BTC", timeframe, 10)
    assert candles, f"no candles for {timeframe.value}"

    times = [candle.open_time_ms for candle in candles]
    assert times == sorted(times), "candles must be oldest-first"
    assert len(set(times)) == len(times), "duplicate candles"

    for candle in candles:
        assert candle.low <= candle.open <= candle.high
        assert candle.low <= candle.close <= candle.high
        assert candle.close_time_ms > candle.open_time_ms


async def test_paging_past_the_5000_candle_cap(info):
    """A single candleSnapshot caps at 5000, so longer histories must page."""
    candles = await info.recent_candles("BTC", Timeframe.M5, 6_000)
    assert len(candles) > 5_000
    times = [candle.open_time_ms for candle in candles]
    assert times == sorted(times)
    assert len(set(times)) == len(times)


async def test_clearinghouse_state_reads_without_a_key(info):
    """Account reads need only an address — no signing, no wallet configured."""
    state = await info.clearinghouse_state("0x0000000000000000000000000000000000000001")
    assert state.account_value >= 0
    assert state.positions == ()
