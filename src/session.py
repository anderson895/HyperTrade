"""Assembling a running bot, in one place.

The desktop app and the console runner both need the same four objects wired the same
way: a connection to Hyperliquid, the asset's constraints, a broker, and an engine
holding a strategy. Building that twice is how the two quietly drift apart — a new
setting gets honoured in the window and ignored in the terminal.

This is also where the Paper/Live choice will be made once live execution exists, so
that decision lives at one address rather than at every call site.
"""

from __future__ import annotations

import logging
import sqlite3

from .broker.paper import PaperBroker
from .config import AppSettings
from .core.models import AssetMeta
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
        self.broker: PaperBroker
        self.engine: BotEngine
        self._wire()

    def _wire(self) -> None:
        self.broker = PaperBroker(
            self.settings.coin,
            self.asset,
            self.settings.paper_starting_balance,
            slippage=self.settings.slippage,
            conn=self._conn,
        )
        self.engine = BotEngine(
            settings=self.settings,
            info=self.info,
            broker=self.broker,
            strategy=TrendFollowing(),
            asset=self.asset,
            conn=self._conn,
            poll_seconds=self._poll_seconds,
        )

    def apply_settings(self, settings: AppSettings) -> None:
        """Re-wire for changed settings, keeping the connection and the asset meta.

        The broker reloads its persisted state, so an open paper position and its
        balance survive a settings change.
        """
        self.settings = settings
        self._wire()

    async def aclose(self) -> None:
        if self.engine.is_running:
            await self.engine.stop()
        await self.info.aclose()


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
    return Session(conn, settings, info, asset, poll_seconds=poll_seconds)
