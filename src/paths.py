"""Where the app keeps its runtime files.

`data/` sits next to the executable once frozen, and next to the source tree during
development, so a packaged copy stays self-contained and portable.
"""

from __future__ import annotations

import sys
from pathlib import Path


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    directory = app_dir() / "data"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def db_path() -> Path:
    return data_dir() / "hypertrade.db"


def log_path() -> Path:
    return data_dir() / "app.log"
