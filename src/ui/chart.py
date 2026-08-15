"""BTC price chart — candlesticks or a close line, with the position's levels.

Candles are the default, and not just out of convention: the stop is `2 x ATR`, and
ATR is built from the highs and lows. A close-only line hides the very thing that
decides where the stop goes, so anyone checking the bot's reasoning would be reading
the wrong picture.

Drawn with pyqtgraph rather than finplot. pyqtgraph has no candlestick primitive, so
there is a small GraphicsObject below — worth it to keep one charting library, one
set of axes, and the level lines working across both views.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

import pyqtgraph as pg
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFont, QPainter, QPicture

from ..core.models import Candle
from . import theme

BODY_WIDTH = 0.62

#: Live-tick view: samples kept, and how many of them are drawn. At roughly one
#: poll a second that is half an hour retained, five minutes shown.
TICK_BUFFER = 1_800
TICK_WINDOW = 300


class CandlestickItem(pg.GraphicsObject):
    """Paints OHLC bars once into a QPicture, then blits it on every redraw."""

    def __init__(self) -> None:
        super().__init__()
        self._picture = QPicture()
        self._rect = QRectF()

    def set_candles(self, candles: Sequence[Candle]) -> None:
        picture = QPicture()
        painter = QPainter(picture)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        for index, candle in enumerate(candles):
            rising = candle.close >= candle.open
            colour = theme.GREEN if rising else theme.RED
            painter.setPen(pg.mkPen(colour, width=1))

            # The wick first, so the body paints over its middle.
            painter.drawLine(
                pg.QtCore.QPointF(index, candle.low),
                pg.QtCore.QPointF(index, candle.high),
            )

            body = QRectF(
                index - BODY_WIDTH / 2,
                min(candle.open, candle.close),
                BODY_WIDTH,
                abs(candle.close - candle.open),
            )
            if body.height() == 0:
                # A doji has no body to fill; draw the open/close as a flat line.
                painter.drawLine(body.topLeft(), body.topRight())
            else:
                painter.setBrush(pg.mkBrush(colour))
                painter.drawRect(body)

        painter.end()
        self._picture = picture
        self._rect = QRectF(picture.boundingRect())
        self.prepareGeometryChange()
        self.informViewBoundsChanged()
        self.update()

    def paint(self, painter: QPainter, *args: object) -> None:
        painter.drawPicture(0, 0, self._picture)

    def boundingRect(self) -> QRectF:
        return self._rect


class PriceChart(pg.PlotWidget):
    def __init__(self) -> None:
        super().__init__(background=theme.CARD)
        pg.setConfigOptions(antialias=True)

        self.showGrid(x=False, y=True, alpha=0.12)
        # Tick and widget fonts are set in points on purpose. The stylesheet sizes
        # text in pixels, and a pixel-sized QFont reports pointSize() == -1.
        #
        # This does NOT silence Qt's "QFont::setPointSize: Point size <= 0 (-1)"
        # warning on startup — traced with qInstallMessageHandler, that one is
        # emitted from inside app.exec() with no Python frame above it, so it comes
        # from pyqtgraph's C++ layer and cannot be fixed from here. It is printed to
        # stderr once per launch and never reaches data/app.log, which is the file
        # users are asked to send.
        tick_font = QFont("Segoe UI", 9)
        self.setFont(tick_font)
        for name in ("left", "bottom"):
            axis = self.getAxis(name)
            axis.setTextPen(theme.MUTED)
            axis.setPen(theme.BORDER)
            axis.setTickFont(tick_font)
        self.setMenuEnabled(False)
        # No dragging, and no auto-range button offering to undo it. The range
        # selector is the view control; `_redraw` re-applies its range on every
        # live price, once a second, so a pan was discarded before the hand left
        # the mouse. An interaction that visibly does nothing is worse than one
        # that is not offered.
        self.setMouseEnabled(x=False, y=False)
        self.plotItem.hideButtons()

        self._candlesticks = CandlestickItem()
        self.addItem(self._candlesticks)
        self._curve = self.plot(pen=pg.mkPen(theme.ACCENT, width=2))
        self._curve.hide()

        self._mark = pg.InfiniteLine(
            angle=0, pen=pg.mkPen(theme.MUTED, width=1, style=Qt.PenStyle.DashLine)
        )
        self.addItem(self._mark)
        self._mark.hide()

        self._level_lines: list[pg.InfiniteLine] = []
        self._levels: list[float] = []
        self._candles: list[Candle] = []
        self._forming: Candle | None = None
        self._live: float | None = None
        # The range selector decides how many candles are loaded, so everything
        # handed over is drawn. `set_window` stays available for trimming.
        self._window: int | None = None
        self._style = "candles"
        self._mode = "candles"
        self._ticks: list[float] = []

    # --- data ------------------------------------------------------------

    def load_candles(
        self, candles: Sequence[Candle], forming: Candle | None = None
    ) -> None:
        """Closed candles, plus the one still being built if the feed supplies it."""
        # The UI hands over the whole buffer once a second; only genuinely new data
        # is worth a redraw.
        if list(candles) == self._candles and forming == self._forming:
            return
        self._candles = list(candles)
        self._forming = forming
        self._redraw()

    def set_mark(self, price: float) -> None:
        """The live mid: a horizontal line, and the tip of the forming candle.

        Closed candles only change once a timeframe — four hours on the default — so
        folding the live price into the candle currently forming is what makes the
        chart move rather than sit still between closes.
        """
        if price <= 0:
            self._mark.hide()
            self._live = None
            return
        self._mark.setPos(price)
        self._mark.show()
        self._live = price

        # Recorded even while a candle view is showing, so switching to the live
        # view has something to draw instead of starting from an empty chart.
        self._ticks.append(price)
        del self._ticks[:-TICK_BUFFER]
        self._redraw()

    def set_mode(self, mode: str) -> None:
        """`candles` for the OHLC views, `ticks` for the live price line."""
        self._mode = mode
        self._redraw()

    def ticks(self) -> list[float]:
        return self._ticks[-TICK_WINDOW:]

    def set_style(self, style: str) -> None:
        """`candles` or `line`."""
        self._style = style
        self._redraw()

    def set_window(self, candles: int | None) -> None:
        self._window = candles
        self._redraw()

    def set_levels(
        self,
        entry: float | None = None,
        stop: float | None = None,
        target: float | None = None,
    ) -> None:
        """Draw the open position's entry, stop and target across the chart."""
        for line in self._level_lines:
            self.removeItem(line)
        self._level_lines.clear()
        self._levels = []

        for price, colour, style in (
            (entry, theme.TEXT, Qt.PenStyle.DashLine),
            (stop, theme.RED, Qt.PenStyle.DotLine),
            (target, theme.ACCENT, Qt.PenStyle.DotLine),
        ):
            if not price:
                continue
            line = pg.InfiniteLine(
                pos=price, angle=0, pen=pg.mkPen(colour, width=1, style=style)
            )
            self.addItem(line)
            self._level_lines.append(line)
            self._levels.append(price)

        # Auto-range follows the price series alone, so a target above the recent
        # high would sit off-screen — exactly the level the user wants to see.
        self._redraw()

    # --- what is on screen -----------------------------------------------

    def visible_candles(self) -> list[Candle]:
        candles = self._candles[-self._window :] if self._window else list(self._candles)
        forming = self._live_candle()
        return candles + [forming] if forming is not None else candles

    def points(self) -> list[float]:
        return [candle.close for candle in self.visible_candles()]

    def _live_candle(self) -> Candle | None:
        """The forming candle with the live price folded into it.

        The feed only refreshes it every few seconds, so between fetches the mark
        stretches its high and low and moves its close.
        """
        if self._forming is None:
            if self._live is None or not self._candles:
                return None
            # No forming candle from the feed yet: open it at the last close.
            previous = self._candles[-1].close
            return Candle(
                open_time_ms=self._candles[-1].close_time_ms + 1,
                close_time_ms=self._candles[-1].close_time_ms + 2,
                open=previous,
                high=max(previous, self._live),
                low=min(previous, self._live),
                close=self._live,
                volume=0.0,
                trades=0,
            )
        if self._live is None:
            return self._forming
        return replace(
            self._forming,
            high=max(self._forming.high, self._live),
            low=min(self._forming.low, self._live),
            close=self._live,
        )

    # --- internals -------------------------------------------------------

    def _redraw(self) -> None:
        if self._mode == "ticks":
            self._draw_ticks()
            return

        candles = self.visible_candles()

        if self._style == "candles":
            self._curve.hide()
            self._candlesticks.show()
            self._candlesticks.set_candles(candles)
        else:
            self._candlesticks.hide()
            self._curve.show()
            closes = [candle.close for candle in candles]
            self._curve.setData(list(range(len(closes))), closes)

        if not candles:
            return

        # Both ranges are set explicitly rather than left to auto-range, which also
        # fits the infinite mark and level lines and left the x axis stuck on its
        # empty-plot default.
        #
        # How far the drawing actually reaches depends on the style: a candle is a
        # body BODY_WIDTH wide centred on its index, a line is a point exactly on
        # it. Ranging to -1..len either way left a whole empty slot at each end —
        # on the 1H line view that is 12 points inside 13 units, 15% of the width
        # blank, and the chart reads as though it stops short of the price.
        last = len(candles) - 1
        if self._style == "candles":
            # Half a body of air, so the outermost candles do not touch the axis.
            left, right = -BODY_WIDTH, last + BODY_WIDTH
        else:
            left, right = 0, last
        self.setXRange(left, max(right, left + 1), padding=0.01)

        span = [candle.high for candle in candles] + [candle.low for candle in candles]
        span += self._levels
        self._apply_y_range(span)

    def _draw_ticks(self) -> None:
        """The live price line, one point per poll.

        Always a line: a candle built from single samples has no meaningful body,
        and this view starts empty because ticks cannot be backfilled — there is no
        second-resolution history to fetch from anyone.
        """
        ticks = self.ticks()
        self._candlesticks.hide()
        self._curve.show()
        self._curve.setData(list(range(len(ticks))), ticks)
        if not ticks:
            return

        self.setXRange(0, max(1, len(ticks) - 1), padding=0.02)
        # BTC's tick on Hyperliquid is $1 and the spread is usually exactly that, so
        # a quiet minute moves the mid by one or two ticks. Scaled to fit, a $1 step
        # would fill the chart and read as a crash. A floor keeps it proportionate.
        self._apply_y_range(list(ticks) + self._levels, min_fraction=0.001)

    def _apply_y_range(self, span: list[float], min_fraction: float = 0.0) -> None:
        low, high = min(span), max(span)

        floor = high * min_fraction
        if floor and high - low < floor:
            middle = (high + low) / 2
            low, high = middle - floor / 2, middle + floor / 2

        padding = (high - low) * 0.06 or high * 0.001
        # Clamped at zero: on the All view seven years of BTC span roughly $3.8k to
        # $124k, and 6% of that padding pushes the axis below zero. A price axis
        # labelled with negative numbers looks broken.
        self.setYRange(max(0.0, low - padding), high + padding, padding=0)
