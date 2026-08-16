"""Is this account ready to trade live? Read-only, changes nothing.

Everything that has to line up before a live run, checked in one place:

    .\\venv\\Scripts\\python.exe tools\\check_account.py
    .\\venv\\Scripts\\python.exe tools\\check_account.py --network testnet
    .\\venv\\Scripts\\python.exe tools\\check_account.py --address 0x... --risk 2 --leverage 5

Written after a live setup failed three separate ways at once - funds in the wrong
wallet, an agent approved on the wrong network, and a risk/leverage pair that could
never fit. Each one alone would have looked like "the bot just isn't trading".

Never prints the private key; only the address it derives to.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import secrets_store  # noqa: E402
from src.config import load_settings  # noqa: E402
from src.strategy import create  # noqa: E402
from src.core.models import Network, Side, Timeframe  # noqa: E402
from src.core.sizing import Rejection, plan_position  # noqa: E402
from src.data.hl_info import HyperliquidInfo  # noqa: E402
from src.db import connect  # noqa: E402

OK, BAD, WARN = "  OK  ", " FAIL ", " WARN "


def line(mark: str, text: str) -> None:
    print(f"[{mark}] {text}")


async def check(args) -> int:
    conn = connect()
    settings = load_settings(conn)
    conn.close()

    network = Network(args.network) if args.network else settings.network
    address = args.address or settings.account_address
    leverage = args.leverage if args.leverage is not None else settings.leverage
    timeframe = Timeframe(args.timeframe) if args.timeframe else settings.timeframe
    # `risk` is resolved once equity is known: a percentage setting has no fixed
    # USDC value. Reading `risk_usdc` directly reported the wrong size for every
    # percentage config — on a check whose entire job is to say what will happen.
    risk_override = args.risk

    print(f"network   : {network.value}")
    print(f"address   : {address or '(none)'}")
    risk_text = (
        f"risk {risk_override} USDC" if risk_override is not None
        else f"risk {settings.risk_pct:.1%} of equity" if settings.risk_pct
        else f"risk {settings.risk_usdc} USDC"
    )
    print(f"settings  : {timeframe.label}, {settings.strategy}, {risk_text}, "
          f"{leverage}x {settings.margin_mode.value}\n")

    failures = 0

    # --- the agent key ---------------------------------------------------
    key = secrets_store.load_agent_key()
    agent_address = None
    if key is None:
        line(BAD, "no API wallet key in the credential store - paste one in Settings")
        failures += 1
    else:
        from eth_account import Account

        agent_address = Account.from_key(key).address
        line(OK, f"API wallet key stored, signs as {agent_address}")

    if not address:
        line(BAD, "no main wallet address set - the bot has no account to trade")
        return failures + 1

    async with HyperliquidInfo(network) as info:
        # --- the account --------------------------------------------------
        state = await info.clearinghouse_state(address)
        spot = await info.spot_usdc(address)
        role = await info._post({"type": "userRole", "user": address})

        if role.get("role") == "missing":
            line(BAD, f"{address} does not exist on {network.value}")
            failures += 1
        else:
            line(OK, f"account exists on {network.value} (role: {role.get('role')})")

        if state.account_value > 0:
            line(OK, f"perps wallet holds {state.account_value:,.2f} USDC")
        elif spot > 0:
            line(
                BAD,
                f"perps wallet is empty but spot holds {spot:,.2f} USDC - "
                f"transfer it to Perps, only that balance margins a trade",
            )
            failures += 1
        else:
            line(BAD, f"no USDC on {network.value}, in either wallet")
            failures += 1

        # --- is the agent approved, here? ----------------------------------
        if agent_address:
            agent_role = await info._post({"type": "userRole", "user": agent_address})
            owner = agent_role.get("data", {}).get("user", "")
            if agent_role.get("role") != "agent":
                line(
                    BAD,
                    f"the stored key is not an approved agent on {network.value} - "
                    f"agents are per-network, so approve one here too",
                )
                failures += 1
            elif owner.lower() != address.lower():
                line(BAD, f"the agent belongs to {owner}, not {address}")
                failures += 1
            else:
                line(OK, "the stored key is an approved agent for this account")

        # --- can these settings produce a trade? ---------------------------
        asset = await info.asset_meta(settings.coin)
        candles = await info.recent_candles(settings.coin, timeframe, 200)
        # Ask the configured strategy how far its stop sits, rather than assuming
        # 2xATR: volume rejection puts its stop past a rejection wick, and sizing
        # against the wrong distance answers a question about a different bot.
        strategy = create(settings.strategy)
        distance = strategy.typical_stop_distance(candles[:-1])
        if distance and state.account_value > 0:
            risk = (
                risk_override if risk_override is not None
                else settings.risk_for(state.account_value)
            )
            entry = candles[-1].close
            plan = plan_position(
                side=Side.LONG, entry_price=entry, stop_price=entry - distance,
                risk_usdc=risk, equity_usdc=state.account_value,
                leverage=leverage, asset=asset,
                clamp_to_leverage=settings.clamp_size_to_leverage,
            )
            if isinstance(plan, Rejection):
                line(BAD, f"these settings cannot trade: {plan}")
                failures += 1
            else:
                line(
                    OK,
                    f"a trade would be {plan.size:g} {settings.coin} "
                    f"({plan.notional:,.0f} USDC notional, {plan.margin_required:,.0f} margin)",
                )
                if plan.risk_usdc < risk * 0.99:
                    line(
                        WARN,
                        f"the {leverage}x cap trims that trade to "
                        f"{plan.risk_usdc:.2f} USDC of risk "
                        f"({plan.risk_usdc / state.account_value:.2%} of equity), "
                        f"not the {risk:.2f} ({risk / state.account_value:.1%}) asked for",
                    )
        elif state.account_value > 0:
            line(WARN, f"{settings.strategy} cannot estimate a stop distance yet")
        else:
            line(WARN, "cannot check the settings until the perps wallet is funded")

        if timeframe in (Timeframe.M5, Timeframe.M15):
            line(WARN, f"{timeframe.label} backtested at a loss - see README.md")

    print()
    if failures:
        print(f"{failures} thing(s) to fix before this can trade.")
    else:
        print("Ready. Nothing here blocks a live run.")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--network", choices=["mainnet", "testnet"])
    parser.add_argument("--address")
    parser.add_argument("--risk", type=float)
    parser.add_argument("--leverage", type=int)
    parser.add_argument("--timeframe", choices=[tf.value for tf in Timeframe])
    return 1 if asyncio.run(check(parser.parse_args())) else 0


if __name__ == "__main__":
    raise SystemExit(main())
