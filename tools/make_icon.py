"""Render the app icon from the same source the UI draws its logo from.

    .\\venv\\Scripts\\python.exe tools\\make_icon.py

Writes `assets/hypertrade.ico`, which the build embeds in the executable and the
window uses for its title bar and taskbar entry.

The logo is not an image file anywhere in this project — the sidebar draws it as a
Font Awesome bolt in the brand teal, so an icon exported by hand would be a second
copy free to drift from it. This renders the same glyph in the same colour, which
means the icon changes when the brand does and cannot fall out of step on its own.

Committed rather than generated during the build, so packaging needs no display and
the exact bytes that ship are the ones in the repository.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_API", "pyside6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.path.isdir(r"C:\Windows\Fonts"):
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")

#: Every size Windows picks between — the taskbar, alt-tab, the desktop and the
#: file properties dialog all ask for different ones, and a single-size icon gets
#: scaled into mush at the rest.
SIZES = [16, 24, 32, 48, 64, 128, 256]
MASTER = 512  # rendered once at this size, then downscaled by Pillow

OUT = ROOT / "assets" / "hypertrade.ico"


def main() -> int:
    import io

    import qtawesome as qta
    from PIL import Image
    from PySide6.QtCore import QBuffer, QByteArray
    from PySide6.QtWidgets import QApplication

    from src.ui import theme

    app = QApplication.instance() or QApplication([])  # noqa: F841 — qtawesome needs one

    pixmap = qta.icon("fa6s.bolt", color=theme.BRAND).pixmap(MASTER, MASTER)

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QBuffer.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    buffer.close()

    image = Image.open(io.BytesIO(bytes(data))).convert("RGBA")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    image.save(OUT, format="ICO", sizes=[(size, size) for size in SIZES])

    print(f"{OUT.relative_to(ROOT)}  {OUT.stat().st_size:,} bytes")
    print(f"sizes: {', '.join(f'{s}x{s}' for s in SIZES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
