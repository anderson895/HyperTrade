"""Launcher for the packaged build.

PyInstaller freezes whichever script it is given as a *top-level* module — it does
not keep that file's package. Freezing `src/main.py` directly therefore produced a
bundle where `main` sat at the top level and every `from .engine import ...` in it
died with `attempted relative import with no known parent package`, before anything
else ran.

Freezing this instead keeps `src` a real package inside the bundle: nothing here is
relative, and importing `src.main` is what pulls the package in whole.

Not needed from source, where `python -m src.main` already does the right thing.
"""

from __future__ import annotations

from src.main import main

if __name__ == "__main__":
    raise SystemExit(main())
