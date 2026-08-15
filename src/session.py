"""Assembling a running bot, in one place.

The desktop app and the console runner both need the same four objects wired the same
way: a connection to Hyperliquid, the asset's constraints, a broker, and an engine
holding a strategy. Building that twice is how the two quietly drift apart — a new
setting gets honoured in the window and ignored in the terminal.

This is also where Paper and Live are chosen, and where Live **fails closed**: if the
key is missing, the address is wrong, or the exchange cannot be reached, the session
comes up in Paper mode with the reason recorded, rather than half-configured against
real money.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3

from . import secrets_store
from .broker.base import Broker
from .broker.live import LiveBroker
from .broker.paper import PaperBroker
from .config import AppSettings
from .core.models import AssetMeta
from .data.calendar import EconomicCalendar
from .data.hl_info import HyperliquidInfo
from .engine import DEFAULT_POLL_SECONDS, BotEngine
from .strategy import TrendFollowing

log = logging.getLogger(__name__)


class Session:
    """One live wiring of settings to a broker and an engine."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: AppSettings,
        info: HyperliquidInfo,
        asset: AssetMeta,
        *,
        poll_seconds: float = DEFAULT_POLL_SECONDS,
    ) -> None:
        self._conn = conn
        self._poll_seconds = poll_seconds
        self.settings = settings
        self.info = info
        self.asset = asset
        #: Shared across re-wirings so a settings change does not throw away the
        #: cached calendar and refetch it. Backed by the database, so a restart does
        #: not either — the feed rate-limits, and the blackout fails closed.
        self.calendar = EconomicCalendar(conn=conn)
        #: Why Live was refused, when it was. None when the mode is as requested.
        self.fell_back_to_paper: str | None = None
        self.broker: Broker
        self.engine: BotEngine

    async def wire(self) -> None:
        self.broker = await self._build_broker()
        self.engine = BotEngine(
            settings=self.settings,
            info=self.info,
            broker=self.broker,
            strategy=TrendFollowing(),
            asset=self.asset,
            conn=self._conn,
            calendar=self.calendar,
            poll_seconds=self._poll_seconds,
        )

    async def apply_settings(self, settings: AppSettings) -> None:
        """Re-wire for changed settings, keeping the connection and the asset meta.

        A paper broker reloads its persisted state, so an open simulated position and
        its balance survive a settings change.
        """
        self.settings = settings
        await self.wire()

    async def aclose(self) -> None:
        if self.engine.is_running:
            await self.engine.stop()
        await self.calendar.aclose()
        await self.info.aclose()

    # --- brokers ---------------------------------------------------------

    async def _build_broker(self) -> Broker:
        self.fell_back_to_paper = None
        if not self.settings.is_live:
            return self._paper_broker()

        try:
            broker = await self._live_broker()
        except Exception as exc:  # noqa: BLE001 — every failure means the same thing
            # Fail closed. A half-built live broker is worse than no live broker:
            # nothing has been ordered yet, and this way nothing will be.
            log.error("Live mode refused, falling back to Paper: %s", exc)
            self.fell_back_to_paper = str(exc)
            return self._paper_broker()

        log.warning(
            "LIVE MODE on %s - orders will spend real USDC from %s",
            self.settings.network.value, self.settings.account_address,
        )
        return broker

    def _paper_broker(self) -> PaperBroker:
        return PaperBroker(
            self.settings.coin,
            self.asset,
            self.settings.paper_starting_balance,
            slippage=self.settings.slippage,
            conn=self._conn,
        )

    async def _live_broker(self) -> LiveBroker:
        from eth_account import Account
        from hyperliquid.exchange import Exchange

        key = secrets_store.load_agent_key()
        if key is None:
            raise RuntimeError(
                "no API wallet key found. Approve an agent on Hyperliquid and paste "
                "its key in Settings."
            )

        wallet = Account.from_key(key)
        # Constructing an Exchange fetches the asset universe over the network, so it
        # is done off the event loop like every other SDK call.
        exchange = await asyncio.to_thread(
            Exchange,
            wallet,
            self.settings.network.api_url,
            None,
            None,
            self.settings.account_address,
        )

        # Prove the address is real, reachable and able to margin a trade before
        # anything can be ordered against it. A typo here would otherwise surface as
        # a failed order, and an empty perps wallet as a bot that silently rejects
        # every trade forever.
        state = await self.info.clearinghouse_state(self.settings.account_address)
        if state.account_value <= 0:
            spot = await self.info.spot_usdc(self.settings.account_address)
            if spot > 0:
                raise RuntimeError(
                    f"the perps wallet is empty, but {spot:,.2f} USDC is sitting in "
                    f"spot. Hyperliquid keeps the two apart and only the perps "
                    f"balance margins a trade - transfer it to Perps first."
                )
            raise RuntimeError(
                f"{self.settings.account_address} holds no USDC on "
                f"{self.settings.network.value}."
            )

        log.info(
            "live account %s: %.2f USDC, %d open position(s)",
            self.settings.account_address, state.account_value, len(state.positions),
        )

        return LiveBroker(
            self.settings.coin,
            self.asset,
            exchange=exchange,
            info=self.info,
            account_address=self.settings.account_address,
            slippage=self.settings.slippage,
            conn=self._conn,
        )


async def open_session(
    conn: sqlite3.Connection,
    settings: AppSettings,
    *,
    poll_seconds: float = DEFAULT_POLL_SECONDS,
) -> Session:
    """Connect, read the asset's constraints, and wire everything together."""
    info = HyperliquidInfo(settings.network)
    asset = await info.asset_meta(settings.coin)
    log.info(
        "%s: szDecimals=%d, max leverage %dx",
        asset.name, asset.sz_decimals, asset.max_leverage,
    )
    session = Session(conn, settings, info, asset, poll_seconds=poll_seconds)
    await session.wire()
    return session
