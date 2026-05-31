"""PyQt floating meter pinned to a corner of a chosen display.

Visual language:
  arc fill = % of ceiling used · hue = pace delta · comet = burn rate
  pace tick = where you "should be" now · collapsed dot = pillar fill by 5h %
"""
from __future__ import annotations

import json
import math
import sys
import threading
from pathlib import Path

from PyQt5.QtCore import Qt, QTimer, QPoint, QRect, QRectF
from PyQt5.QtGui import QColor, QFont, QPainter, QPen, QPainterPath
from PyQt5.QtWidgets import QApplication, QWidget

from claude_meter import config, counter
from claude_meter.mac_window import make_always_visible


POSITION_FILE = Path.home() / ".claude" / "state" / "claude-meter-position.json"


# ── State persistence ─────────────────────────────────────────────────────────

def _load_ui_state() -> dict:
    try:
        data = json.loads(POSITION_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_ui_state(**kw) -> None:
    try:
        POSITION_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = _load_ui_state()
        data.update(kw)
        POSITION_FILE.write_text(json.dumps(data))
    except OSError:
        pass


# ── Widget ────────────────────────────────────────────────────────────────────

class MeterWidget(QWidget):
    SIZE          = 215
    SIDE_PANEL    = 165
    WIDTH         = SIZE + SIDE_PANEL
    HEIGHT        = SIZE
    MARGIN        = 14
    DOT_W         = 120   # collapsed pill width
    DOT_H         = 38    # collapsed pill height
    CHEV_SIZE     = 22
    CHEV_MARGIN   = 7
    RING_THICK    = 13
    RING_GAP      = 4
    RING_TOP      = 0    # rings centered; buttons live in side panel, not ring zone

    BURN_FULL_TPM  = 50_000
    MAX_TAIL_DEG   = 35.0
    DRAG_THRESH    = 4

    def __init__(self) -> None:
        super().__init__(flags=Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)

        st = _load_ui_state()
        self._collapsed: bool = bool(st.get("collapsed", False))
        self.setFixedSize(self.DOT_W if self._collapsed else self.WIDTH,
                          self.DOT_H if self._collapsed else self.HEIGHT)

        # Data state (read only on main thread, written by _apply_staged)
        self._five_hour: counter.WindowStats | None = None
        self._weekly:    counter.WindowStats | None = None
        self._burn_tpm:  float = 0.0
        self._official:  dict | None = None
        self._last_good_official: dict | None = None

        # Background-fetch state — written by bg thread, consumed by _tick_anim
        self._data_lock   = threading.Lock()
        self._staged: dict | None = None   # set by bg thread
        self._fetch_busy  = False          # guards against overlapping fetches

        # Refresh-in-flight state
        self._refresh_pending:      bool = False
        self._refresh_baseline_ts:  str | None = None
        self._refresh_recycled:     bool = False
        self._refresh_pending_since: float = 0.0   # monotonic; watchdog stuck-detect

        # Drag state
        self._drag_origin: QPoint | None = None
        self._drag_moved:  bool = False

        self._position_initial(st)

        # Schedule a background data fetch every 5 s (no I/O on main thread)
        self._data_timer = QTimer(self)
        self._data_timer.timeout.connect(self._schedule_fetch)
        self._data_timer.start(config.REFRESH_SECONDS * 1000)

        # Pin to screen every 2 s
        self._pin_timer = QTimer(self)
        self._pin_timer.timeout.connect(lambda: make_always_visible(self))
        self._pin_timer.start(2000)

        # Pty refresh every AUTO_REFRESH_SECONDS — unconditional (see below)
        self._auto_refresh_timer = QTimer(self)
        self._auto_refresh_timer.timeout.connect(self._auto_refresh_tick)
        self._auto_refresh_timer.start(config.AUTO_REFRESH_SECONDS * 1000)

        # 20 fps animation tick — also applies staged data from bg thread
        self._anim_phase = 0.0
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick_anim)
        self._anim_timer.start(50)

        from datetime import datetime
        print(f"[{datetime.now():%H:%M:%S}] claude-meter started; "
              f"pty auto-refresh every {config.AUTO_REFRESH_SECONDS}s (unconditional)",
              file=sys.stderr, flush=True)

        # Kick off the first fetch immediately in the background
        self._schedule_fetch()

    # ── Timers ────────────────────────────────────────────────────────────────

    def _tick_anim(self) -> None:
        self._anim_phase = (self._anim_phase + 0.04) % (2 * math.pi)
        # Apply any data fetched by the background thread
        with self._data_lock:
            staged = self._staged
            self._staged = None
        if staged is not None:
            self._five_hour = staged["five_hour"]
            self._weekly    = staged["weekly"]
            self._burn_tpm  = staged["burn_tpm"]
            self._official  = staged["official"]
            if staged["official"] is not None:
                self._last_good_official = staged["official"]
            if self._refresh_pending:
                cur = (self._official or {}).get("captured_at") if self._official else None
                if cur and cur != self._refresh_baseline_ts:
                    self._refresh_pending = False
                    self._refresh_baseline_ts = None
                    self._refresh_recycled = False
                    self._refresh_pending_since = 0.0
        self.update()

    def _auto_refresh_tick(self) -> None:
        """Always fire a pty call — captured_at updates every 30 s even when
        rate-limit VALUES haven't changed, so age-gating gives false freshness."""
        import time
        from datetime import datetime
        ts = datetime.now().strftime("%H:%M:%S")
        if self._refresh_pending:
            # WATCHDOG. If "in-flight" has outlived the 130s give-up timer plus
            # margin, the singleShot meant to clear it never ran — the process
            # was suspended across a reboot / display-sleep, so the flag is
            # stuck and every tick logs "skip (in-flight)" forever. That is the
            # bug that froze the meter after a restart. Force-clear so the loop
            # self-heals on the very next tick instead of needing a manual fix.
            if self._refresh_pending_since and (
                time.monotonic() - self._refresh_pending_since > 150
            ):
                print(f"[{ts}] auto-refresh tick: WATCHDOG force-clear (stuck in-flight)",
                      file=sys.stderr, flush=True)
                self._refresh_pending = False
                self._refresh_recycled = False
                self._refresh_pending_since = 0.0
            else:
                print(f"[{ts}] auto-refresh tick: skip (in-flight)", file=sys.stderr, flush=True)
                return
        print(f"[{ts}] auto-refresh tick: FIRING", file=sys.stderr, flush=True)
        self._run_refresh()

    def _run_refresh(self) -> None:
        if self._refresh_pending:
            return
        import time
        self._refresh_pending = True
        self._refresh_recycled = False
        self._refresh_pending_since = time.monotonic()
        self._refresh_baseline_ts = (self._official or {}).get("captured_at") if self._official else None
        for ms in (800, 1600, 2400, 3200, 4500, 6000, 8000, 11000, 14000):
            QTimer.singleShot(ms, self._refresh_data)
        QTimer.singleShot(7_000, self._maybe_recycle_pty)
        for ms in (9000, 11000, 13000, 16000, 19000, 22000):
            QTimer.singleShot(ms, self._refresh_data)
        for ms in (30_000, 40_000, 55_000, 70_000, 90_000, 110_000, 125_000):
            QTimer.singleShot(ms, self._refresh_data)
        QTimer.singleShot(130_000, self._clear_refresh_pending)
        self.update()
        # Run pty I/O off the Qt main thread — spawn() sleeps 6+ s which
        # would freeze the event loop and cause a beach ball.
        threading.Thread(target=self._pty_refresh_bg, daemon=True).start()

    def _pty_refresh_bg(self) -> None:
        try:
            from . import pty_session
            pty_session.refresh()
        except Exception:
            pass

    def _maybe_recycle_pty(self) -> None:
        if not self._refresh_pending or self._refresh_recycled:
            return
        self._refresh_recycled = True
        threading.Thread(target=self._pty_recycle_bg, daemon=True).start()

    def _pty_recycle_bg(self) -> None:
        try:
            from . import pty_session
            pty_session.recycle()
        except Exception:
            pass

    def _clear_refresh_pending(self) -> None:
        if self._refresh_pending:
            self._refresh_pending = False
            self._refresh_baseline_ts = None
            self._refresh_recycled = False
            self._refresh_pending_since = 0.0
            self.update()

    # ── Data (all disk I/O runs on a daemon thread) ───────────────────────────

    def _schedule_fetch(self) -> None:
        """Enqueue a background data fetch. No-op if one is already running."""
        if self._fetch_busy:
            return
        self._fetch_busy = True
        threading.Thread(target=self._fetch_bg, daemon=True).start()

    def _fetch_bg(self) -> None:
        """Background thread: read all data, stage it for the main thread."""
        try:
            now = counter.now_utc()
            five_hour = counter.stats_for_window(now, config.FIVE_HOUR_WINDOW)
            weekly    = counter.stats_for_window(now, config.WEEKLY_WINDOW)
            burn_tpm  = counter.burn_rate_last_n_minutes(now, 30.0)
            official  = counter.read_official_rate_limits()
            with self._data_lock:
                self._staged = dict(five_hour=five_hour, weekly=weekly,
                                    burn_tpm=burn_tpm, official=official)
        except Exception:
            pass
        finally:
            self._fetch_busy = False

    # _refresh_data kept as an alias so QTimer.singleShot call sites in
    # _run_refresh continue to work without change.
    def _refresh_data(self) -> None:
        self._schedule_fetch()

    def _official_pct(self, key: str, src: dict | None = None) -> float | None:
        data = src if src is not None else self._official
        if not data:
            return None
        block = (data.get("rate_limits") or {}).get(key)
        return float(block["used_percentage"]) / 100.0 if block else None

    def _data_age_seconds(self) -> float | None:
        cap = (self._official or {}).get("captured_at") if self._official else None
        if not cap:
            return None
        from datetime import datetime, timezone
        try:
            ts = datetime.fromisoformat(str(cap).replace("Z", "+00:00"))
            return (datetime.now(timezone.utc) - ts).total_seconds()
        except Exception:
            return None

    # ── Positioning ───────────────────────────────────────────────────────────

    def _position_initial(self, st: dict) -> None:
        try:
            x, y = int(st["x"]), int(st["y"])
            if self._point_on_any_screen(x, y):
                self.move(x, y)
                return
        except (KeyError, TypeError, ValueError):
            pass
        geo = self._preferred_screen().availableGeometry()
        self.move(geo.left() + self.MARGIN, geo.top() + self.MARGIN)

    def _preferred_screen(self):
        for s in QApplication.instance().screens():
            n = (s.name() or "").lower()
            if "color lcd" in n or "built-in" in n or "builtin" in n:
                return s
        return QApplication.instance().primaryScreen()

    def _point_on_any_screen(self, x: int, y: int) -> bool:
        return any(s.availableGeometry().contains(x + 4, y + 4)
                   for s in QApplication.instance().screens())

    def _clamp_to_screen(self) -> None:
        p = self.pos()
        if not self._point_on_any_screen(p.x(), p.y()):
            geo = self._preferred_screen().availableGeometry()
            self.move(geo.left() + self.MARGIN, geo.top() + self.MARGIN)

    # ── Collapse ──────────────────────────────────────────────────────────────

    def _set_collapsed(self, v: bool) -> None:
        if v == self._collapsed:
            return
        self._collapsed = v
        self.setFixedSize(self.DOT_W if v else self.WIDTH,
                          self.DOT_H if v else self.HEIGHT)
        self._clamp_to_screen()
        _save_ui_state(collapsed=v)
        self.update()

    def _chev_rect(self) -> QRect:
        return QRect(self.CHEV_MARGIN, self.CHEV_MARGIN, self.CHEV_SIZE, self.CHEV_SIZE)

    def _refresh_rect(self) -> QRect:
        return QRect(self.CHEV_MARGIN * 2 + self.CHEV_SIZE, self.CHEV_MARGIN,
                     self.CHEV_SIZE, self.CHEV_SIZE)

    # ── Mouse ─────────────────────────────────────────────────────────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.RightButton:
            self._set_collapsed(not self._collapsed); e.accept(); return
        if e.button() == Qt.LeftButton:
            self._drag_origin = e.globalPos() - self.frameGeometry().topLeft()
            self._drag_moved = False; e.accept(); return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if self._drag_origin is None or not (e.buttons() & Qt.LeftButton):
            return super().mouseMoveEvent(e)
        new = e.globalPos() - self._drag_origin
        if (new - self.frameGeometry().topLeft()).manhattanLength() >= self.DRAG_THRESH:
            self._drag_moved = True
        if self._drag_moved:
            self.move(new)
        e.accept()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton or self._drag_origin is None:
            return super().mouseReleaseEvent(e)
        dragged, pos = self._drag_moved, e.pos()
        self._drag_origin = None; self._drag_moved = False
        if dragged:
            p = self.pos(); _save_ui_state(x=p.x(), y=p.y()); e.accept(); return
        if self._collapsed:
            self._set_collapsed(False); e.accept(); return
        if self._chev_rect().contains(pos):
            self._set_collapsed(True); e.accept(); return
        if self._refresh_rect().contains(pos):
            self._run_refresh(); e.accept(); return
        e.accept()

    def showEvent(self, e):
        super().showEvent(e)
        make_always_visible(self)

    # ── Color / pace ──────────────────────────────────────────────────────────

    def _verdict_color(self, delta: float, palette: str = "5h") -> QColor:
        """delta = actual − expected (fraction). Positive = over pace."""
        if palette == "5h":
            stops = [
                (-0.30, QColor(  0, 240, 255)),
                (-0.15, QColor( 70, 240, 220)),
                (-0.05, QColor(120, 245, 180)),
                ( 0.05, QColor(190, 250, 130)),
                ( 0.12, QColor(180, 200, 255)),
                ( 0.25, QColor(155, 130, 255)),
                ( 1.00, QColor(110,  90, 230)),
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
                t0, c0 = stops[i - 1]
                k = max(0.0, min(1.0, (delta - t0) / max(t - t0, 1e-6)))
                return QColor(int(c0.red()   + k * (c.red()   - c0.red())),
                              int(c0.green() + k * (c.green() - c0.green())),
                              int(c0.blue()  + k * (c.blue()  - c0.blue())))
        return stops[-1][1]

    def _pace_position(self, window_hours: float) -> float:
        key = "five_hour" if window_hours <= 6 else "seven_day"
        block = ((self._official or {}).get("rate_limits") or {}).get(key) or {}
        ts = block.get("resets_at")
        if ts is not None:
            try:
                from datetime import datetime, timezone
                reset = (datetime.fromtimestamp(ts, tz=timezone.utc) if isinstance(ts, (int, float))
                         else datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
                left = max(0, (reset - datetime.now(timezone.utc)).total_seconds())
                return max(0.0, min(1.0 - left / (window_hours * 3600), 1.0))
            except Exception:
                pass
        stats = self._five_hour if window_hours <= 6 else self._weekly
        if stats is None or stats.earliest is None:
            return 0.0
        return max(0.0, min(
            (counter.now_utc() - stats.earliest).total_seconds() / (window_hours * 3600), 1.0))

    def _bright(self, c: QColor) -> QColor:
        return QColor(min(c.red() + 30, 255), min(c.green() + 30, 255), min(c.blue() + 30, 255))

    def _verdict_word(self, delta: float) -> str:
        if delta >= 0.25: return "STOP"
        if delta >= 0.12: return "SLOW"
        if delta >= 0.05: return "EASE"
        if delta >= -0.05: return "ON PACE"
        if delta >= -0.15: return "FINE"
        return "REST EASY"

    def _time_left(self, key: str, window_hours: float, stats) -> str:
        block = ((self._official or {}).get("rate_limits") or {}).get(key) or {}
        ts = block.get("resets_at")
        if ts is not None:
            try:
                from datetime import datetime, timezone
                reset = (datetime.fromtimestamp(ts, tz=timezone.utc) if isinstance(ts, (int, float))
                         else datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
                s = max(0, int((reset - datetime.now(timezone.utc)).total_seconds()))
                if s < 60:   return f"{s}s"
                if s < 3600: return f"{s // 60}m"
                h, m = s // 3600, (s % 3600) // 60
                return f"{h}h {m}m" if m else f"{h}h"
            except Exception:
                pass
        if stats is None or stats.earliest is None:
            return f"{int(window_hours)}h"
        left = max(0, window_hours * 60 - (counter.now_utc() - stats.earliest).total_seconds() / 60)
        h, m = int(left // 60), int(round(left % 60))
        return f"{h}h {m}m" if (h and m) else (f"{h}h" if h else f"{m}m")

    def _reset_wall(self, key: str) -> str:
        block = ((self._official or {}).get("rate_limits") or {}).get(key) or {}
        ts = block.get("resets_at")
        if ts is None:
            return ""
        try:
            from datetime import datetime, timezone
            reset = (datetime.fromtimestamp(ts, tz=timezone.utc) if isinstance(ts, (int, float))
                     else datetime.fromisoformat(str(ts).replace("Z", "+00:00")))
            local = reset.astimezone()
            now_l = datetime.now().astimezone()
            t = local.strftime("%-I:%M %p").lower()
            if key == "five_hour" and local.date() == now_l.date():
                return f"resets {t}"
            return f"resets {local.strftime('%a')} {t}"
        except Exception:
            return ""

    # ── Paint ─────────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self._collapsed:
            self._paint_dot(p); return

        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 17, 22, 235))
        p.drawRoundedRect(self.rect(), 18, 18)

        if self._five_hour is None or self._weekly is None:
            return

        render = self._last_good_official or self._official
        f5 = self._official_pct("five_hour", render)
        fw = self._official_pct("seven_day", render)
        if f5 is None or fw is None:
            self._paint_waiting(p); return

        t = self.RING_THICK
        ox = self.SIDE_PANEL
        oi = 20
        od = self.SIZE - 2 * oi
        ot = oi + self.RING_TOP
        ii = oi + t + self.RING_GAP
        id_ = self.SIZE - 2 * ii
        it = ii + self.RING_TOP

        r5  = (ox + oi, ot, od, od)
        rw  = (ox + ii, it, id_, id_)
        p5  = self._pace_position(config.FIVE_HOUR_WINDOW)
        pw  = self._pace_position(config.WEEKLY_WINDOW)
        d5  = min(f5, 1.0) - p5
        dw  = min(fw, 1.0) - pw
        c5  = self._verdict_color(d5, "5h")
        cw  = self._verdict_color(dw, "weekly")
        tp5 = p5  # time pressure = pace elapsed
        self._draw_ring(p, r5, t, min(f5, 1.0), f5, c5, p5, tp5, self._burn_tpm)
        self._draw_ring(p, rw, t, min(fw, 1.0), fw, cw, pw, pw, 0.0)
        self._draw_pace_marker(p, r5, t, p5, max(0.0, min(d5 * 2, 1.0)))
        self._draw_pace_marker(p, rw, t, pw, max(0.0, min(dw * 2, 1.0)))
        self._draw_ring_pct(p, r5, f5, c5)
        self._draw_ring_pct(p, rw, fw, cw)
        self._draw_center_text(p, f5, d5)
        self._draw_side_panel(p, f5, fw, d5, dw, p5, pw, c5, cw)
        self._draw_buttons(p)

    def _draw_ring(self, p, rect, thick, frac, raw, color, pace, tp, burn):
        x, y, w, h = rect
        # track
        trk = QPen(QColor(255, 255, 255, int(14 + 14 * tp)))
        trk.setWidth(max(2, thick - 4)); trk.setCapStyle(Qt.RoundCap)
        p.setPen(trk); p.drawArc(x, y, w, h, 0, 360 * 16)
        if frac <= 0:
            return
        # fill arc
        fp = QPen(color); fp.setWidth(thick); fp.setCapStyle(Qt.FlatCap)
        p.setPen(fp)
        p.drawArc(x, y, w, h, 90 * 16, -int(frac * 360 * 16))
        if frac > 0.005:
            cp = QPen(color); cp.setWidth(thick); cp.setCapStyle(Qt.RoundCap)
            p.setPen(cp)
            p.drawArc(x, y, w, h, int((90 - frac * 360) * 16), -8)
        # comet tail
        if burn > 0:
            deg = self.MAX_TAIL_DEG * min(burn / self.BURN_FULL_TPM, 1.0)
            tc = QColor(color); tc.setAlpha(180)
            tp2 = QPen(tc); tp2.setWidth(thick + 2); tp2.setCapStyle(Qt.RoundCap)
            p.setPen(tp2)
            lead = 90 - 360 * frac
            p.drawArc(x, y, w, h, int((lead + deg) * 16), -int(deg * 16))
        # overflow dashed
        if raw > 1.0:
            ov = min(raw - 1.0, 0.5)
            dp = QPen(color); dp.setWidth(thick); dp.setCapStyle(Qt.FlatCap)
            dp.setStyle(Qt.DashLine); p.setPen(dp)
            p.drawArc(x, y, w, h, 90 * 16, -int(ov * 360 * 16))

    def _draw_pace_marker(self, p, rect, thick, pace, pulse_i=0.0):
        if pace <= 0:
            return
        x, y, w, h = rect
        cx, cy, r = x + w / 2, y + h / 2, w / 2
        ang = math.radians(90 - 360 * pace)
        half = thick / 2 + 3
        x1 = cx + math.cos(ang) * (r - half); y1 = cy - math.sin(ang) * (r - half)
        x2 = cx + math.cos(ang) * (r + half); y2 = cy - math.sin(ang) * (r + half)
        if pulse_i > 0:
            pulse = (math.sin(self._anim_phase * (1 + pulse_i * 2)) + 1) / 2
            hp = QPen(QColor(255, 255, 255, int(60 + 80 * pulse * pulse_i)))
            hp.setWidth(int(6 + 4 * pulse_i)); hp.setCapStyle(Qt.RoundCap)
            p.setPen(hp); p.drawLine(int(x1), int(y1), int(x2), int(y2))
        for pen in (QPen(QColor(0, 0, 0, 200)), QPen(QColor(255, 255, 255, 255))):
            pen.setWidth(4 if pen.color().alpha() < 255 else 2)
            pen.setCapStyle(Qt.RoundCap); p.setPen(pen)
            p.drawLine(int(x1), int(y1), int(x2), int(y2))

    def _draw_ring_pct(self, p, rect, frac, color):
        x, y, w, h = rect
        tx, ty = x + w / 2, y + h
        txt = f"{int(round(frac * 100))}%"
        f = QFont("Helvetica Neue"); f.setPointSize(11); f.setBold(True)
        p.setFont(f); fm = p.fontMetrics()
        pw2, ph = fm.horizontalAdvance(txt), fm.ascent()
        pad = 6, 3
        pill_w, pill_h = pw2 + pad[0] * 2, ph + pad[1] * 2
        px, py = int(tx - pill_w / 2), int(ty - pill_h / 2)
        p.setPen(Qt.NoPen); p.setBrush(QColor(15, 17, 22, 220))
        p.drawRoundedRect(px, py, pill_w, pill_h, 6, 6)
        p.setPen(color)
        p.drawText(int(tx - pw2 / 2), int(py + pad[1] + ph - 1), txt)

    def _draw_buttons(self, p):
        import math as _m
        for rect, draw_fn in ((self._chev_rect(), self._draw_chev),
                              (self._refresh_rect(), self._draw_refresh_icon)):
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(255, 255, 255, 26 if rect == self._chev_rect() else 36))
            p.drawEllipse(rect)
            draw_fn(p, rect)

    def _draw_chev(self, p, rect):
        pen = QPen(QColor(230, 230, 240, 220)); pen.setWidth(2)
        pen.setCapStyle(Qt.RoundCap); pen.setJoinStyle(Qt.RoundJoin)
        p.setPen(pen)
        cx, cy, s = rect.x() + rect.width() / 2, rect.y() + rect.height() / 2, 4.0
        p.drawLine(int(cx - s), int(cy - s + 1), int(cx), int(cy + s - 1))
        p.drawLine(int(cx), int(cy + s - 1), int(cx + s), int(cy - s + 1))

    def _draw_refresh_icon(self, p, rect):
        import math as _m
        cx, cy = rect.x() + rect.width() / 2, rect.y() + rect.height() / 2
        r = rect.width() / 2 - 5
        pen = QPen(QColor(230, 230, 240, 230)); pen.setWidth(2); pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen); p.setBrush(Qt.NoBrush)
        p.drawArc(int(cx - r), int(cy - r), int(r * 2), int(r * 2), 90 * 16, -300 * 16)
        er = _m.radians(90 - 300)
        tx, ty = cx + r * _m.cos(er), cy - r * _m.sin(er)
        for da in (0.5, -0.5):
            ax, ay = tx + 4 * _m.cos(er + _m.pi / 2 + da), ty - 4 * _m.sin(er + _m.pi / 2 + da)
            p.drawLine(int(tx), int(ty), int(ax), int(ay))

    def _paint_dot(self, p):
        """Collapsed pill — '72%  ·  2h30m' on a dark rounded rect."""
        r = self.rect()
        radius = self.DOT_H / 2

        # Gather data
        f5 = 0.0; color = QColor(150, 150, 170, 220); over = 0.0
        if self._five_hour is not None and self._official:
            f5 = self._official_pct("five_hour") or 0.0
            pace = self._pace_position(config.FIVE_HOUR_WINDOW)
            delta = min(f5, 1.0) - pace
            color = self._verdict_color(delta, "5h")
            over = max(0.0, delta)

        # Pulse halo behind the pill when over-pace
        if over > 0.05:
            pulse = (math.sin(self._anim_phase * (1 + over * 2)) + 1) / 2
            halo = QColor(color); halo.setAlpha(int(35 + 65 * pulse * min(over * 4, 1.0)))
            hp = QPen(halo); hp.setWidth(3)
            p.setPen(hp); p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(-3, -3, 3, 3), radius + 3, radius + 3)

        # Pill body
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(15, 17, 22, 245))
        p.drawRoundedRect(r, radius, radius)

        # Thin fill bar at the very bottom of the pill
        bar_pad = 6; bar_h = 3
        bar_x = bar_pad; bar_y = r.height() - bar_pad
        bar_w = r.width() - bar_pad * 2
        p.setBrush(QColor(255, 255, 255, 22))
        p.drawRoundedRect(bar_x, bar_y, bar_w, bar_h, 1, 1)
        fill_w = int(bar_w * min(f5, 1.0))
        if fill_w > 1:
            p.setBrush(color)
            p.drawRoundedRect(bar_x, bar_y, fill_w, bar_h, 1, 1)

        # Label  "72%  ·  2h30m"
        pct = f"{int(round(min(f5, 1.0) * 100))}%" if self._official else "–"
        tl  = (self._time_left("five_hour", config.FIVE_HOUR_WINDOW, self._five_hour)
               if f5 > 0 else "")
        label = f"{pct}  ·  {tl}" if tl else pct

        fnt = QFont("Helvetica Neue"); fnt.setPointSize(12); fnt.setBold(True)
        p.setFont(fnt); fm = p.fontMetrics()
        text_y = int((r.height() - bar_h - bar_pad) / 2 + fm.ascent() / 2)
        p.setPen(self._bright(color))
        p.drawText(int(r.width() / 2 - fm.horizontalAdvance(label) / 2), text_y, label)

        # Border ring
        bp = QPen(QColor(color.red(), color.green(), color.blue(), 120)); bp.setWidth(1)
        p.setPen(bp); p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r.adjusted(0, 0, -1, -1), radius, radius)

    def _paint_waiting(self, p):
        ox = self.SIDE_PANEL; oi = 14; od = self.SIZE - 2 * oi; ot = oi + self.RING_TOP
        ii = oi + self.RING_THICK + self.RING_GAP; id_ = self.SIZE - 2 * ii; it = ii + self.RING_TOP
        for idx, (rect, t) in enumerate([
            ((ox + oi, ot, od, od), self.RING_THICK),
            ((ox + ii, it, id_, id_), self.RING_THICK),
        ]):
            x, y, w, h = rect
            tp = QPen(QColor(255, 255, 255, 20)); tp.setWidth(t); tp.setCapStyle(Qt.RoundCap)
            p.setPen(tp); p.drawArc(x, y, w, h, 0, 360 * 16)
            sp = QPen(QColor(110, 220, 255, 110)); sp.setWidth(t); sp.setCapStyle(Qt.RoundCap)
            p.setPen(sp)
            phase = self._anim_phase * (1.0 if idx == 0 else 0.7)
            start = int(((phase / (2 * math.pi)) * 360 + idx * 180) * 16) % (360 * 16)
            p.drawArc(x, y, w, h, start, -int(40 * 16))
        cx = self.SIDE_PANEL + self.SIZE / 2; cy = self.SIZE / 2 + self.RING_TOP
        f = QFont("Helvetica Neue"); f.setPointSize(9); f.setBold(True); p.setFont(f)
        p.setPen(QColor(255, 255, 255, 130))
        fm = p.fontMetrics(); msg = "—"
        p.drawText(int(cx - fm.horizontalAdvance(msg) / 2), int(cy - 4), msg)
        f2 = QFont("Helvetica Neue"); f2.setPointSize(7); p.setFont(f2)
        p.setPen(QColor(255, 255, 255, 90))
        fm2 = p.fontMetrics()
        for i, sub in enumerate(("no live data", "open a claude session")):
            p.drawText(int(cx - fm2.horizontalAdvance(sub) / 2), int(cy + 10 + i * 12), sub)

    def _draw_side_panel(self, p, f5, fw, d5, dw, p5, pw, c5, cw):
        xl, xtrack, tw = 14, 14, self.SIDE_PANEL - 28
        pulse = (math.sin(self._anim_phase) + 1) / 2
        vf = QFont("Helvetica Neue"); vf.setPointSize(12); vf.setBold(True)
        rf = QFont("Helvetica Neue"); rf.setPointSize(9)
        rows = [("clock", f5, p5, c5, "five_hour"), ("calendar", fw, pw, cw, "seven_day")]
        for i, (icon, fill, time_v, color, rl_key) in enumerate(rows):
            ytop = 38 + i * 60; ytrack = ytop + 22
            # icon
            self._draw_icon(p, icon, xl, ytop - 4, color)
            sf = QFont("Helvetica Neue"); sf.setPointSize(9); sf.setBold(True); p.setFont(sf)
            p.setPen(QColor(255, 255, 255, 220))
            p.drawText(xl + 20, ytop + 8, "5h" if icon == "clock" else "wk")
            # rail
            ro = 46; tleft = xtrack + ro; tright = tleft + (tw - ro); tspan = tright - tleft
            rp2 = QPen(QColor(255, 255, 255, 55)); rp2.setWidth(4); rp2.setCapStyle(Qt.RoundCap)
            p.setPen(rp2); p.drawLine(tleft, ytrack, tright, ytrack)
            fx = int(tleft + min(fill, 1.0) * tspan)
            tx2 = int(tleft + min(time_v, 1.0) * tspan)
            # fill
            fp2 = QPen(color); fp2.setWidth(6); fp2.setCapStyle(Qt.RoundCap)
            p.setPen(fp2); p.drawLine(tleft, ytrack, fx, ytrack)
            # knob halo
            hr = int(11 + pulse * 3)
            hc = QColor(color); hc.setAlpha(int(60 + pulse * 90))
            p.setPen(Qt.NoPen); p.setBrush(hc)
            p.drawEllipse(fx - hr, ytrack - hr, hr * 2, hr * 2)
            p.setBrush(self._bright(color))
            p.drawEllipse(fx - 6, ytrack - 6, 12, 12)
            # time tick
            for pen in (QPen(QColor(0, 0, 0, 200)), QPen(QColor(255, 255, 255, 235))):
                pen.setWidth(5 if pen.color().alpha() < 255 else 2); pen.setCapStyle(Qt.RoundCap)
                p.setPen(pen); p.drawLine(tx2, ytrack - 7, tx2, ytrack + 7)
            # pct value
            p.setFont(vf); p.setPen(self._bright(color))
            pct = f"{int(round(min(fill, 1.0) * 100))}%"
            fm = p.fontMetrics()
            p.drawText(int(tright - fm.horizontalAdvance(pct)), int(ytop - 2), pct)
            # reset time
            rt = self._reset_wall(rl_key)
            if rt:
                p.setFont(rf); dc = QColor(color); dc.setAlpha(215)
                p.setPen(dc); p.drawText(tleft, ytrack + 17, rt)
        # ref timestamp
        cap = (self._official or {}).get("captured_at") if self._official else None
        if cap:
            from datetime import datetime
            try:
                ts = datetime.fromisoformat(str(cap).replace("Z", "+00:00"))
                ls = ts.astimezone().strftime("ref %H:%M:%S")
                tf = QFont("Helvetica Neue"); tf.setPointSize(8); p.setFont(tf)
                p.setPen(QColor(200, 200, 215, 150))
                p.drawText(xl, self.HEIGHT - 10, ls)
            except Exception:
                pass

    def _draw_icon(self, p, kind, x, y, color):
        size = 16; rect = QRect(x, y, size, size)
        pen = QPen(color); pen.setWidth(1); p.setPen(pen); p.setBrush(Qt.NoBrush)
        if kind == "clock":
            p.drawEllipse(rect)
            cx2, cy2 = x + size / 2, y + size / 2
            p.drawLine(int(cx2), int(cy2), int(cx2), y + 4)
            p.drawLine(int(cx2), int(cy2), x + size - 4, int(cy2))
        else:
            p.drawRoundedRect(rect, 2, 2); p.drawLine(x, y + 5, x + size, y + 5)
            p.drawLine(x + 4, y, x + 4, y + 3); p.drawLine(x + size - 4, y, x + size - 4, y + 3)

    def _draw_center_text(self, p, f5, d5):
        cx = self.SIDE_PANEL + self.SIZE / 2; cy = self.SIZE / 2 + self.RING_TOP
        c = self._verdict_color(d5, "5h")
        dim = QColor(c); dim.setAlpha(190)
        bright = self._bright(c)
        lf = QFont("Helvetica Neue"); lf.setPointSize(11); p.setFont(lf)
        fm = p.fontMetrics(); lh = fm.height()
        lines = [
            (f"{int(round(min(f5, 1.0) * 100))}% USED", QFont.Medium, dim),
            (self._verdict_word(d5),                     QFont.Black,  bright),
            (self._time_left("five_hour", config.FIVE_HOUR_WINDOW, self._five_hour), QFont.Bold, c),
        ]
        top = cy - lh * 1.5 + fm.ascent()
        for i, (txt, wt, col) in enumerate(lines):
            f2 = QFont(lf); f2.setWeight(wt); p.setFont(f2); p.setPen(col)
            fm2 = p.fontMetrics()
            p.drawText(int(cx - fm2.horizontalAdvance(txt) / 2), int(top + i * lh), txt)
        # stale warning
        age = self._data_age_seconds()
        if age is not None and age > 150:
            mins = int(age // 60)
            stxt = f"stale {mins}m" if mins >= 1 else f"stale {int(age)}s"
            sf = QFont("Helvetica Neue"); sf.setPointSize(8); sf.setBold(True); p.setFont(sf)
            fm3 = p.fontMetrics(); sw = fm3.horizontalAdvance(stxt); sh = fm3.height()
            px3, py3 = 6, 2; pw3 = sw + px3 * 2; ph3 = sh + py3
            px4 = int(cx - pw3 / 2); py4 = int(top + 3 * lh + 4)
            p.setPen(Qt.NoPen); p.setBrush(QColor(200, 100, 60, 220))
            p.drawRoundedRect(px4, py4, pw3, ph3, 6, 6)
            p.setPen(QColor(15, 17, 22, 255))
            p.drawText(int(cx - sw / 2), int(py4 + py3 + fm3.ascent() - 1), stxt)


def main() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    widget = MeterWidget()
    widget.show()
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
