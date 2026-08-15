"""Application logging: rotating file, console, and an optional in-app sink.

Every handler formats through `RedactingFormatter`, so a private key cannot reach
`data/app.log` even if it slips into an exception message — and that file is the one
users are asked to send when something breaks.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .paths import log_path

LOG_FORMAT = "%(asctime)s  %(levelname)-7s %(name)-22s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5

_PRIVATE_KEY_RE = re.compile(r"\b0x[0-9a-fA-F]{64}\b")
_ADDRESS_RE = re.compile(r"\b0x([0-9a-fA-F]{4})[0-9a-fA-F]{32}([0-9a-fA-F]{4})\b")


def redact(text: str) -> str:
    """Strip secrets from a rendered log line.

    Private keys are removed outright. Addresses are shortened to `0x1234...abcd` —
    still enough to tell two wallets apart in a support thread, without publishing
    the user's full address.
    """
    text = _PRIVATE_KEY_RE.sub("0x<redacted-private-key>", text)
    return _ADDRESS_RE.sub(r"0x\1...\2", text)


class RedactingFormatter(logging.Formatter):
    """Redacts the fully rendered record, tracebacks included."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


@dataclass(frozen=True)
class LogLine:
    """One log event, already redacted, split up for the UI's table and list."""

    time: str
    level: str
    levelno: int
    message: str
    formatted: str


class CallbackHandler(logging.Handler):
    """Feeds redacted log lines to a callable — used by the UI's log panels.

    Redaction happens here rather than in the consumer: the UI must never receive a
    secret it could then render, and there is exactly one place to get that right.
    Deliberately not Qt-aware so the engine can be driven headless in tests.
    """

    def __init__(self, callback: Callable[[LogLine], None], level: int = logging.INFO):
        super().__init__(level)
        self._callback = callback

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._callback(
                LogLine(
                    time=datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                    level=record.levelname,
                    levelno=record.levelno,
                    message=redact(record.getMessage()),
                    formatted=self.format(record),
                )
            )
        except Exception:  # noqa: BLE001 — a broken UI sink must not kill the bot
            self.handleError(record)


def setup_logging(
    level: int = logging.INFO,
    *,
    to_console: bool = True,
    file_path: Path | None = None,
) -> logging.Logger:
    """Configure the root logger. Safe to call more than once."""
    formatter = RedactingFormatter(LOG_FORMAT, datefmt=DATE_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    file_handler = RotatingFileHandler(
        file_path or log_path(),
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    if to_console:
        stream = sys.stderr
        # Windows consoles default to a legacy code page, which turns any non-ASCII
        # in a log line into mojibake once the output is piped. Ask for UTF-8 and
        # carry on quietly if the stream will not take it.
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass
        console = logging.StreamHandler(stream)
        console.setFormatter(formatter)
        root.addHandler(console)

    # These log every request at DEBUG, which buries the bot's own decisions.
    for noisy in ("httpx", "httpcore", "websockets", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    return root


def add_ui_sink(
    callback: Callable[[LogLine], None], level: int = logging.INFO
) -> CallbackHandler:
    handler = CallbackHandler(callback, level)
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, datefmt=DATE_FORMAT))
    logging.getLogger().addHandler(handler)
    return handler
