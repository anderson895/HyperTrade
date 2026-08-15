"""Read-only Hyperliquid data: the `/info` endpoint.

Everything here is public and unauthenticated except the account calls, which need
only an address — no signing, no key. Kept separate from execution so the chart, the
balance card, and backtests all work with the bot stopped and no wallet configured.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from ..core.models import AccountState, AssetMeta, Candle, Network, Position, Timeframe

log = logging.getLogger(__name__)

#: `candleSnapshot` returns at most this many candles per request, so longer
#: histories are paged backwards.
MAX_CANDLES_PER_REQUEST = 5000


class HyperliquidInfoError(RuntimeError):
    """The info endpoint was reachable but returned something unusable."""


def _now_ms() -> int:
    return int(time.time() * 1000)


class HyperliquidInfo:
    """Async client for `POST /info`.

    Hyperliquid resolves and serves normally from this region, so unlike the
    Polymarket build there is no DNS-over-HTTPS shim and no proxy — plain httpx.
    """

    def __init__(
        self,
        network: Network = Network.MAINNET,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        self.network = network
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=network.api_url,
            timeout=timeout,
            headers={"Content-Type": "application/json"},
        )

    async def __aenter__(self) -> HyperliquidInfo:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _post(self, payload: dict[str, Any]) -> Any:
        response = await self._client.post("/info", json=payload)
        response.raise_for_status()
        return response.json()

    # --- market metadata --------------------------------------------------

    async def perp_meta(self) -> dict[str, AssetMeta]:
        """Every perp asset's constraints, keyed by coin name.

        The asset's index in `universe` is its wire identifier in order payloads, so
        it is captured here rather than assumed.
        """
        data = await self._post({"type": "meta"})
        universe = data.get("universe") if isinstance(data, dict) else None
        if not universe:
            raise HyperliquidInfoError(f"meta response has no universe: {data!r}")

        return {
            entry["name"]: AssetMeta(
                name=entry["name"],
                asset_index=index,
                sz_decimals=int(entry["szDecimals"]),
                max_leverage=int(entry["maxLeverage"]),
            )
            for index, entry in enumerate(universe)
            if not entry.get("isDelisted")
        }

    async def asset_meta(self, coin: str = "BTC") -> AssetMeta:
        meta = await self.perp_meta()
        if coin not in meta:
            raise HyperliquidInfoError(f"{coin} is not in the perp universe")
        return meta[coin]

    # --- prices -----------------------------------------------------------

    async def all_mids(self) -> dict[str, float]:
        data = await self._post({"type": "allMids"})
        return {coin: float(px) for coin, px in data.items()}

    async def mid_price(self, coin: str = "BTC") -> float:
        mids = await self.all_mids()
        if coin not in mids:
            raise HyperliquidInfoError(f"no mid price for {coin}")
        return mids[coin]

    # --- candles ----------------------------------------------------------

    async def candles(
        self,
        coin: str,
        timeframe: Timeframe,
        start_ms: int,
        end_ms: int | None = None,
    ) -> list[Candle]:
        """One `candleSnapshot` page, oldest first."""
        data = await self._post(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": timeframe.value,
                    "startTime": int(start_ms),
                    "endTime": int(end_ms if end_ms is not None else _now_ms()),
                },
            }
        )
        if not isinstance(data, list):
            raise HyperliquidInfoError(f"candleSnapshot returned {type(data).__name__}: {data!r}")
        return [Candle.from_hl(raw) for raw in data]

    async def recent_candles(
        self, coin: str, timeframe: Timeframe, count: int
    ) -> list[Candle]:
        """The most recent `count` candles, oldest first, paging past the 5000 cap.

        The final (still-forming) candle is included; callers that act only on closed
        candles must drop it themselves.
        """
        if count <= 0:
            return []

        interval_ms = timeframe.seconds * 1000
        collected: dict[int, Candle] = {}
        end = _now_ms()

        while len(collected) < count:
            page = min(count - len(collected), MAX_CANDLES_PER_REQUEST)
            batch = await self.candles(coin, timeframe, end - page * interval_ms, end)
            if not batch:
                break

            before = len(collected)
            collected.update({candle.open_time_ms: candle for candle in batch})
            if len(collected) == before:
                # The exchange has no more history this far back; stop rather than
                # loop forever asking for it.
                break
            end = min(candle.open_time_ms for candle in batch) - 1

        ordered = sorted(collected.values(), key=lambda candle: candle.open_time_ms)
        if len(ordered) < count:
            log.debug(
                "requested %d %s candles for %s, exchange had %d",
                count, timeframe.value, coin, len(ordered),
            )
        return ordered[-count:]

    # --- account ----------------------------------------------------------

    async def clearinghouse_state(self, address: str) -> AccountState:
        """Margin and open positions for `address`. Read-only, needs no key."""
        data = await self._post({"type": "clearinghouseState", "user": address})
        summary = data.get("marginSummary", {})

        positions = []
        for wrapper in data.get("assetPositions", []):
            raw = wrapper.get("position", {})
            size = float(raw.get("szi", 0) or 0)
            if size == 0:
                continue
            liquidation = raw.get("liquidationPx")
            leverage = raw.get("leverage", {})
            positions.append(
                Position(
                    coin=raw["coin"],
                    size=size,
                    entry_price=float(raw.get("entryPx", 0) or 0),
                    liquidation_price=float(liquidation) if liquidation else None,
                    unrealized_pnl=float(raw.get("unrealizedPnl", 0) or 0),
                    margin_used=float(raw.get("marginUsed", 0) or 0),
                    leverage=int(float(leverage.get("value", 1))),
                )
            )

        return AccountState(
            account_value=float(summary.get("accountValue", 0) or 0),
            withdrawable=float(data.get("withdrawable", 0) or 0),
            total_margin_used=float(summary.get("totalMarginUsed", 0) or 0),
            positions=tuple(positions),
        )

    async def spot_usdc(self, address: str) -> float:
        """USDC sitting in the spot wallet.

        Hyperliquid keeps spot and perps balances apart, and only the perps balance
        margins a trade. Funding the account and finding the bot still refuses every
        trade is a confusing place to end up, so this is read to say why.
        """
        data = await self._post({"type": "spotClearinghouseState", "user": address})
        for balance in data.get("balances", []) if isinstance(data, dict) else []:
            if balance.get("coin") == "USDC":
                return float(balance.get("total", 0) or 0)
        return 0.0

    async def user_fills(self, address: str) -> list[dict]:
        """Recent fills, newest first. How a live exit is discovered after the fact.

        The exchange triggers a stop on its own, so the only way to learn the price
        it actually got — and the realised profit — is to read back what filled.
        """
        data = await self._post({"type": "userFills", "user": address})
        return data if isinstance(data, list) else []

    async def open_orders(self, address: str) -> list[dict]:
        """Resting orders, including trigger orders, with their trigger prices.

        Used to recover a position's stop and target after a restart: the exchange
        holds them, so they do not need storing locally.
        """
        data = await self._post({"type": "frontendOpenOrders", "user": address})
        return data if isinstance(data, list) else []
