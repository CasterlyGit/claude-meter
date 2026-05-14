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
            self._burn_tpm = counter.burn_rate_last_n_minutes(now, 30.0)
            self._official = counter.read_official_rate_limits()
        except Exception:
            return
        self.update()

    def _official_pct(self, key: str) -> float | None:
        """Return five_hour or seven_day percentage from Claude's own data."""
        if not self._official:
            return None
        rl = (self._official or {}).get("rate_limits") or {}
        block = rl.get(key)
        if not block:
            return None
        return float(block.get("used_percentage", 0.0)) / 100.0

    def _official_resets_at(self, key: str) -> str | None:
        if not self._official:
            return None
        rl = (self._official or {}).get("rate_limits") or {}
        block = rl.get(key)
        if not block:
            return None
        return block.get("resets_at")

    # ---- color = pace-vs-actual delta ----
    # The dominant fill color tells you whether you're burning faster than
    # your budget. Two parallel palettes (5h and weekly) so the rings stay
    # visually distinct but read the same emotional meaning.

    def _verdict_color(self, delta: float, palette: str = "5h") -> QColor:
        """delta = actual - expected (fraction units).

        Ocean Drive / synthwave neon palette: electric cyan → seafoam → lime →
        sunset gold → hot magenta. The 5h ring leans cooler (cyan side), the
        weekly ring leans warmer (sunset side), but both share the same vibe.
        """
        if palette == "5h":
            stops = [
                (-0.30, QColor(  0, 240, 255)),  # electric cyan
                (-0.15, QColor( 70, 245, 220)),  # neon seafoam
                (-0.05, QColor(120, 250, 180)),  # neon lime
                ( 0.05, QColor(190, 255, 100)),  # acid yellow-green
                ( 0.12, QColor(255, 220,  80)),  # sunset gold
                ( 0.25, QColor(255, 100, 200)),  # hot pink
                ( 1.00, QColor(255,  60, 150)),  # neon magenta
            ]
        else:
            stops = [
                (-0.30, QColor( 90, 200, 255)),  # cyan-blue
                (-0.15, QColor(140, 200, 255)),  # cooler purple-blue
                (-0.05, QColor(200, 170, 255)),  # neon lavender
                ( 0.05, QColor(255, 170, 230)),  # cotton-candy pink
                ( 0.12, QColor(255, 140, 200)),  # bubblegum
                ( 0.25, QColor(255,  90, 160)),  # hot rose
                ( 1.00, QColor(255,  50, 130)),  # neon hot pink
            ]
        for i, (t, c) in enumerate(stops):
            if delta <= t:
                if i == 0:
                    return c
                t_prev, c_prev = stops[i - 1]
                span = max(t - t_prev, 1e-6)
                k = max(0.0, min(1.0, (delta - t_prev) / span))
                return QColor(
                    int(c_prev.red()   * (1 - k) + c.red()   * k),
                    int(c_prev.green() * (1 - k) + c.green() * k),
                    int(c_prev.blue()  * (1 - k) + c.blue()  * k),
                )
        return stops[-1][1]

    # ---- pace calculation ----

    def _pace_position(self, window_hours: float) -> float:
        """How far through the window we are (0..1).

        Used only for track-opacity ('time pressure') visualization, not for
        the verdict — the verdict now uses the simpler 'will I hit the cap
        before the window resets' heuristic instead.
        """
        if window_hours <= 0:
            return 0.0
        stats = self._five_hour if window_hours <= 6 else self._weekly
        if stats is None or stats.earliest is None:
            return 0.0
        elapsed_min = (counter.now_utc() - stats.earliest).total_seconds() / 60.0
        total_min = window_hours * 60.0
        return max(0.0, min(elapsed_min / total_min, 1.0))

    def _pace_delta(self, frac: float, pace: float) -> float:
        """How far ahead/behind the 'fair pace' line you are.

        > 0 → you've burned more than the time-elapsed share (warm/slow down)
        < 0 → you've burned less than the time-elapsed share (cool/headroom)
        """
        return frac - pace

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

        # Prefer Claude's own gauge values if the statusline hook captured them
        official5 = self._official_pct("five_hour")
        officialw = self._official_pct("seven_day")
        if official5 is not None:
            raw5 = official5
        else:
            raw5 = self._five_hour.billed_tokens / max(limits.five_hour_ceiling, 1)
        if officialw is not None:
            raww = officialw
        else:
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

        pace5 = self._pace_position(config.FIVE_HOUR_WINDOW)
        pacew = self._pace_position(config.WEEKLY_WINDOW)
        delta5 = self._pace_delta(frac5, pace5)
        deltaw = self._pace_delta(fracw, pacew)

        self._draw_loaded_ring(
            painter, outer_rect, t5,
            frac=frac5, raw_frac=raw5,
            color=self._verdict_color(delta5, "5h"),
            pace=pace5,
            time_pressure=self._time_pressure(config.FIVE_HOUR_WINDOW),
            burn_tpm=self._burn_tpm,
        )
        self._draw_loaded_ring(
            painter, inner_rect, tw,
            frac=fracw, raw_frac=raww,
            color=self._verdict_color(deltaw, "weekly"),
            pace=pacew,
            time_pressure=self._time_pressure(config.WEEKLY_WINDOW),
            burn_tpm=0.0,
        )

        # Draw the pace markers ON TOP of the fill arcs — radial spokes
        # that stick out past the ring so they're visible regardless of
        # where the fill currently ends.
        self._draw_pace_marker(painter, outer_rect, t5, pace5)
        self._draw_pace_marker(painter, inner_rect, tw, pacew)

        self._draw_center_text(painter, frac5, fracw, delta5, deltaw)

    def _draw_pace_marker(self, painter, rect_tuple, thickness, pace):
        """A radial spoke crossing the ring track perpendicular to it.

        Drawn AFTER the fill arc so it's always on top. Extends slightly
        beyond the ring on both sides for max visibility.
        """
        if pace <= 0:
            return
        x, y, w, h = rect_tuple
        # Center of the ring
        cx_local = x + w / 2.0
        cy_local = y + h / 2.0
        # Ring radius (mid-line of the stroke)
        r = w / 2.0
        # Angle in radians; 12 o'clock = -pi/2 in Qt screen coords
        # (Y grows downward, so we use -sin for the upward direction)
        angle_deg = 90 - 360 * pace  # clockwise from 12
        angle_rad = math.radians(angle_deg)
        # Inner/outer endpoints of the spoke
        half = thickness / 2.0 + 3  # extends 3px past the ring on each side
        x1 = cx_local + math.cos(angle_rad) * (r - half)
        y1 = cy_local - math.sin(angle_rad) * (r - half)
        x2 = cx_local + math.cos(angle_rad) * (r + half)
        y2 = cy_local - math.sin(angle_rad) * (r + half)

        # Bright white spoke with a thin dark outline for contrast against
        # both the fill color AND the track.
        pen_outline = QPen(QColor(0, 0, 0, 200))
        pen_outline.setWidth(4)
        pen_outline.setCapStyle(Qt.RoundCap)
        painter.setPen(pen_outline)
        painter.drawLine(int(x1), int(y1), int(x2), int(y2))

        pen_line = QPen(QColor(255, 255, 255, 255))
        pen_line.setWidth(2)
        pen_line.setCapStyle(Qt.RoundCap)
        painter.setPen(pen_line)
        painter.drawLine(int(x1), int(y1), int(x2), int(y2))

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

        # NOTE: pace marker is drawn AFTER the fill arc (in _draw_pace_marker)
        # so it stays visible regardless of fill state. Skipped here.
        pass

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

    def _time_until_cap(self, stats, ceiling: int, burn_tpm: float) -> str:
        """How long until we hit the ceiling at current burn rate.

        Returns a short string like "1h 12m" or "8m" or "—" for idle.
        """
        remaining = max(ceiling - stats.billed_tokens, 0)
        if burn_tpm <= 0 or remaining <= 0:
            return "—"
        minutes = remaining / burn_tpm
        if minutes < 1:
            return "<1m"
        if minutes < 60:
            return f"{int(round(minutes))}m"
        h = int(minutes // 60)
        m = int(round(minutes % 60))
        if m == 0:
            return f"{h}h"
        return f"{h}h {m}m"

    def _window_time_left(self, stats, window_hours: float) -> str:
        """Time remaining in the rolling window before old samples age out.

        For a 5-hour rolling window, this is (5h - age_of_earliest_sample).
        """
        if stats is None or stats.earliest is None:
            return f"{int(window_hours)}h"
        now = counter.now_utc()
        elapsed = (now - stats.earliest).total_seconds() / 60.0  # min
        total = window_hours * 60.0
        left = max(total - elapsed, 0)
        if left < 60:
            return f"{int(round(left))}m"
        h = int(left // 60)
        m = int(round(left % 60))
        if m == 0:
            return f"{h}h"
        return f"{h}h {m}m"

    def _verdict_word(self, delta: float) -> str:
        """delta = actual - expected (fraction units).
           +0.10 means 'arc is 10 percentage points past the marker.'
        """
        if delta >= 0.25:
            return "STOP"
        if delta >= 0.12:
            return "SLOW"
        if delta >= 0.05:
            return "EASE"
        if delta >= -0.05:
            return "ON PACE"
        if delta >= -0.15:
            return "FINE"
        return "REST EASY"

    def _draw_center_text(self, painter, frac5, fracw, delta5, deltaw):
        cx = self.SIZE / 2
        cy = self.SIZE / 2

        color5 = self._verdict_color(delta5, "5h")

        # 1. Verdict word at the top
        verdict_font = QFont("Helvetica Neue")
        verdict_font.setPointSize(8)
        verdict_font.setBold(True)
        painter.setFont(verdict_font)
        painter.setPen(color5)
        word = self._verdict_word(delta5)
        fm = painter.fontMetrics()
        vw = fm.horizontalAdvance(word)
        painter.drawText(int(cx - vw / 2), int(cy - 24), word)

        # 2. Big number = time left in the 5h window (always meaningful)
        big_font = QFont("Helvetica Neue")
        big_font.setPointSize(15)
        big_font.setBold(True)
        painter.setFont(big_font)
        painter.setPen(color5)
        win_left = self._window_time_left(self._five_hour, config.FIVE_HOUR_WINDOW)
        fm = painter.fontMetrics()
        tw = fm.horizontalAdvance(win_left)
        th = fm.ascent()
        painter.drawText(int(cx - tw / 2), int(cy + th / 2 - 4), win_left)

        # Small label under the big number
        label_font = QFont("Helvetica Neue")
        label_font.setPointSize(7)
        painter.setFont(label_font)
        painter.setPen(QColor(255, 255, 255, 110))
        lbl = "5h window left"
        fm = painter.fontMetrics()
        lw = fm.horizontalAdvance(lbl)
        painter.drawText(int(cx - lw / 2), int(cy + th / 2 + 6), lbl)

        # 3. Bottom line: 5h % · weekly %
        sub_font = QFont("Helvetica Neue")
        sub_font.setPointSize(7)
        painter.setFont(sub_font)
        painter.setPen(QColor(255, 255, 255, 150))
        bottom = f"{int(round(frac5 * 100))}%  ·  wk {int(round(fracw * 100))}%"
        fm = painter.fontMetrics()
        bw = fm.horizontalAdvance(bottom)
        painter.drawText(int(cx - bw / 2), int(cy + th / 2 + 18), bottom)


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    widget = MeterWidget()
    widget.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
