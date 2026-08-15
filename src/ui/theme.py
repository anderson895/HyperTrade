"""Dark theme (QSS).

Same structure as PolyTrade Pro — property-based selectors (`card`, `muted`, `h1`,
`h2`, `accent`) so the widgets stay free of inline colour — with a different palette.
PolyTrade is indigo on navy; HyperTrade is teal on charcoal, which matches
Hyperliquid's own mint accent and makes the two apps obvious at a glance.
"""

# Palette
BG = "#0c1116"
CARD = "#141b22"
BORDER = "#1e2831"
INPUT_BG = "#0f161d"
TEXT = "#e6edf3"
MUTED = "#8b98a5"
ACCENT = "#2dd4bf"
ACCENT_DIM = "#0d3b36"
ACCENT_TEXT = "#a7f3e4"
GREEN = "#22c55e"
RED = "#ef4444"
AMBER = "#f59e0b"
BTC_ORANGE = "#f7931a"

# Moving averages. Kept clear of the candle greens and reds and of the accent, so a
# crossing reads at a glance rather than blending into the bars behind it.
EMA_FAST = "#fbbf24"
EMA_SLOW = "#a78bfa"

STYLESHEET = f"""
QMainWindow, QWidget {{
    background: {BG};
    color: {TEXT};
    font-family: 'Segoe UI';
    font-size: 13px;
}}
QLabel {{ background: transparent; border: none; }}
QLabel[muted="true"] {{ color: {MUTED}; }}
QLabel[h1="true"] {{ font-size: 26px; font-weight: bold; }}
QLabel[h2="true"] {{ font-size: 16px; font-weight: bold; }}
QLabel[accent="true"] {{ color: {ACCENT}; font-weight: bold; font-size: 14px; }}

QFrame[card="true"] {{
    background: {CARD};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}

QListWidget#sidebar {{
    background: {BG};
    border: none;
    outline: none;
    font-size: 14px;
}}
QListWidget#sidebar::item {{
    padding: 10px 14px;
    border-radius: 6px;
    margin: 2px 8px;
    color: {MUTED};
}}
/* Collapsed (icon-only): tighter padding so the icons stay centred. */
QListWidget#sidebar[collapsed="true"]::item {{
    padding: 10px 6px;
    margin: 2px 4px;
}}
QListWidget#sidebar::item:hover {{ background: {CARD}; }}
QListWidget#sidebar::item:selected {{
    background: {ACCENT_DIM};
    color: {ACCENT_TEXT};
}}

/* Minimum heights everywhere below are load-bearing, not decoration. A form taller
   than its scroll area does not scroll unless its contents refuse to shrink; without
   them Qt squeezed the Settings buttons from 35px down to 19px and clipped their
   labels rather than showing a scrollbar. */
QPushButton {{
    background: {BORDER};
    border: 1px solid #2a3a46;
    border-radius: 6px;
    padding: 8px 14px;
    min-height: 18px;
    color: {TEXT};
}}
QPushButton:hover {{ background: #2a3a46; }}
QPushButton:disabled {{ color: #5b6b78; }}
QPushButton#startBtn {{
    background: #15a06f; color: white;
    font-weight: bold; font-size: 15px; padding: 10px 18px;
    border: none;
    border-radius: 6px;
}}
QPushButton#startBtn:hover {{ background: #17b87e; }}
QPushButton#startBtn:disabled {{ background: #17302a; color: #5b6b78; }}
QPushButton#stopBtn {{
    background: transparent; color: {RED};
    font-weight: bold; font-size: 15px; padding: 10px 18px;
    border: 1px solid {RED};
    border-radius: 6px;
}}
QPushButton#stopBtn:hover {{ background: #3a1518; }}
QPushButton#stopBtn:disabled {{ color: #5b6b78; border-color: #3a4550; }}

QComboBox, QLineEdit, QDoubleSpinBox, QSpinBox {{
    background: {INPUT_BG};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 7px 9px;
    min-height: 20px;
    color: {TEXT};
    selection-background-color: {ACCENT_DIM};
}}
QComboBox:focus, QLineEdit:focus, QDoubleSpinBox:focus, QSpinBox:focus {{
    border-color: {ACCENT};
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {CARD};
    border: 1px solid {BORDER};
    selection-background-color: {ACCENT_DIM};
    selection-color: {ACCENT_TEXT};
    outline: none;
}}
/* The spin buttons are deliberately left unstyled. Overriding their background
   without supplying arrow images leaves a blank grey block where the arrows should
   be, and Qt's CSS border-triangle trick does not render reliably. Native it is. */

/* Transparent, or the base QWidget colour paints a dark slab across the card behind
   it and the row reads as an input field. */
QCheckBox {{ spacing: 8px; background: transparent; }}
QCheckBox::indicator {{
    width: 16px; height: 16px;
    border: 1px solid {BORDER};
    border-radius: 4px;
    background: {INPUT_BG};
}}
QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}

QTableWidget {{
    background: {CARD};
    alternate-background-color: {INPUT_BG};
    gridline-color: {BORDER};
    border: 1px solid {BORDER};
    border-radius: 8px;
    outline: none;
}}
QTableWidget::item {{ padding: 6px; }}
QTableWidget::item:selected {{ background: {ACCENT_DIM}; color: {ACCENT_TEXT}; }}
QHeaderView::section {{
    background: {INPUT_BG};
    color: {MUTED};
    border: none;
    border-bottom: 1px solid {BORDER};
    padding: 8px;
    font-weight: bold;
}}

QListWidget {{
    background: transparent;
    border: none;
    outline: none;
}}
QListWidget::item {{ padding: 4px 2px; }}

QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: #2a3a46; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: #354855; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{ background: #2a3a46; border-radius: 5px; min-width: 24px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QToolTip {{
    background: {CARD};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px;
}}
"""
