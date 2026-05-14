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

    # ---- color = pace-vs-actual delta ----
    # The dominant fill color tells you whether you're burning faster than
    # your budget. Two parallel palettes (5h and weekly) so the rings stay
    # visually distinct but read the same emotional meaning.

    def _verdict_color(self, ratio: float, palette: str = "5h") -> QColor:
        """ratio comes from _will_hit_cap_ratio.
              -1 = idle (no burn) → calm color
               0..0.5 = comfortable (rest easy / fine)
               0.5..1.0 = on pace / ease
               1.0..1.5 = slow
               1.5+ = stop

           Palette: NO orange. Soft cool→sage→rose. Weekly palette is the
           same hue family at slightly lower saturation so the rings stay
           visually distinct.
        """
        # Idle → mute calm
        if ratio < 0:
            return QColor(140, 200, 215) if palette == "5h" else QColor(160, 200, 200)

        if palette == "5h":
            stops = [
                (0.00, QColor( 95, 200, 220)),  # cool blue-teal
                (0.50, QColor(140, 220, 200)),  # mint
                (0.80, QColor(180, 225, 165)),  # cool green
                (1.00, QColor(225, 220, 140)),  # soft yellow (NOT orange)
                (1.30, QColor(235, 160, 175)),  # dusty rose
                (1.80, QColor(225, 100, 140)),  # cool pink
            ]
        else:
            stops = [
                (0.00, QColor(130, 195, 200)),  # pale teal
                (0.50, QColor(165, 215, 190)),  # sage
                (0.80, QColor(195, 220, 165)),  # honeydew
                (1.00, QColor(220, 215, 150)),  # cream-yellow
                (1.30, QColor(225, 170, 175)),  # mauve
                (1.80, QColor(215, 120, 140)),  # rose
            ]
        for i, (t, c) in enumerate(stops):
            if ratio <= t:
                if i == 0:
                    return c
                t_prev, c_prev = stops[i - 1]
                span = max(t - t_prev, 1e-6)
                k = max(0.0, min(1.0, (ratio - t_prev) / span))
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

    def _will_hit_cap_ratio(self, stats, ceiling: int, burn_tpm: float,
                            window_hours: float) -> float:
        """Returns a number where:
              0  = idle / will never hit cap (REST EASY)
              0.5 = will hit cap exactly when window resets (ON PACE)
              1+  = will hit cap WELL before window resets (STOP)

        = (window_time_left / time_until_cap) clipped sensibly.
        """
        if stats is None or burn_tpm <= 0:
            return -1.0  # idle sentinel
        # time until cap at current burn (minutes)
        remaining_tokens = max(ceiling - stats.billed_tokens, 0)
        if remaining_tokens <= 0:
            return 2.0  # already over
        time_until_cap = remaining_tokens / burn_tpm  # minutes
        # time left in the rolling window
        if stats.earliest is None:
            return 0.0
        elapsed_min = (counter.now_utc() - stats.earliest).total_seconds() / 60.0
        window_left = max(window_hours * 60.0 - elapsed_min, 0.0)
        if time_until_cap <= 0:
            return 2.0
        return window_left / time_until_cap

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

        pace5 = self._pace_position(config.FIVE_HOUR_WINDOW)
        pacew = self._pace_position(config.WEEKLY_WINDOW)
        ratio5 = self._will_hit_cap_ratio(
            self._five_hour, limits.five_hour_ceiling,
            self._burn_tpm, config.FIVE_HOUR_WINDOW,
        )
        # For the weekly ring, use a slower burn-rate (avg over last hour)
        # because weekly trends are about hours, not minutes.
        burn_weekly = self._burn_tpm  # for now, same source
        ratiow = self._will_hit_cap_ratio(
            self._weekly, limits.weekly_ceiling,
            burn_weekly, config.WEEKLY_WINDOW,
        )

        self._draw_loaded_ring(
            painter, outer_rect, t5,
            frac=frac5, raw_frac=raw5,
            color=self._verdict_color(ratio5, "5h"),
            pace=pace5,
            time_pressure=self._time_pressure(config.FIVE_HOUR_WINDOW),
            burn_tpm=self._burn_tpm,
        )
        self._draw_loaded_ring(
            painter, inner_rect, tw,
            frac=fracw, raw_frac=raww,
            color=self._verdict_color(ratiow, "weekly"),
            pace=pacew,
            time_pressure=self._time_pressure(config.WEEKLY_WINDOW),
            burn_tpm=0.0,
        )

        self._draw_center_text(painter, frac5, fracw, ratio5, ratiow)

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

    def _verdict_word(self, ratio: float) -> str:
        """ratio from _will_hit_cap_ratio.

        -1  = idle
         0..0.5 = comfortable (REST EASY)
         0.5..0.8 = comfortable (FINE)
         0.8..1.0 = on the edge (ON PACE)
         1.0..1.3 = a little fast (EASE)
         1.3..1.8 = noticeably fast (SLOW)
         1.8+ = stop
        """
        if ratio < 0:
            return "IDLE"
        if ratio < 0.5:
            return "REST EASY"
        if ratio < 0.8:
            return "FINE"
        if ratio < 1.0:
            return "ON PACE"
        if ratio < 1.3:
            return "EASE"
        if ratio < 1.8:
            return "SLOW"
        return "STOP"

    def _draw_center_text(self, painter, frac5, fracw, ratio5, ratiow):
        cx = self.SIZE / 2
        cy = self.SIZE / 2

        limits = config.active_limit()
        color5 = self._verdict_color(ratio5, "5h")

        # 1. Verdict word at the top
        verdict_font = QFont("Helvetica Neue")
        verdict_font.setPointSize(8)
        verdict_font.setBold(True)
        painter.setFont(verdict_font)
        painter.setPen(color5)
        word = self._verdict_word(ratio5)
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
