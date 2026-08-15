"""Exchange precision rules. A bug here means silently rejected orders."""

import pytest

from src.core.precision import (
    floor_size,
    is_valid_price,
    price_decimals,
    round_price,
    round_size,
    slippage_price,
)

BTC_SZ_DECIMALS = 5  # BTC perp on Hyperliquid


def test_price_decimals_budget():
    assert price_decimals(BTC_SZ_DECIMALS) == 1  # 6 - 5
    assert price_decimals(2) == 4
    assert price_decimals(BTC_SZ_DECIMALS, is_spot=True) == 3  # 8 - 5
    assert price_decimals(9) == 0  # never negative


def test_round_price_applies_five_significant_figures():
    # 2345.678 carries 7 significant digits; trimming to 5 gives 2345.7, which also
    # fits the 2-decimal budget for an asset with szDecimals=4.
    assert round_price(2345.678, 4) == 2345.7


def test_round_price_keeps_large_integers_exact():
    """Integers are exempt from the sig-fig rule, so BTC keeps dollar precision.

    Naively applying %.5g would flatten 123456.7 to 123460 — a $3.30 error on every
    order, which on a stop is the difference between a planned loss and a bigger one.
    """
    assert round_price(123456.7, BTC_SZ_DECIMALS) == 123457.0


def test_round_price_result_is_always_valid():
    for price in (95123.7, 123456.7, 2345.678, 0.123456, 68999.99, 1.000049):
        for sz_decimals in (0, 2, 5):
            assert is_valid_price(round_price(price, sz_decimals), sz_decimals)


def test_round_price_rejects_non_positive():
    with pytest.raises(ValueError):
        round_price(0, BTC_SZ_DECIMALS)


def test_floor_size_never_rounds_up():
    """Rounding a size up would risk more than the user authorised."""
    assert floor_size(0.0012399, BTC_SZ_DECIMALS) == 0.00123
    assert floor_size(0.999999, BTC_SZ_DECIMALS) == 0.99999
    assert floor_size(0.0000099, BTC_SZ_DECIMALS) == 0.0


def test_floor_size_survives_binary_float_noise():
    # 0.001 * 1e5 is 99.99999999999999 in binary floating point; a naive
    # floor(size * 10**dp) would return 0.00099 and under-size every order.
    assert floor_size(0.001, BTC_SZ_DECIMALS) == 0.001
    assert floor_size(0.07, 2) == 0.07


def test_round_size_is_nearest_for_exits():
    # Exits must close the whole position; flooring would leave dust behind.
    assert round_size(0.0012399, BTC_SZ_DECIMALS) == 0.00124


def test_is_valid_price():
    assert is_valid_price(95124.0, BTC_SZ_DECIMALS)
    assert is_valid_price(123457, BTC_SZ_DECIMALS)  # integer exemption
    assert not is_valid_price(95124.5, BTC_SZ_DECIMALS)  # 6 sig figs, not an integer
    assert not is_valid_price(1234.56, BTC_SZ_DECIMALS)  # 2 decimals, budget is 1
    assert not is_valid_price(-1, BTC_SZ_DECIMALS)


def test_slippage_price_crosses_the_book():
    """A market order is an IOC limit priced through the book, so buys go up."""
    assert slippage_price(100000, is_buy=True, slippage=0.01, sz_decimals=BTC_SZ_DECIMALS) == 101000
    assert slippage_price(100000, is_buy=False, slippage=0.01, sz_decimals=BTC_SZ_DECIMALS) == 99000
