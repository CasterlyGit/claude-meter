"""PyQt floating window pinned to the top-right of an external display.

Visual language — every property of the rings encodes information:
  * Filled arc length = % of ceiling used
  * Hue family       = which window (5h = cyan/coral, weekly = green/amber)
  * Hue intensity    = urgency tier (calm → warn → danger)
  * Pace tick        = where you "should be" right now at this point in the window
  * Track opacity    = time pressure (track brightens as window winds down)
  * Comet tail       = burn rate over last 5 min (length proportional to tok/min)
  * Ring thickness   = which window has more pressure (heavier = more loaded)
  * Dashed overflow  = arc past 100% (drawn dashed) when above ceiling estimate

Pinning uses curby's NSView→NSWindow shim — battle-tested and survives
app switches.
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtGui import QColor, QFont, QPainter, QPen
from PyQt5.QtWidgets import QApplication, QWidget

from claude_meter import config, counter
from claude_meter.mac_window import make_always_visible


class MeterWidget(QWidget):
    SIZE = 140
    MARGIN_FROM_EDGE = 14
    BASE_RING_THICKNESS = 9
    MAX_RING_THICKNESS = 14
    MIN_RING_THICKNESS = 6
    RING_GAP = 3

    # Burn-rate scale: 50k tokens/min = full comet tail.
    BURN_FULL_TAIL_TPM = 50_000
    MAX_TAIL_DEGREES = 35.0

    def __init__(self) -> None:
        super().__init__(
            flags=Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(self.SIZE, self.SIZE)

        self._five_hour: counter.WindowStats | None = None
        self._weekly: counter.WindowStats | None = None
        self._burn_tpm: float = 0.0  # recent burn rate (last 5 min)

        self._position_top_right_external()

        self._data_timer = QTimer(self)
        self._data_timer.timeout.connect(self._refresh_data)
        self._data_timer.start(config.REFRESH_SECONDS * 1000)

        self._pin_timer = QTimer(self)
        self._pin_timer.timeout.connect(lambda: make_always_visible(self))
        self._pin_timer.start(2000)

        self._refresh_data()

    def _position_top_right_external(self) -> None:
        app = QApplication.instance()
        screens = app.screens()
        target = max(screens, key=lambda s: s.geometry().x())
        geo = target.availableGeometry()
        x = geo.right() - self.SIZE - self.MARGIN_FROM_EDGE
        y = geo.top() + self.MARGIN_FROM_EDGE
        self.move(x, y)

    def showEvent(self, event):  # noqa: N802
        super().showEvent(event)
        make_always_visible(self)

    def _refresh_data(self) -> None:
        try:
            now = counter.now_utc()
            self._five_hour = counter.stats_for_window(now, config.FIVE_HOUR_WINDOW)
            self._weekly = counter.stats_for_window(now, config.WEEKLY_WINDOW)
            self._burn_tpm = counter.burn_rate_last_n_minutes(now, 5.0)
        except Exception:
            return
        self.update()

    # ---- color families per ring ----

    def _color_5h(self, frac: float) -> QColor:
        if frac >= config.DANGER_THRESHOLD:
            return QColor(255, 70, 130)   # hot pink/magenta
        if frac >= config.WARN_THRESHOLD:
            return QColor(255, 140, 90)   # coral
        if frac >= 0.30:
            return QColor(110, 200, 255)  # cyan
        return QColor(140, 230, 220)      # mint-cyan idle

    def _color_weekly(self, frac: float) -> QColor:
        if frac >= config.DANGER_THRESHOLD:
            return QColor(255, 90, 60)
        if frac >= config.WARN_THRESHOLD:
            return QColor(255, 200, 70)
        return QColor(120, 230, 150)

    # ---- pace calculation ----

    def _pace_position(self, window_hours: float) -> float:
        """Where you 'should be' at this moment, as a fraction 0..1.

        For a rolling window, the pace position is just (elapsed_in_window /
        total_window). But for a rolling window you're ALWAYS in the middle
        of it — there's no clean "elapsed." So we use the time since the
        earliest sample in the window as a proxy: how long have you been
        actively using the window.
        """
        if window_hours <= 0:
            return 0.0
        # For a rolling window, the "pace" reference is uniform: by linearity,
        # you should be at fraction = (window_age / window_total).
        # We approximate window_age as time since the earliest sample.
        stats = self._five_hour if window_hours <= 6 else self._weekly
        if stats is None or stats.earliest is None or stats.latest is None:
            return 0.0
        elapsed_min = (counter.now_utc() - stats.earliest).total_seconds() / 60.0
        total_min = window_hours * 60.0
        return max(0.0, min(elapsed_min / total_min, 1.0))

    # ---- time pressure (drives track opacity) ----

    def _time_pressure(self, window_hours: float) -> float:
        """0 = window is fresh (low pressure), 1 = window is winding down.

        Approximated by: fraction of the window already 'consumed' by time,
        since the earliest sample.
        """
        return self._pace_position(window_hours)

    # ---- ring thickness from relative pressure ----

    def _ring_thicknesses(self, frac5: float, fracw: float) -> tuple[int, int]:
        """Heavier-loaded ring gets more visual mass."""
        total = max(frac5 + fracw, 0.001)
        share5 = frac5 / total
        sharew = fracw / total
        # Map shares 0..1 → thickness MIN..MAX, centered at BASE
        spread = self.MAX_RING_THICKNESS - self.MIN_RING_THICKNESS
        t5 = int(self.MIN_RING_THICKNESS + spread * share5)
        tw = int(self.MIN_RING_THICKNESS + spread * sharew)
        return t5, tw

    # ---- painting ----

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        rect = self.rect()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(15, 17, 22, 235))
        painter.drawRoundedRect(rect, 18, 18)

        if self._five_hour is None or self._weekly is None:
            return

        limits = config.active_limit()
        raw5 = self._five_hour.billed_tokens / max(limits.five_hour_ceiling, 1)
        raww = self._weekly.billed_tokens / max(limits.weekly_ceiling, 1)
        frac5 = min(raw5, 1.0)
        fracw = min(raww, 1.0)

        # Feature 6: asymmetric thickness
        t5, tw = self._ring_thicknesses(frac5, fracw)

        outer_inset = 14
        outer_diam = self.SIZE - 2 * outer_inset
        outer_rect = (outer_inset, outer_inset, outer_diam, outer_diam)

        # Inner inset depends on outer thickness
        inner_inset = outer_inset + t5 + self.RING_GAP
        inner_diam = self.SIZE - 2 * inner_inset
        inner_rect = (inner_inset, inner_inset, inner_diam, inner_diam)

        # Outer ring (5h)
        self._draw_loaded_ring(
            painter,
            outer_rect,
            t5,
            frac=frac5,
            raw_frac=raw5,
            color=self._color_5h(frac5),
            pace=self._pace_position(config.FIVE_HOUR_WINDOW),
            time_pressure=self._time_pressure(config.FIVE_HOUR_WINDOW),
            burn_tpm=self._burn_tpm,
        )
        # Inner ring (weekly) — no comet tail (weekly burn isn't meaningful at this granularity)
        self._draw_loaded_ring(
            painter,
            inner_rect,
            tw,
            frac=fracw,
            raw_frac=raww,
            color=self._color_weekly(fracw),
            pace=self._pace_position(config.WEEKLY_WINDOW),
            time_pressure=self._time_pressure(config.WEEKLY_WINDOW),
            burn_tpm=0.0,
        )

        self._draw_center_text(painter, frac5, fracw)

    def _draw_loaded_ring(
        self,
        painter,
        rect_tuple,
        thickness,
        frac,
        raw_frac,
        color,
        pace,
        time_pressure,
        burn_tpm,
    ):
        x, y, w, h = rect_tuple

        # --- track (feature 2: opacity = time pressure) ---
        # Idle: 20 alpha. Wound-down: 70 alpha.
        track_alpha = int(20 + 50 * time_pressure)
        track_pen = QPen(QColor(255, 255, 255, track_alpha))
        track_pen.setWidth(thickness)
        track_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(track_pen)
        painter.drawArc(x, y, w, h, 0, 360 * 16)

        # --- pace tick (feature 1) ---
        # Tick at pace position on the track. Goes 12 o'clock + clockwise.
        # 12 o'clock = 90° in Qt's angle system. Clockwise = negative angle.
        if pace > 0:
            tick_angle_deg = 90 - 360 * pace
            tick_thickness = max(2, thickness // 4)
            pen_tick = QPen(QColor(255, 255, 255, 200))
            pen_tick.setWidth(tick_thickness)
            pen_tick.setCapStyle(Qt.FlatCap)
            painter.setPen(pen_tick)
            painter.drawArc(x, y, w, h, int(tick_angle_deg * 16), int(2 * 16))

        if frac <= 0:
            return

        # --- main filled arc up to min(frac, 1.0) ---
        fill_pen = QPen(color)
        fill_pen.setWidth(thickness)
        fill_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(fill_pen)
        start_angle = 90 * 16
        span = -int(frac * 360 * 16)
        painter.drawArc(x, y, w, h, start_angle, span)

        # --- feature 3: comet tail ---
        # A second arc OVERLAID on the leading-edge portion, brighter & wider.
        # Length proportional to recent burn rate. Only on the 5h ring (burn_tpm=0 for weekly).
        if burn_tpm > 0:
            tail_frac = min(burn_tpm / self.BURN_FULL_TAIL_TPM, 1.0)
            tail_deg = self.MAX_TAIL_DEGREES * tail_frac
            tail_color = QColor(color)
            tail_color.setAlpha(180)
            tail_pen = QPen(tail_color)
            tail_pen.setWidth(thickness + 2)  # slightly wider to "glow"
            tail_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(tail_pen)
            # Tail extends BACKWARDS from the leading edge (against direction of travel)
            lead_angle_deg = 90 - 360 * frac
            tail_start_deg = lead_angle_deg + tail_deg  # behind leading edge
            tail_span_deg = -tail_deg
            painter.drawArc(
                x, y, w, h,
                int(tail_start_deg * 16),
                int(tail_span_deg * 16),
            )

        # --- feature 7: dotted overflow ---
        # When raw_frac > 1.0, draw the overflow portion as a dashed arc
        # in the SAME color (so it's visually continuous) but with a dash pattern.
        if raw_frac > 1.0:
            overflow = min(raw_frac - 1.0, 0.5)  # cap visual overflow at +50%
            dash_pen = QPen(color)
            dash_pen.setWidth(thickness)
            dash_pen.setCapStyle(Qt.FlatCap)
            dash_pen.setStyle(Qt.DashLine)
            painter.setPen(dash_pen)
            # Overflow continues from where the full ring ended.
            # 100% point: angle = 90 - 360 = -270 (which is the same as 90° — full circle).
            # We start the dashed arc at 90° and continue clockwise into a second lap.
            overflow_start_deg = 90
            overflow_span_deg = -overflow * 360
            painter.drawArc(
                x, y, w, h,
                int(overflow_start_deg * 16),
                int(overflow_span_deg * 16),
            )

    def _draw_center_text(self, painter, frac5, fracw):
        cx = self.SIZE / 2
        cy = self.SIZE / 2

        big_font = QFont("Helvetica Neue")
        big_font.setPointSize(19)
        big_font.setBold(True)
        painter.setFont(big_font)
        painter.setPen(self._color_5h(frac5))
        text = f"{int(round(frac5 * 100))}"
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(text)
        th = fm.ascent()
        painter.drawText(int(cx - tw / 2), int(cy + th / 2 - 2), text)

        # tiny weekly % under
        sub_font = QFont("Helvetica Neue")
        sub_font.setPointSize(8)
        painter.setFont(sub_font)
        painter.setPen(self._color_weekly(fracw))
        wk_text = f"wk·{int(round(fracw * 100))}"
        fm = painter.fontMetrics()
        ww = fm.horizontalAdvance(wk_text)
        painter.drawText(int(cx - ww / 2), int(cy + th / 2 + 10), wk_text)


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    widget = MeterWidget()
    widget.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
