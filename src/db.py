"""SQLite storage shared by settings, trades, and logs.

Schema changes are append-only entries in `_MIGRATIONS`; `PRAGMA user_version` tracks
which have run, so an existing install upgrades in place instead of losing its
history.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .paths import db_path

_MIGRATIONS: list[str] = [
    # v1 — settings key/value store
    """
    CREATE TABLE IF NOT EXISTS settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
    # v2 — paper account ledger and the fill history behind the Trades page
    """
    CREATE TABLE IF NOT EXISTS paper_state (
        id                INTEGER PRIMARY KEY CHECK (id = 1),
        balance           REAL    NOT NULL,
        leverage          INTEGER NOT NULL DEFAULT 1,
        coin              TEXT,
        side              TEXT,
        size              REAL,
        entry_price       REAL,
        stop_price        REAL,
        take_profit_price REAL,
        entry_time_ms     INTEGER,
        entry_fee         REAL NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS fills (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        time_ms      INTEGER NOT NULL,
        mode         TEXT    NOT NULL,
        coin         TEXT    NOT NULL,
        side         TEXT    NOT NULL,
        size         REAL    NOT NULL,
        price        REAL    NOT NULL,
        fee          REAL    NOT NULL,
        reason       TEXT    NOT NULL,
        realised_pnl REAL,
        note         TEXT
    );

    CREATE INDEX IF NOT EXISTS fills_time ON fills (time_ms);
    """,
]


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    """Open the database, applying any pending migrations.

    Pass `":memory:"` in tests.
    """
    conn = sqlite3.connect(path if path is not None else db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


def get_ui_state(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    """Small UI preferences (sidebar collapsed, and the like).

    Namespaced under `ui.` in the same table as settings; `load_settings` only reads
    keys that match an `AppSettings` field, so these ride along untouched.
    """
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (f"ui.{key}",)).fetchone()
    return row["value"] if row else default


def set_ui_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (f"ui.{key}", str(value)),
    )
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for index, script in enumerate(_MIGRATIONS[version:], start=version):
        conn.executescript(script)
        conn.execute(f"PRAGMA user_version = {index + 1}")
    conn.commit()
