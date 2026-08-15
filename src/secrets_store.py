"""Private key storage, backed by Windows Credential Manager via `keyring`.

HyperTrade only ever holds a Hyperliquid **API wallet (agent) key**. An agent can
place and cancel orders but cannot withdraw or transfer funds, so a leaked agent key
cannot drain the account — unlike the main wallet key, which is never entered into
this app at all.

Keys are never written to the SQLite database, to `data/`, or to any file.
"""

from __future__ import annotations

import logging
import re

import keyring

log = logging.getLogger(__name__)

SERVICE_NAME = "HyperTrade"
AGENT_KEY_ENTRY = "agent_private_key"

PRIVATE_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")


def normalise_private_key(raw: str) -> str:
    """Trim, add the `0x` prefix if the user pasted it without one, and validate.

    Raises `ValueError` with a message safe to show in the UI — it must never echo
    the key back.
    """
    candidate = (raw or "").strip()
    if not candidate:
        raise ValueError("No private key was entered.")
    if not candidate.startswith("0x"):
        candidate = "0x" + candidate

    if ADDRESS_RE.match(candidate):
        # Approving an API wallet shows both its address and its key, and the two
        # get copied in the wrong order often enough to name the mistake.
        raise ValueError(
            "That is a wallet address, not a private key. Approving an API wallet "
            "shows both - the key is the longer one, 64 characters."
        )
    if not PRIVATE_KEY_RE.match(candidate):
        raise ValueError(
            "That does not look like a private key. Expected 64 hexadecimal "
            "characters, optionally prefixed with 0x."
        )
    return candidate


def save_agent_key(raw: str) -> None:
    """Validate and store the agent key. Returns nothing; raises on a bad key."""
    keyring.set_password(SERVICE_NAME, AGENT_KEY_ENTRY, normalise_private_key(raw))
    log.info("API wallet key saved to Windows Credential Manager")


def load_agent_key() -> str | None:
    try:
        return keyring.get_password(SERVICE_NAME, AGENT_KEY_ENTRY)
    except keyring.errors.KeyringError:
        # A locked or misconfigured credential store must not crash startup; the
        # caller falls back to Paper mode.
        log.exception("could not read the API wallet key from the credential store")
        return None


def has_agent_key() -> bool:
    return load_agent_key() is not None


def delete_agent_key() -> None:
    try:
        keyring.delete_password(SERVICE_NAME, AGENT_KEY_ENTRY)
        log.info("API wallet key removed from Windows Credential Manager")
    except keyring.errors.PasswordDeleteError:
        pass  # already absent
