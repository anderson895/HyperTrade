"""Risk sizing — the rules that decide how much money is on the line."""

import pytest

from src.core.models import AssetMeta, Side
from src.core.sizing import (
    PositionPlan,
    RejectReason,
    Rejection,
    estimate_liquidation_price,
    plan_position,
)

BTC = AssetMeta(name="BTC", asset_index=0, sz_decimals=5, max_leverage=40)


def _plan(**overrides):
    kwargs = dict(
        side=Side.LONG,
        entry_price=100_000.0,
        stop_price=99_000.0,  # $1,000 away
        risk_usdc=50.0,
        equity_usdc=10_000.0,
        leverage=1,
        asset=BTC,
    )
    kwargs.update(overrides)
    return plan_position(**kwargs)


# --- sizing ---------------------------------------------------------------


def test_size_comes_from_risk_over_stop_distance():
    plan = _plan()
    assert isinstance(plan, PositionPlan)
    assert plan.size == 0.05  # 50 USDC / 1000 USDC per BTC
    assert plan.risk_usdc == pytest.approx(50.0)
    assert plan.notional == pytest.approx(5_000.0)


def test_stop_loss_costs_exactly_the_risk_per_trade():
    """The headline promise of the risk setting: stop out, lose that much."""
    for risk in (5.0, 25.0, 200.0):
        plan = _plan(risk_usdc=risk, equity_usdc=1_000_000.0)
        assert isinstance(plan, PositionPlan)
        realised_loss = plan.size * abs(plan.entry_price - plan.stop_price)
        assert realised_loss == pytest.approx(risk, rel=1e-9)


def test_risk_is_invariant_to_leverage():
    """Leverage is a margin constraint, not a sizing input.

    Same risk and same stop must produce the same size at 1x and at 10x. Only the
    margin posted changes. Getting this wrong is how a bot that "risks 50 USDC"
    quietly risks 500.
    """
    plans = [_plan(leverage=lev) for lev in (1, 2, 5, 10)]
    assert all(isinstance(p, PositionPlan) for p in plans)
    assert len({p.size for p in plans}) == 1
    assert all(p.risk_usdc == pytest.approx(50.0) for p in plans)
    # Margin required falls as leverage rises, while the position stays the same.
    assert plans[0].margin_required == pytest.approx(5_000.0)
    assert plans[3].margin_required == pytest.approx(500.0)


def test_short_sizes_the_same_as_long():
    plan = _plan(side=Side.SHORT, entry_price=100_000.0, stop_price=101_000.0)
    assert isinstance(plan, PositionPlan)
    assert plan.size == 0.05
    assert plan.risk_usdc == pytest.approx(50.0)


def test_prices_are_rounded_before_risk_is_measured():
    """Risk must reflect the prices the exchange will actually trade."""
    plan = _plan(entry_price=100_000.4, stop_price=99_000.6)
    assert isinstance(plan, PositionPlan)
    assert plan.entry_price == 100_000.0
    assert plan.stop_price == 99_001.0
    assert plan.risk_usdc == pytest.approx(plan.size * 999.0)


# --- refusals -------------------------------------------------------------


def test_rejects_when_notional_exceeds_leverage_cap():
    """Never silently resize, and never raise leverage to make a trade fit."""
    plan = _plan(stop_price=99_900.0, risk_usdc=50.0, equity_usdc=100.0, leverage=2)
    assert isinstance(plan, Rejection)
    assert plan.reason is RejectReason.EXCEEDS_LEVERAGE_CAP
    assert "200.00" in plan.detail  # tells the user what the cap actually was


def test_rejects_when_size_rounds_to_zero():
    plan = _plan(stop_price=90_000.0, risk_usdc=0.05)
    assert isinstance(plan, Rejection)
    assert plan.reason is RejectReason.SIZE_ROUNDS_TO_ZERO


def test_rejects_stop_that_sits_past_liquidation():
    """At 40x the position is liquidated ~1.25% out; a $1,000 stop never triggers."""
    plan = _plan(leverage=40)
    assert isinstance(plan, Rejection)
    assert plan.reason is RejectReason.STOP_BEYOND_LIQUIDATION


def test_rejects_stop_on_the_wrong_side():
    assert _plan(side=Side.LONG, stop_price=101_000.0).reason is RejectReason.STOP_ON_WRONG_SIDE
    assert (
        _plan(side=Side.SHORT, stop_price=99_000.0).reason is RejectReason.STOP_ON_WRONG_SIDE
    )


def test_rejects_leverage_above_asset_maximum():
    plan = _plan(leverage=50)
    assert isinstance(plan, Rejection)
    assert plan.reason is RejectReason.LEVERAGE_ABOVE_MAX


@pytest.mark.parametrize(
    "overrides",
    [
        {"risk_usdc": 0},
        {"equity_usdc": 0},
        {"leverage": 0},
        {"entry_price": 0},
    ],
)
def test_rejects_invalid_input(overrides):
    assert _plan(**overrides).reason is RejectReason.INVALID_INPUT


# --- liquidation estimate -------------------------------------------------


def test_liquidation_moves_closer_as_leverage_rises():
    entry = 100_000.0
    distances = [
        entry - estimate_liquidation_price(entry, Side.LONG, lev, BTC) for lev in (1, 5, 20, 40)
    ]
    assert distances == sorted(distances, reverse=True)
    assert all(d > 0 for d in distances)


def test_liquidation_at_max_leverage_matches_maintenance_margin():
    # At max leverage the position can absorb exactly the maintenance margin
    # fraction (1 / (2 * max_leverage)) before liquidation: 1.25% for BTC at 40x.
    liq = estimate_liquidation_price(100_000.0, Side.LONG, 40, BTC)
    assert liq == pytest.approx(98_750.0)


def test_liquidation_is_above_entry_for_a_short():
    assert estimate_liquidation_price(100_000.0, Side.SHORT, 5, BTC) > 100_000.0
