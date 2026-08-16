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
import inspect
import logging
import sqlite3

from . import secrets_store
from .broker.base import Broker, now_ms
from .broker.live import LiveBroker
from .broker.paper import PaperBroker
from .config import AppSettings
from .core.models import AssetMeta
from .data.calendar import EconomicCalendar
from .data.hl_info import HyperliquidInfo
from .engine import DEFAULT_POLL_SECONDS, BotEngine
from .strategy import Strategy, available, create

log = logging.getLogger(__name__)

#: Used when the saved strategy cannot be built. Matches `AppSettings.strategy`.
DEFAULT_STRATEGY = "volume_rejection"


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
            strategy=self._build_strategy(),
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

    def _build_strategy(self) -> Strategy:
        """The chosen strategy, given the exit settings it understands.

        Only parameters the strategy actually declares are passed. A strategy that
        does not take one of these would raise TypeError rather than quietly ignore
        it, which is the point — a setting the user can edit and that reaches
        nothing is worse than no setting.
        """
        settings = self.settings
        wanted = {
            "take_profit_rr": settings.take_profit_rr,
            "stop_buffer": settings.stop_buffer_pct,
        }
        accepted = inspect.signature(
            available()[settings.strategy].__init__
        ).parameters if settings.strategy in available() else {}
        shared = {key: value for key, value in wanted.items() if key in accepted}

        try:
            return create(settings.strategy, **shared)
        except (KeyError, TypeError) as exc:
            # A saved config naming a strategy this build no longer has, or one
            # whose parameters have changed. Falling back is better than refusing
            # to start, but it must be loud: the bot would otherwise trade a system
            # the user did not choose.
            log.error(
                "could not build strategy %r (%s) - falling back to %s",
                settings.strategy, exc, DEFAULT_STRATEGY,
            )
            return create(DEFAULT_STRATEGY)

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

        # Prove the key can actually act for this account, before anything can be
        # ordered with it. Reading a balance needs only an address, so up to here a
        # wrong key looks identical to a right one: the account value comes back,
        # the connection reads as healthy, and nothing exercises the key until the
        # first real order — which is the worst possible moment to find out.
        owner = await self.info.agent_owner(wallet.address)
        if owner is None:
            raise RuntimeError(
                f"the API wallet key is not an approved agent on "
                f"{self.settings.network.value}. It derives to {wallet.address}, "
                f"which Hyperliquid does not recognise as an agent for any account. "
                f"Approve it in the Hyperliquid app, or paste the right key."
            )
        if owner.lower() != self.settings.account_address.lower():
            raise RuntimeError(
                f"the API wallet key belongs to a different account. It derives to "
                f"{wallet.address}, which is an approved agent for {owner} — not for "
                f"{self.settings.account_address}. Orders signed with it would be "
                f"rejected."
            )

        # Approvals lapse. When one does the key stops being able to order while
        # everything else still looks fine, which is a wrong key arriving on a
        # schedule — so it is worth saying before it happens, not after.
        expiries = await self.info.agent_expiries(self.settings.account_address)
        expires_ms = expiries.get(wallet.address.lower())
        if expires_ms:
            days = (expires_ms - now_ms()) / 86_400_000
            if days <= 0:
                raise RuntimeError(
                    f"the API wallet approval expired. Re-approve {wallet.address} "
                    f"in the Hyperliquid app."
                )
            if days <= 14:
                log.warning(
                    "the API wallet approval expires in %.0f day(s) - re-approve "
                    "%s before it lapses", days, wallet.address,
                )

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
