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

from PyQt5.QtCore import QEvent  # noqa: E402  — grouped after QWidget on purpose

from claude_meter import config, counter
from claude_meter.mac_window import make_always_visible


class MeterWidget(QWidget):
    # Widget dimensions — sized for legibility on non-Retina ultrawide
    # monitors at 100% scaling. If you want the older compact look, halve
    # SIZE and SIDE_PANEL and shrink each font by 3pt.
    SIZE = 200           # rings area (was 140)
    SIDE_PANEL = 160     # extra width to the LEFT for info readouts (was 110)
    WIDTH = SIZE + SIDE_PANEL
    HEIGHT = SIZE
    MARGIN_FROM_EDGE = 14
    DOT_SIZE = 28        # bigger so it's tappable on ultrawide (was 22)
    CHEV_SIZE = 22
    CHEV_MARGIN = 7
    BASE_RING_THICKNESS = 13   # scaled up with SIZE
    MAX_RING_THICKNESS = 18
    MIN_RING_THICKNESS = 8
    RING_GAP = 4

    # Burn-rate scale: 50k tokens/min = full comet tail.
    BURN_FULL_TAIL_TPM = 50_000
    MAX_TAIL_DEGREES = 35.0

    def __init__(self) -> None:
        super().__init__(
            flags=Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(self.WIDTH, self.HEIGHT)

        self._five_hour: counter.WindowStats | None = None
        self._weekly: counter.WindowStats | None = None
        self._burn_tpm: float = 0.0  # recent burn rate (last 5 min)
        self._collapsed: bool = False
        self._official: dict | None = None
        # Set true while the refresh script is in flight; cleared when the
        # rate-limits file picks up a newer captured_at than this snapshot.
        self._refresh_pending: bool = False
        self._refresh_baseline_ts: str | None = None
        # Tracks whether the in-flight refresh has already been retried via
        # a pty recycle. Prevents an infinite respawn loop if the new TUI
        # also fails to fire the statusline.
        self._refresh_recycled: bool = False

        self._position_top_right_external()

        self._data_timer = QTimer(self)
        self._data_timer.timeout.connect(self._refresh_data)
        self._data_timer.start(config.REFRESH_SECONDS * 1000)

        self._pin_timer = QTimer(self)
        self._pin_timer.timeout.connect(lambda: make_always_visible(self))
        self._pin_timer.start(2000)

        # Animation tick: drives the comet tail rotation and pace pulse
        self._anim_phase = 0.0
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick_animation)
        self._anim_timer.start(50)  # 20fps — smooth motion, low CPU

        self._refresh_data()

    def _tick_animation(self) -> None:
        self._anim_phase = (self._anim_phase + 0.04) % (2 * math.pi)
        self.update()

    def _position_top_right_external(self) -> None:
        app = QApplication.instance()
        screens = app.screens()
        target = max(screens, key=lambda s: s.geometry().x())
        geo = target.availableGeometry()
        if self._collapsed:
            w = self.DOT_SIZE
        else:
            w = self.WIDTH
        x = geo.right() - w - self.MARGIN_FROM_EDGE
        y = geo.top() + self.MARGIN_FROM_EDGE
        self.move(x, y)

    # ------------------------------------------------------------------
    # Collapse / expand
    # ------------------------------------------------------------------

    def _set_collapsed(self, collapsed: bool) -> None:
        """Toggle between full meter and a tiny urgency-colored dot."""
        if collapsed == self._collapsed:
            return
        self._collapsed = collapsed
        if collapsed:
            self.setFixedSize(self.DOT_SIZE, self.DOT_SIZE)
        else:
            self.setFixedSize(self.WIDTH, self.HEIGHT)
        self._position_top_right_external()
        self.update()

    def _chev_rect(self):
        """Rect of the collapse chevron in the expanded widget (top-right corner)."""
        from PyQt5.QtCore import QRect
        return QRect(
            self.WIDTH - self.CHEV_SIZE - self.CHEV_MARGIN,
            self.CHEV_MARGIN,
            self.CHEV_SIZE,
            self.CHEV_SIZE,
        )

    def _refresh_rect(self):
        """Rect of the refresh button — sits to the LEFT of the chevron."""
        from PyQt5.QtCore import QRect
        return QRect(
            self.WIDTH - 2 * self.CHEV_SIZE - 2 * self.CHEV_MARGIN,
            self.CHEV_MARGIN,
            self.CHEV_SIZE,
            self.CHEV_SIZE,
        )

    def _run_refresh(self) -> None:
        """Send a tiny prompt to the persistent headless claude TUI we own.
        The TUI re-renders, the statusline hook fires, the rate-limits file
        updates within ~5 seconds. First click is slow (~5s cold pty boot);
        subsequent clicks are ~1-2s because the TUI is already running."""
        from . import pty_session
        if self._refresh_pending:
            return  # already in-flight; don't double-spend tokens
        try:
            ok = pty_session.refresh()
        except Exception:
            ok = False
        if not ok:
            return
        self._refresh_pending = True
        self._refresh_recycled = False
        self._refresh_baseline_ts = (self._official or {}).get("captured_at") if self._official else None
        # Poll the file aggressively while the refresh is in flight so the
        # spinner clears as soon as the file actually updates.
        for delay_ms in (800, 1600, 2400, 3200, 4500, 6000, 8000, 11000, 14000):
            QTimer.singleShot(delay_ms, self._refresh_data)
        # If after ~7s the file still hasn't picked up a new captured_at,
        # the TUI is alive-but-wedged: recycle it and resend the prompt.
        QTimer.singleShot(7_000, self._maybe_recycle_pty)
        # Second post-recycle poll window so the spinner clears the moment
        # the respawned TUI's first statusline tick hits the file.
        for delay_ms in (9000, 11000, 13000, 16000, 19000, 22000):
            QTimer.singleShot(delay_ms, self._refresh_data)
        # Safety: hard-clear the pending flag after 25s even if recycle
        # also failed (no network, auth broken, etc).
        QTimer.singleShot(25_000, self._clear_refresh_pending)
        self.update()

    def _maybe_recycle_pty(self) -> None:
        """If the refresh is still pending after the first window, the TUI
        is wedged. Force-respawn it once and resend."""
        if not self._refresh_pending or self._refresh_recycled:
            return
        self._refresh_recycled = True
        from . import pty_session
        try:
            pty_session.recycle()
        except Exception:
            pass

    def _clear_refresh_pending(self) -> None:
        if self._refresh_pending:
            self._refresh_pending = False
            self._refresh_baseline_ts = None
            self._refresh_recycled = False
            self.update()

    def mousePressEvent(self, event):  # noqa: N802
        # When expanded, a click in the top-right chevron toggles collapse.
        # When collapsed, a click anywhere on the dot expands.
        # Right-click anywhere also toggles (kept for power users).
        if event.button() == Qt.RightButton:
            self._set_collapsed(not self._collapsed)
            event.accept()
            return
        if event.button() == Qt.LeftButton:
            if self._collapsed:
                self._set_collapsed(False)
                # Expanding from the dot also kicks a refresh — the user is
                # coming back to look at the numbers, so re-fetch like a
                # reload click would. Cheap (~half a cent) and matches the
                # mental model that "showing me the meter again" = fresh data.
                self._run_refresh()
                event.accept()
                return
            if self._chev_rect().contains(event.pos()):
                self._set_collapsed(True)
                event.accept()
                return
            if self._refresh_rect().contains(event.pos()):
                self._run_refresh()
                event.accept()
                return
        super().mousePressEvent(event)

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
        # If a refresh was in flight, clear the pending flag once we see a
        # newer captured_at than the baseline snapshot taken at click time.
        if self._refresh_pending:
            cur_ts = (self._official or {}).get("captured_at") if self._official else None
            if cur_ts and cur_ts != self._refresh_baseline_ts:
                self._refresh_pending = False
                self._refresh_baseline_ts = None
                self._refresh_recycled = False
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

    def _data_age_seconds(self) -> float | None:
        """How old is the captured rate-limits data, in seconds.

        Reads the `captured_at` ISO timestamp from the official block and
        diffs against now(UTC). Returns None if no data yet."""
        if not self._official:
            return None
        cap = self._official.get("captured_at")
        if not cap:
            return None
        from datetime import datetime, timezone
        try:
            ts = datetime.fromisoformat(str(cap).replace("Z", "+00:00"))
        except Exception:
            return None
        return (datetime.now(timezone.utc) - ts).total_seconds()

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
        # Synthwave/Ocean Drive but easier on the eye in the over-cap range,
        # since that's where the user spends most of their time. We replace
        # the hot pink/magenta with deep violet → indigo. Still neon, still
        # signals 'past the line', but doesn't scream.
        if palette == "5h":
            stops = [
                (-0.30, QColor(  0, 240, 255)),  # electric cyan (way under)
                (-0.15, QColor( 70, 240, 220)),  # neon seafoam
                (-0.05, QColor(120, 245, 180)),  # neon lime
                ( 0.05, QColor(190, 250, 130)),  # acid lime
                ( 0.12, QColor(180, 200, 255)),  # neon periwinkle (on-pace+)
                ( 0.25, QColor(155, 130, 255)),  # electric violet (a bit over)
                ( 1.00, QColor(110,  90, 230)),  # deep indigo (way over)
            ]
        else:
            stops = [
                (-0.30, QColor( 80, 220, 255)),
                (-0.15, QColor(120, 200, 255)),
                (-0.05, QColor(170, 180, 255)),
                ( 0.05, QColor(200, 170, 255)),
                ( 0.12, QColor(190, 160, 250)),
                ( 0.25, QColor(160, 120, 240)),
                ( 1.00, QColor(120, 100, 220)),
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

        Uses Anthropic's authoritative `resets_at` when present — the window
        is fixed-slot, not rolling-from-first-use. Falls back to transcript
        heuristic only when no live data is available.
        """
        if window_hours <= 0:
            return 0.0

        # Prefer the official reset timestamp.
        key = "five_hour" if window_hours <= 6 else "seven_day"
        rl = (self._official or {}).get("rate_limits") or {}
        block = rl.get(key) or {}
        ts = block.get("resets_at")
        if ts is not None:
            try:
                from datetime import datetime, timezone
                if isinstance(ts, (int, float)):
                    reset_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                else:
                    reset_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                secs_left = max(0, (reset_dt - datetime.now(timezone.utc)).total_seconds())
                total_secs = window_hours * 3600.0
                elapsed_secs = max(0.0, total_secs - secs_left)
                return max(0.0, min(elapsed_secs / total_secs, 1.0))
            except Exception:
                pass

        # Fallback: transcript-based heuristic.
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
        """Both rings use base thickness — keeps the visual consistent."""
        return self.BASE_RING_THICKNESS, self.BASE_RING_THICKNESS

    # ---- painting ----

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        if self._collapsed:
            self._paint_collapsed_dot(painter)
            return

        rect = self.rect()
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(15, 17, 22, 235))
        painter.drawRoundedRect(rect, 18, 18)

        # The rings are drawn anchored to the right of the card; the left
        # SIDE_PANEL pixels are reserved for the info panel. Shift all ring
        # geometry by +SIDE_PANEL so the rings sit on the right.
        self._ring_origin_x = self.SIDE_PANEL

        if self._five_hour is None or self._weekly is None:
            return

        official5 = self._official_pct("five_hour")
        officialw = self._official_pct("seven_day")
        if official5 is None or officialw is None:
            self._draw_waiting_state(painter)
            return

        raw5 = official5
        raww = officialw
        frac5 = min(raw5, 1.0)
        fracw = min(raww, 1.0)

        # Feature 6: asymmetric thickness
        t5, tw = self._ring_thicknesses(frac5, fracw)

        outer_inset = 20  # scaled with SIZE
        outer_diam = self.SIZE - 2 * outer_inset
        outer_rect = (self._ring_origin_x + outer_inset, outer_inset, outer_diam, outer_diam)

        # Inner inset depends on outer thickness
        inner_inset = outer_inset + t5 + self.RING_GAP
        inner_diam = self.SIZE - 2 * inner_inset
        inner_rect = (self._ring_origin_x + inner_inset, inner_inset, inner_diam, inner_diam)

        pace5 = self._pace_position(config.FIVE_HOUR_WINDOW)
        pacew = self._pace_position(config.WEEKLY_WINDOW)
        delta5 = self._pace_delta(frac5, pace5)
        deltaw = self._pace_delta(fracw, pacew)

        color5_q = self._verdict_color(delta5, "5h")
        colorw_q = self._verdict_color(deltaw, "weekly")

        self._draw_loaded_ring(
            painter, outer_rect, t5,
            frac=frac5, raw_frac=raw5,
            color=color5_q,
            pace=pace5,
            time_pressure=self._time_pressure(config.FIVE_HOUR_WINDOW),
            burn_tpm=self._burn_tpm,
        )
        self._draw_loaded_ring(
            painter, inner_rect, tw,
            frac=fracw, raw_frac=raww,
            color=colorw_q,
            pace=pacew,
            time_pressure=self._time_pressure(config.WEEKLY_WINDOW),
            burn_tpm=0.0,
        )

        # Draw the pace markers ON TOP of the fill arcs.
        pulse5 = max(0.0, min(delta5 * 2.0, 1.0))
        pulsew = max(0.0, min(deltaw * 2.0, 1.0))
        self._draw_pace_marker(painter, outer_rect, t5, pace5, pulse5)
        self._draw_pace_marker(painter, inner_rect, tw, pacew, pulsew)

        # Percent badges on each ring (color-matched, near leading edge).
        self._draw_ring_pct(painter, outer_rect, frac5, color5_q, label="5h")
        self._draw_ring_pct(painter, inner_rect, fracw, colorw_q, label="wk")

        self._draw_center_text(painter, frac5, fracw, delta5, deltaw)
        self._draw_side_panel(painter, frac5, fracw, delta5, deltaw)
        self._draw_collapse_chevron(painter)
        self._draw_refresh_button(painter)

    def _draw_ring_pct(self, painter, rect_tuple, frac, color, label):
        """Small color-matched percentage label sitting just inside the leading
        edge of the arc. Compact (e.g. '38%') with a tiny scope label
        ('5h' or 'wk') stacked underneath at half size.

        Placement: at the top of the ring (12 o'clock) for the outer ring and
        at the bottom (6 o'clock) for the inner ring — keeps them from
        colliding with the center text and with each other.
        """
        x, y, w, h = rect_tuple
        cx = x + w / 2.0
        cy = y + h / 2.0

        # Both pills sit BELOW the bottom edge of their ring, stacked.
        # Outer (5h) goes just below the outer ring; inner (wk) just below
        # the inner ring. Same visual placement style.
        is_outer = (label == "5h")
        if is_outer:
            tx = cx
            ty = y + h - 24      # just inside the bottom of the outer ring
            anchor_top = False
        else:
            tx = cx
            ty = y + h + 12      # just below the bottom of the inner ring
            anchor_top = False

        pct_text = f"{int(round(frac * 100))}%"

        big_font = QFont("Helvetica Neue")
        big_font.setPointSize(11)  # was 8
        big_font.setBold(True)
        painter.setFont(big_font)
        fm = painter.fontMetrics()
        pw = fm.horizontalAdvance(pct_text)
        ph = fm.ascent()

        # Pill background for legibility against fill
        pad_x, pad_y = 6, 3
        pill_w = pw + pad_x * 2
        pill_h = ph + pad_y * 2
        px = int(tx - pill_w / 2)
        py = int(ty - (pill_h if anchor_top else 0))

        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(15, 17, 22, 220))
        painter.drawRoundedRect(px, py, pill_w, pill_h, 6, 6)

        # Color-matched percent text
        painter.setPen(color)
        painter.drawText(int(tx - pw / 2),
                         int(py + pad_y + ph - 1),
                         pct_text)

    def _draw_collapse_chevron(self, painter):
        """Small clickable chevron in the top-right of the expanded widget.
        Click it to collapse to the dot."""
        rect = self._chev_rect()
        # Subtle background circle for the hit target
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(255, 255, 255, 26))
        painter.drawEllipse(rect)

        # Draw a ">" pointing right (toward the edge of the screen) — visual
        # metaphor: "tuck me away to the right."
        chev_pen = QPen(QColor(230, 230, 240, 220))
        chev_pen.setWidth(2)
        chev_pen.setCapStyle(Qt.RoundCap)
        chev_pen.setJoinStyle(Qt.RoundJoin)
        painter.setPen(chev_pen)
        cx = rect.x() + rect.width() / 2
        cy = rect.y() + rect.height() / 2
        s = 4.0  # arm length
        # ">" shape
        painter.drawLine(int(cx - s + 1), int(cy - s),
                         int(cx + s - 1), int(cy))
        painter.drawLine(int(cx + s - 1), int(cy),
                         int(cx - s + 1), int(cy + s))

    def _draw_refresh_button(self, painter):
        """Circular-arrow refresh icon, left of the chevron. Click =
        explicit token-spending refresh of the rate-limits file.
        Greyed out while a refresh is in flight (clicks are no-ops then)."""
        import math as _m
        rect = self._refresh_rect()
        pending = self._refresh_pending

        # background hit target — dimmer + flatter when disabled (pending),
        # normal when idle. Was previously *brighter* while pending which
        # made the disabled state look more inviting; reversed it.
        painter.setPen(Qt.NoPen)
        bg_alpha = 14 if pending else 36
        painter.setBrush(QColor(255, 255, 255, bg_alpha))
        painter.drawEllipse(rect)

        cx = rect.x() + rect.width() / 2
        cy = rect.y() + rect.height() / 2
        r = (rect.width() / 2) - 5

        # Stroke + arrowhead alpha
        stroke_alpha = 90 if pending else 230
        spin = self._anim_phase if pending else 0.0
        pen = QPen(QColor(230, 230, 240, stroke_alpha))
        pen.setWidth(2)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        start_deg = int((90 + (180 / _m.pi) * spin) * 16)
        span_deg = -int(300 * 16)
        painter.drawArc(int(cx - r), int(cy - r), int(r * 2), int(r * 2),
                        start_deg, span_deg)

        end_rad = _m.radians(90 + (180 / _m.pi) * spin - 300)
        tip_x = cx + r * _m.cos(end_rad)
        tip_y = cy - r * _m.sin(end_rad)
        head_a = end_rad + _m.pi / 2 + 0.5
        head_b = end_rad + _m.pi / 2 - 0.5
        ah1_x = tip_x + 4 * _m.cos(head_a)
        ah1_y = tip_y - 4 * _m.sin(head_a)
        ah2_x = tip_x + 4 * _m.cos(head_b)
        ah2_y = tip_y - 4 * _m.sin(head_b)
        painter.drawLine(int(tip_x), int(tip_y), int(ah1_x), int(ah1_y))
        painter.drawLine(int(tip_x), int(tip_y), int(ah2_x), int(ah2_y))

    def _paint_collapsed_dot(self, painter):
        """Tiny circle showing the 5h urgency hue. Pulses gently when in danger."""
        rect = self.rect()
        # Pick color from the live data if we have it; otherwise a neutral hue.
        if self._five_hour is not None and self._official:
            frac5 = self._official_pct("five_hour") or 0.0
            pace5 = self._pace_position(config.FIVE_HOUR_WINDOW)
            delta5 = self._pace_delta(min(frac5, 1.0), pace5)
            color = self._verdict_color(delta5, "5h")
        else:
            color = QColor(150, 150, 170, 220)

        # Halo — pulse intensity scales with overpace
        pulse = (math.sin(self._anim_phase * 1.5) + 1) / 2  # 0..1
        halo = QColor(color)
        halo.setAlpha(int(70 + 60 * pulse))
        painter.setPen(Qt.NoPen)
        painter.setBrush(halo)
        painter.drawEllipse(rect)

        # Solid inner dot
        inner = rect.adjusted(4, 4, -4, -4)
        bright = QColor(min(color.red() + 30, 255),
                        min(color.green() + 30, 255),
                        min(color.blue() + 30, 255), 255)
        painter.setBrush(bright)
        painter.drawEllipse(inner)

    def _draw_waiting_state(self, painter):
        """Render when no live rate-limit data is available.

        Draws empty ring outlines and a small 'waiting' message in the center.
        No estimates, no guessed numbers — the user asked for real data only.
        """
        cx = self.SIDE_PANEL + self.SIZE / 2
        cy = self.SIZE / 2

        outer_inset = 14
        outer_diam = self.SIZE - 2 * outer_inset
        inner_inset = outer_inset + self.BASE_RING_THICKNESS + self.RING_GAP
        inner_diam = self.SIZE - 2 * inner_inset

        # Two faint ring outlines + a slow synthwave-cyan sweep so the
        # waiting state feels alive, not broken
        ox = self.SIDE_PANEL
        for idx, (rect_tuple, thickness) in enumerate([
            ((ox + outer_inset, outer_inset, outer_diam, outer_diam), self.BASE_RING_THICKNESS),
            ((ox + inner_inset, inner_inset, inner_diam, inner_diam), self.BASE_RING_THICKNESS),
        ]):
            x, y, w, h = rect_tuple
            pen = QPen(QColor(255, 255, 255, 20))
            pen.setWidth(thickness)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.drawArc(x, y, w, h, 0, 360 * 16)

            # Slow rotating arc — different speeds per ring
            sweep_pen = QPen(QColor(110, 220, 255, 110))
            sweep_pen.setWidth(thickness)
            sweep_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(sweep_pen)
            phase = self._anim_phase * (1.0 if idx == 0 else 0.7)
            start_angle = int(((phase / (2 * math.pi)) * 360 + idx * 180) * 16) % (360 * 16)
            painter.drawArc(x, y, w, h, start_angle, int(-40 * 16))

        # Center message
        font = QFont("Helvetica Neue")
        font.setPointSize(9)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(255, 255, 255, 130))
        msg = "—"
        fm = painter.fontMetrics()
        mw = fm.horizontalAdvance(msg)
        painter.drawText(int(cx - mw / 2), int(cy - 4), msg)

        sub_font = QFont("Helvetica Neue")
        sub_font.setPointSize(7)
        painter.setFont(sub_font)
        painter.setPen(QColor(255, 255, 255, 90))
        sub = "no live data"
        fm = painter.fontMetrics()
        sw = fm.horizontalAdvance(sub)
        painter.drawText(int(cx - sw / 2), int(cy + 10), sub)

        sub2 = "open a claude session"
        sw2 = fm.horizontalAdvance(sub2)
        painter.drawText(int(cx - sw2 / 2), int(cy + 22), sub2)

    def _time_until_reset(self, key: str) -> str:
        """Compute 'time until window resets' from resets_at timestamp."""
        from datetime import datetime, timezone
        rl = (self._official or {}).get("rate_limits") or {}
        block = rl.get(key) or {}
        ts = block.get("resets_at")
        if ts is None:
            return "—"
        # Can be unix seconds (int) or ISO8601 string
        try:
            if isinstance(ts, (int, float)):
                reset_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                reset_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except Exception:
            return "—"
        delta = reset_dt - datetime.now(timezone.utc)
        secs = int(delta.total_seconds())
        if secs <= 0:
            return "now"
        if secs < 3600:
            return f"{secs // 60}m"
        if secs < 86400:
            h = secs // 3600
            m = (secs % 3600) // 60
            return f"{h}h {m}m" if m else f"{h}h"
        d = secs // 86400
        h = (secs % 86400) // 3600
        return f"{d}d {h}h" if h else f"{d}d"

    def _draw_side_panel(self, painter, frac5, fracw, delta5, deltaw):
        """Left-side info panel: slider-style horizontal lines, one per dim.

        Four rows: 5h used, time elapsed in 5h, week used, time elapsed in week.
        Each row: small label, a faint track line, a colored glowing knob at
        the position. Matches the slider aesthetic from the docs demo.
        """
        c5 = self._verdict_color(delta5, "5h")
        cw = self._verdict_color(deltaw, "weekly")
        pace5 = self._pace_position(config.FIVE_HOUR_WINDOW)
        pacew = self._pace_position(config.WEEKLY_WINDOW)

        # Geometry — scaled up for legibility on non-Retina ultrawide.
        x_lbl = 14
        x_track = 14
        track_w = self.SIDE_PANEL - 28
        row_top = 26
        row_h = 38

        label_font = QFont("Helvetica Neue")
        label_font.setPointSize(10)
        label_font.setBold(True)

        # Two dual-track rows: ●━━━━━━━━━━━━━━━  with a thin time-tick on
        # the same track. The gap between budget-fill and time-tick IS the
        # pace story — no separate "elapsed" row needed.
        rows = [
            ("clock",    frac5, pace5, c5),    # 5-hour pair
            ("calendar", fracw, pacew, cw),    # weekly pair
        ]
        # Re-space for two big rows instead of four small ones.
        row_top = 38
        row_h = 60

        pulse = (math.sin(self._anim_phase) + 1) / 2

        value_font = QFont("Helvetica Neue")
        value_font.setPointSize(12)
        value_font.setBold(True)

        for i, (icon, fill_v, time_v, color) in enumerate(rows):
            y_lbl = row_top + i * row_h
            y_track = y_lbl + 22

            # Icon + short scope label (kept small but explicit — "5h" / "wk")
            self._draw_pair_icon(painter, icon, x_lbl, y_lbl - 4, color)
            scope_font = QFont("Helvetica Neue")
            scope_font.setPointSize(9)
            scope_font.setBold(True)
            painter.setFont(scope_font)
            painter.setPen(QColor(255, 255, 255, 220))
            scope_text = "5h" if icon == "clock" else "wk"
            painter.drawText(x_lbl + 20, y_lbl + 8, scope_text)

            # Track (rail) — pushed further right to accommodate the scope word
            rail_offset = 46
            track_pen = QPen(QColor(255, 255, 255, 55))
            track_pen.setWidth(4)
            track_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(track_pen)
            painter.drawLine(x_track + rail_offset, y_track,
                             x_track + rail_offset + track_w - rail_offset, y_track)

            track_left  = x_track + rail_offset
            track_right = track_left + (track_w - rail_offset)
            track_span  = track_right - track_left

            fill_x = int(track_left + min(fill_v, 1.0) * track_span)
            time_x = int(track_left + min(time_v, 1.0) * track_span)

            # Colored fill (budget consumed)
            fill_pen = QPen(color)
            fill_pen.setWidth(6)
            fill_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(fill_pen)
            painter.drawLine(track_left, y_track, fill_x, y_track)

            # Bright knob at the fill head, with breathing halo
            halo_r = int(11 + pulse * 3)
            halo_color = QColor(color)
            halo_color.setAlpha(int(60 + pulse * 90))
            painter.setPen(Qt.NoPen)
            painter.setBrush(halo_color)
            painter.drawEllipse(fill_x - halo_r, y_track - halo_r, halo_r * 2, halo_r * 2)
            bright = QColor(color)
            bright.setRgb(min(color.red() + 30, 255),
                          min(color.green() + 30, 255),
                          min(color.blue() + 30, 255))
            painter.setBrush(bright)
            painter.drawEllipse(fill_x - 6, y_track - 6, 12, 12)

            # Time tick — small white vertical mark on the same rail.
            # Outline (black) then white center for legibility on both
            # filled and unfilled portions.
            tick_h = 14
            tick_pen_bg = QPen(QColor(0, 0, 0, 200))
            tick_pen_bg.setWidth(5)
            tick_pen_bg.setCapStyle(Qt.RoundCap)
            painter.setPen(tick_pen_bg)
            painter.drawLine(time_x, y_track - tick_h // 2,
                             time_x, y_track + tick_h // 2)
            tick_pen = QPen(QColor(255, 255, 255, 235))
            tick_pen.setWidth(2)
            tick_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(tick_pen)
            painter.drawLine(time_x, y_track - tick_h // 2,
                             time_x, y_track + tick_h // 2)

            # Value: just the % of budget, color-matched, right of the track
            painter.setFont(value_font)
            painter.setPen(bright)
            pct_str = f"{int(round(min(fill_v, 1.0) * 100))}%"
            fm = painter.fontMetrics()
            pw = fm.horizontalAdvance(pct_str)
            painter.drawText(int(track_right - pw), int(y_lbl - 2), pct_str)

    def _draw_pair_icon(self, painter, kind: str, x: int, y: int, color: QColor) -> None:
        """Tiny glyph in place of a row label. kind ∈ {"clock", "calendar"}."""
        from PyQt5.QtCore import QRect
        size = 16
        rect = QRect(x, y, size, size)
        pen = QPen(color)
        pen.setWidth(1)
        painter.setPen(pen)
        painter.setBrush(Qt.NoBrush)
        if kind == "clock":
            # Circle + two hands (12 + 3)
            painter.drawEllipse(rect)
            cx = x + size / 2
            cy = y + size / 2
            painter.drawLine(int(cx), int(cy), int(cx), int(y + 4))   # minute hand up
            painter.drawLine(int(cx), int(cy), int(x + size - 4), int(cy))  # hour hand right
        else:
            # Calendar: small rectangle with a header bar and a grid notch
            painter.drawRoundedRect(rect, 2, 2)
            painter.drawLine(x, y + 5, x + size, y + 5)  # header divider
            # Two tabs at the top to look like rings
            painter.drawLine(x + 4, y, x + 4, y + 3)
            painter.drawLine(x + size - 4, y, x + size - 4, y + 3)

    def _draw_pace_marker(self, painter, rect_tuple, thickness, pace,
                          pulse_intensity=0.0):
        """A radial spoke crossing the ring track perpendicular to it.

        Drawn AFTER the fill arc so it's always on top. Extends slightly
        beyond the ring on both sides for max visibility. The pulse_intensity
        argument (0..1) modulates the glow halo around the marker — higher
        = faster, brighter pulse, used to signal "you're overpacing right now."
        """
        if pace <= 0:
            return
        x, y, w, h = rect_tuple
        cx_local = x + w / 2.0
        cy_local = y + h / 2.0
        r = w / 2.0
        angle_deg = 90 - 360 * pace
        angle_rad = math.radians(angle_deg)
        half = thickness / 2.0 + 3
        x1 = cx_local + math.cos(angle_rad) * (r - half)
        y1 = cy_local - math.sin(angle_rad) * (r - half)
        x2 = cx_local + math.cos(angle_rad) * (r + half)
        y2 = cy_local - math.sin(angle_rad) * (r + half)

        # Soft halo that pulses when overpacing — sin wave of self._anim_phase
        if pulse_intensity > 0:
            pulse = (math.sin(self._anim_phase * (1 + pulse_intensity * 2)) + 1) / 2
            halo_alpha = int(60 + 80 * pulse * pulse_intensity)
            halo_pen = QPen(QColor(255, 255, 255, halo_alpha))
            halo_pen.setWidth(int(6 + 4 * pulse_intensity))
            halo_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(halo_pen)
            painter.drawLine(int(x1), int(y1), int(x2), int(y2))

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

        # --- track ---
        # Very faint neutral grey. Was previously hued + tied to "time pressure"
        # which made the unfilled portion read like a phantom data arc on darker
        # palettes. Now it's purely a hairline rail.
        track_alpha = int(14 + 14 * time_pressure)  # 14..28 max
        track_pen = QPen(QColor(255, 255, 255, track_alpha))
        track_pen.setWidth(max(2, thickness - 4))   # thinner than the fill
        track_pen.setCapStyle(Qt.RoundCap)
        painter.setPen(track_pen)
        painter.drawArc(x, y, w, h, 0, 360 * 16)

        # NOTE: pace marker is drawn AFTER the fill arc (in _draw_pace_marker)
        # so it stays visible regardless of fill state. Skipped here.
        pass

        if frac <= 0:
            return

        # --- main filled arc up to min(frac, 1.0) ---
        # IMPORTANT: trailing end is FlatCap so the rounded cap doesn't bulge
        # backwards from 12 o'clock and look like a phantom 5-10% arc on the
        # left side. Leading edge stays rounded for a clean head.
        fill_pen = QPen(color)
        fill_pen.setWidth(thickness)
        fill_pen.setCapStyle(Qt.FlatCap)
        painter.setPen(fill_pen)
        start_angle = 90 * 16
        span = -int(frac * 360 * 16)
        painter.drawArc(x, y, w, h, start_angle, span)

        # Round just the LEADING edge by drawing a tiny rounded cap at the
        # tip. This gives the arc a clean head without the backward bulge.
        if frac > 0.005:
            cap_pen = QPen(color)
            cap_pen.setWidth(thickness)
            cap_pen.setCapStyle(Qt.RoundCap)
            painter.setPen(cap_pen)
            painter.drawArc(x, y, w, h,
                            int((90 - frac * 360) * 16),
                            -8)  # half-degree nub at the leading edge

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
        """Time remaining until the window resets, per Anthropic's own clock.

        Anthropic's "5h window" is NOT a rolling window that starts when you
        start working — it's a fixed slot with a hard `resets_at` epoch on
        every rate-limit response. We use that as the source of truth.
        Falls back to the earliest-sample heuristic only when no live data.
        """
        # Prefer the authoritative resets_at from the official block.
        key = "five_hour" if window_hours <= 6 else "seven_day"
        rl = (self._official or {}).get("rate_limits") or {}
        block = rl.get(key) or {}
        ts = block.get("resets_at")
        if ts is not None:
            try:
                from datetime import datetime, timezone
                if isinstance(ts, (int, float)):
                    reset_dt = datetime.fromtimestamp(ts, tz=timezone.utc)
                else:
                    reset_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                secs = max(0, int((reset_dt - datetime.now(timezone.utc)).total_seconds()))
                if secs < 60:
                    return f"{secs}s"
                if secs < 3600:
                    return f"{secs // 60}m"
                if secs < 86400:
                    h = secs // 3600
                    m = (secs % 3600) // 60
                    return f"{h}h {m}m" if m else f"{h}h"
                d = secs // 86400
                h = (secs % 86400) // 3600
                return f"{d}d {h}h" if h else f"{d}d"
            except Exception:
                pass

        # Fallback: transcript-based heuristic.
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
        cx = self.SIDE_PANEL + self.SIZE / 2
        cy = self.SIZE / 2

        color5 = self._verdict_color(delta5, "5h")

        # Center stack — three equal-size lines, distinguished by weight and
        # alpha rather than by font size:
        #   line 1 (top)    : NN% USED  — color + light  (the *what*)
        #   line 2 (middle) : 4h 43m    — color + bold   (the *time*, primary)
        #   line 3 (bottom) : ON PACE   — color + heavy  (the *verdict*, action)
        line_font = QFont("Helvetica Neue")
        line_font.setPointSize(11)
        painter.setFont(line_font)
        fm = painter.fontMetrics()
        line_h = fm.height()

        pct_str = f"{int(round(min(frac5, 1.0) * 100))}% USED"
        win_left = self._window_time_left(self._five_hour, config.FIVE_HOUR_WINDOW)
        verdict = self._verdict_word(delta5)

        # Slightly different alphas/weights per line.
        dim_color = QColor(color5)
        dim_color.setAlpha(190)
        bright_color = QColor(min(color5.red() + 25, 255),
                              min(color5.green() + 25, 255),
                              min(color5.blue() + 25, 255), 255)

        lines = (
            (pct_str,  QFont.Medium,    dim_color),
            (win_left, QFont.Bold,      color5),
            (verdict,  QFont.Black,     bright_color),
        )

        block_top = cy - line_h * 1.5 + fm.ascent()
        for i, (text_line, weight, color) in enumerate(lines):
            f = QFont(line_font)
            f.setWeight(weight)
            painter.setFont(f)
            painter.setPen(color)
            fm_line = painter.fontMetrics()
            tw = fm_line.horizontalAdvance(text_line)
            painter.drawText(int(cx - tw / 2),
                             int(block_top + i * line_h),
                             text_line)

        # Stale-data warning: if the captured_at is older than 90s, draw a
        # tiny "stale Xm" pill below the center stack so the user knows the
        # numbers aren't live. The hook only fires from terminal Claude
        # sessions, so VSCode-only work drifts.
        stale_secs = self._data_age_seconds()
        if stale_secs is not None and stale_secs > 90:
            mins = int(stale_secs // 60)
            stale_text = f"stale {mins}m" if mins >= 1 else f"stale {int(stale_secs)}s"
            stale_font = QFont("Helvetica Neue")
            stale_font.setPointSize(8)
            stale_font.setBold(True)
            painter.setFont(stale_font)
            fm_s = painter.fontMetrics()
            sw = fm_s.horizontalAdvance(stale_text)
            sh = fm_s.height()
            pad_x, pad_y = 6, 2
            pill_w = sw + pad_x * 2
            pill_h = sh + pad_y
            px = int(cx - pill_w / 2)
            py = int(block_top + 3 * line_h + 4)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(200, 100, 60, 220))
            painter.drawRoundedRect(px, py, pill_w, pill_h, 6, 6)
            painter.setPen(QColor(15, 17, 22, 255))
            painter.drawText(int(cx - sw / 2),
                             int(py + pad_y + fm_s.ascent() - 1),
                             stale_text)


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    widget = MeterWidget()
    widget.show()
    # Clean up the headless claude pty (if one was spawned) on exit so we
    # don't leave orphan claude processes behind when the meter quits.
    def _on_quit():
        try:
            from . import pty_session
            pty_session.shutdown()
        except Exception:
            pass
    app.aboutToQuit.connect(_on_quit)
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
