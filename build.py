"""Package HyperTrade as a Windows folder build, then zip it for release.

    .\\venv\\Scripts\\python.exe build.py

Produces `dist/HyperTrade/HyperTrade.exe` and `dist/HyperTrade-<version>-windows.zip`.

A folder build rather than a single file, deliberately. One-file PyInstaller
unpacks the whole ~200 MB payload to a temporary directory on *every* launch, which
on a PySide6 app is ten to thirty seconds of nothing happening before the window
appears — indistinguishable from a program that failed to start. The folder starts
in two or three seconds and is still portable: `paths.py` puts `data/` beside the
executable, so the whole thing can be unzipped anywhere and moved.

What PyInstaller does not find on its own is **qtawesome's fonts**: the icons are a
font file loaded at runtime by name, so nothing imports it and the analysis never
sees it. Missing, every icon in the app renders as an empty box. Same story for
Qt's own platform plugin, which is loaded by the Qt runtime rather than imported.

`verify_build` checks the result instead of trusting it, and the check that matters
most is the last one: it runs the packaged executable through `--console --once`,
which does a full startup, one poll of the exchange and an orderly exit. A bundle
missing a module still builds, still produces an exe, and still passes any check
that only asks whether files exist.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from src import __version__  # noqa: E402

NAME = "HyperTrade"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
BUNDLE = DIST / NAME
ARCHIVE = DIST / f"{NAME}-{__version__}-windows.zip"

#: `hypertrade.py`, not `src/main.py`. PyInstaller freezes its entry script as a
#: top-level module and does not keep that file's package, so freezing src/main.py
#: produced a bundle whose relative imports could not resolve. See that file.
ENTRY = ROOT / "hypertrade.py"

#: Regenerate with `tools/make_icon.py` — it renders the same bolt the sidebar draws.
ICON = ROOT / "assets" / "hypertrade.ico"

#: Imported by name at runtime or reached through data files, so static analysis
#: cannot see them.
HIDDEN_IMPORTS = [
    "qasync",
    "keyring.backends.Windows",  # chosen at runtime by platform
    "hyperliquid.exchange",
    "hyperliquid.info",
    "hyperliquid.utils.signing",
    "eth_account",
]

#: Packages whose data files matter, not just their modules. qtawesome's icons are
#: a font loaded by name at runtime — nothing imports it, so nothing finds it.
#: (The hyperliquid SDK was on this list on the assumption that it read its EIP-712
#: types from bundled JSON. It does not: it ships no data files at all and the types
#: are literals in `utils/signing.py`. Checking for them failed the build over an
#: absence that was always there.)
COLLECT_DATA = ["qtawesome"]

#: PyQt6 is installed here as a dependency of pyqtgraph, and qasync and qtawesome
#: both support every binding, so the analysis reaches it and PyInstaller refuses to
#: bundle two Qt packages at once. The app forces PySide6 (`QT_API=pyside6` in
#: `ui/app.py`) and never touches the others, so they are excluded rather than
#: chosen between — leaving them in would also add a second ~100 MB copy of Qt.
EXCLUDES = ["PyQt6", "PyQt6.sip", "PyQt5", "PySide2", "tkinter"]


def run_pyinstaller() -> None:
    for stale in (BUILD, DIST):
        shutil.rmtree(stale, ignore_errors=True)

    command = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--windowed",  # no console window behind the app
        "--name", NAME,
        "--distpath", str(DIST),
        "--workpath", str(BUILD),
        "--specpath", str(BUILD),
    ]
    for module in HIDDEN_IMPORTS:
        command += ["--hidden-import", module]
    for package in COLLECT_DATA:
        command += ["--collect-data", package]
    for package in EXCLUDES:
        command += ["--exclude-module", package]
    if ICON.is_file():
        command += ["--icon", str(ICON)]
    else:
        raise SystemExit(f"no icon at {ICON} - run tools\\make_icon.py first")
    command.append(str(ENTRY))

    print(" ".join(command), flush=True)
    subprocess.run(command, check=True)

    # Beside the executable, not inside `_internal`, because `app_dir()` resolves to
    # the executable's own folder — the same rule that keeps `data/` portable.
    assets = BUNDLE / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ICON, assets / ICON.name)


def verify_build() -> None:
    """Fail here rather than on the client's machine.

    Two file checks for the things Qt loads by name rather than by import, then the
    one that actually proves something: running the packaged executable. A bundle
    with a missing module still builds and still produces an exe — it fails on the
    import, at launch, on someone else's computer.
    """
    exe = BUNDLE / f"{NAME}.exe"
    if not exe.is_file():
        raise SystemExit(f"build produced no executable at {exe}")

    internal = BUNDLE / "_internal"
    checks = {
        "qtawesome fonts": list(internal.glob("qtawesome/fonts/*.ttf")),
        "PySide6 platform plugin": list(internal.glob("PySide6/plugins/platforms/*.dll")),
    }
    missing = [name for name, found in checks.items() if not found]
    if missing:
        raise SystemExit(f"build is incomplete - missing: {', '.join(missing)}")
    for name, found in checks.items():
        print(f"  ok  {name}: {len(found)} file(s)")

    # A real start: loads every module, opens the database, reaches Hyperliquid for
    # one poll, then exits. It runs on the bundle's own `data/`, which is empty, so
    # the settings are defaults — Paper. Nothing here can touch a live account.
    print("  .. running the packaged exe (--console --once)", flush=True)
    log = BUNDLE / "data" / "app.log"
    try:
        result = subprocess.run(
            [str(exe), "--console", "--once"],
            cwd=str(BUNDLE),
            capture_output=True,
            text=True,
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        # A --windowed build reports an unhandled exception in a modal dialog and
        # then waits for someone to click Close, which nothing here will ever do.
        # So a hang is usually a crash, and the log says which.
        tail = (log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
                if log.is_file() else ["(no log written - it died before logging)"])
        raise SystemExit(
            "the packaged exe never exited. A windowed build shows unhandled "
            "exceptions in a dialog and waits, so this is usually a crash:\n  "
            + "\n  ".join(tail)
        ) from None

    if result.returncode != 0:
        tail = (log.read_text(encoding="utf-8", errors="replace").splitlines()[-15:]
                if log.is_file() else [])
        raise SystemExit(
            f"the packaged exe exited {result.returncode}\n"
            + (result.stderr or "")[-2000:]
            + "\n".join(tail)
        )
    if not log.is_file():
        raise SystemExit("the packaged exe ran but wrote no log - it did not get far")
    print(f"  ok  packaged exe started, polled and exited cleanly")


def make_archive() -> None:
    # `verify_build` ran the app, which created `data/` with a database and a log.
    # Shipping those would hand the client a settings file and a trade history from
    # this machine, and make their first run a continuation of a test rather than a
    # first run. The app recreates the folder on launch.
    shutil.rmtree(BUNDLE / "data", ignore_errors=True)

    ARCHIVE.unlink(missing_ok=True)
    with zipfile.ZipFile(ARCHIVE, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(BUNDLE.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(DIST))
    print(f"\n{ARCHIVE.name}  {ARCHIVE.stat().st_size / 1e6:,.1f} MB")


def main() -> int:
    print(f"Building {NAME} {__version__}\n")
    run_pyinstaller()
    print("\nVerifying the bundle:")
    verify_build()
    make_archive()
    print(f"\nUnzip and run {NAME}\\{NAME}.exe. `data/` is created beside it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
