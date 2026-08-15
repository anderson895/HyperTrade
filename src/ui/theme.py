"""Dark theme (QSS).

Same structure as PolyTrade Pro — property-based selectors (`card`, `muted`, `h1`,
`h2`, `accent`) so the widgets stay free of inline colour — with a different palette.
PolyTrade is indigo on navy; HyperTrade is teal on charcoal, which matches
Hyperliquid's own mint accent and makes the two apps obvious at a glance.
"""

# Palette, sampled from the reference mockup rather than guessed at.
BG = "#050913"
CARD = "#0A101D"
BORDER = "#1A2032"
INPUT_BG = "#080C16"
TEXT = "#e6edf3"
MUTED = "#7d8aa0"
ACCENT = "#01DE7D"
#: The top bar runs a cyan-to-green ramp: the wordmark's bolt is the cyan end,
#: the page icon and title a mint in the middle, and only state indicators use
#: the full ACCENT green. Sampled off the mockup's glyph cores, not guessed.
BRAND = "#04DEC6"
BRAND_MINT = "#0EDEB3"
ACCENT_DIM = "#0a2e21"
ACCENT_TEXT = "#7df0b8"
GREEN = "#01DE7D"
RED = "#ef4444"
#: Body text inside a red callout. Full RED on a tinted panel vibrates against it.
RED_TEXT = "#f8b4b4"
RED_DIM = "#2a1216"
AMBER = "#f59e0b"
BTC_ORANGE = "#f7931a"

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
    border-radius: 12px;
}}
/* The top bar sits level with the page it heads rather than above it — the bottom
   border is what separates them. Lifting it to CARD made the bar read as a floating
   panel, which the reference design does not do. */
QFrame[topbar="true"] {{
    background: {BG};
    border: none;
    border-bottom: 1px solid {BORDER};
}}
QFrame[statusbar="true"] {{
    background: {INPUT_BG};
    border: none;
    border-top: 1px solid {BORDER};
}}
QFrame[pill="true"] {{
    background: {ACCENT_DIM};
    border: 1px solid {ACCENT};
    border-radius: 12px;
}}
QFrame[notebox="true"] {{
    background: {ACCENT_DIM};
    border: 1px solid {ACCENT};
    border-radius: 8px;
}}
QFrame[errorbox="true"] {{
    background: {RED_DIM};
    border: 1px solid {RED};
    border-radius: 8px;
}}
QLabel[h3="true"] {{ font-size: 15px; font-weight: bold; }}
QLabel[pagetitle="true"] {{ font-size: 24px; font-weight: bold; }}

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
QPushButton#accentBtn {{
    background: {ACCENT}; color: #04140c;
    font-weight: bold;
    border: none;
    border-radius: 6px;
    padding: 8px 16px;
    min-height: 18px;
}}
QPushButton#accentBtn:hover {{ background: #2ef094; }}
QPushButton#accentBtn:disabled {{ background: {ACCENT_DIM}; color: #4a5568; }}

QPushButton#startBtn {{
    background: {ACCENT}; color: #04140c;
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
