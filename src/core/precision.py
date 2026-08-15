"""Hyperliquid price and size precision rules.

The exchange rejects orders whose price or size violate these rules, so every value
that goes on the wire passes through this module. Centralised on purpose: a rounding
bug scattered across the codebase is the classic cause of "my order silently never
filled".

Rules (perps):
  * price — at most 5 significant figures, AND at most ``6 - szDecimals`` decimals.
    Integer prices are exempt from the significant-figure rule.
  * size  — rounded to exactly ``szDecimals`` decimals.

Spot uses 8 instead of 6 for the decimal budget.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

MAX_SIG_FIGS = 5
PERP_MAX_DECIMALS = 6
SPOT_MAX_DECIMALS = 8


def price_decimals(sz_decimals: int, is_spot: bool = False) -> int:
    """Decimal places allowed for a price on an asset with `sz_decimals`."""
    budget = SPOT_MAX_DECIMALS if is_spot else PERP_MAX_DECIMALS
    return max(0, budget - sz_decimals)


def round_price(price: float, sz_decimals: int, is_spot: bool = False) -> float:
    """Round to the nearest exchange-valid price.

    Both constraints apply at once, and integers are a third, overlapping set of
    valid prices. So we build the best candidate under the sig-fig + decimal rules,
    build the integer candidate, and return whichever landed closer to `price` —
    that keeps a large price like 123_456 exact instead of flattening it to 123_460.
    """
    if price <= 0:
        raise ValueError(f"price must be positive, got {price!r}")

    exact = Decimal(str(price))
    quantum = Decimal(1).scaleb(-price_decimals(sz_decimals, is_spot))
    # f"{price:.5g}" yields a decimal string (possibly in scientific notation),
    # which Decimal parses exactly — avoiding the binary float noise that makes
    # round(0.145, 2) surprising.
    significant = Decimal(f"{price:.{MAX_SIG_FIGS}g}").quantize(
        quantum, rounding=ROUND_HALF_UP
    )
    integer = exact.quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if integer > 0 and abs(integer - exact) < abs(significant - exact):
        return float(integer)
    return float(significant)


def floor_size(size: float, sz_decimals: int) -> float:
    """Round a size DOWN to the asset's precision.

    Always down, never nearest: rounding up would put more at risk than the user
    authorised, which is the one direction that must never happen silently.
    """
    if size < 0:
        raise ValueError(f"size must not be negative, got {size!r}")
    quantum = Decimal(1).scaleb(-sz_decimals)
    return float(Decimal(str(size)).quantize(quantum, rounding=ROUND_DOWN))


def round_size(size: float, sz_decimals: int) -> float:
    """Round a size to the nearest valid step.

    For exits only — closing a position needs the true size, not a floored one, or
    a dust remainder is left behind and the position never reads as flat.
    """
    if size < 0:
        raise ValueError(f"size must not be negative, got {size!r}")
    quantum = Decimal(1).scaleb(-sz_decimals)
    return float(Decimal(str(size)).quantize(quantum, rounding=ROUND_HALF_UP))


def is_valid_price(price: float, sz_decimals: int, is_spot: bool = False) -> bool:
    """Whether `price` would be accepted as-is. Used by tests and pre-flight checks."""
    if price <= 0:
        return False
    value = Decimal(str(price)).normalize()
    if value == value.to_integral_value():
        return True
    if -value.as_tuple().exponent > price_decimals(sz_decimals, is_spot):
        return False
    return len(value.as_tuple().digits) <= MAX_SIG_FIGS


def slippage_price(price: float, is_buy: bool, slippage: float, sz_decimals: int) -> float:
    """Aggressive limit price for a market-style order.

    Hyperliquid has no market order type — a market order is an IOC limit priced
    through the book by `slippage` (0.01 = 1%).
    """
    return round_price(price * (1 + slippage if is_buy else 1 - slippage), sz_decimals)
