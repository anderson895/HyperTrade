"""The failures this app expects, so its handlers can be narrow.

A bare `except Exception` around a network call also swallows the `KeyError` or
`AttributeError` that means a real bug, and reports it to the user as "price feed
unavailable". Listing what can genuinely go wrong lets the expected cases be handled
quietly and lets the unexpected ones surface.
"""

from __future__ import annotations

import asyncio

import httpx

from .broker.base import BrokerError
from .data.hl_info import HyperliquidInfoError

#: Reaching Hyperliquid failed, or it answered with something unusable.
FEED_ERRORS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,  # timeouts, connection failures, non-2xx responses
    HyperliquidInfoError,
    OSError,  # socket-level: DNS, refused connections
    asyncio.TimeoutError,
)

#: Everything above, plus a venue refusing an order.
TRADING_ERRORS: tuple[type[BaseException], ...] = (*FEED_ERRORS, BrokerError)
