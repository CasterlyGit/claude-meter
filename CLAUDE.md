# claude-meter
Floating PyQt5 HUD that shows Claude Code's 5h and 7-day token rate-limit rings in real time. Status: v0.3, stable.

## Key files
- `src/claude_meter/window.py` — the entire UI: `MeterWidget` (QWidget), all paint logic, timers, drag/collapse/refresh
- `src/claude_meter/pty_session.py` — `PtySession`: headless `claude` TUI in a pty, module-level singleton; `refresh()` / `recycle()` / `shutdown()`
- `src/claude_meter/counter.py` — reads `~/.claude/projects/**/*.jsonl` transcripts for token usage; `read_official_rate_limits()` reads `~/.claude/state/rate-limits.json`
- `src/claude_meter/config.py` — `ACTIVE_PLAN`, `REFRESH_SECONDS=5`, `AUTO_REFRESH_SECONDS=120`, window sizes; plan ceilings
- `src/claude_meter/mac_window.py` — `make_always_visible()` PyObjC shim, pins widget at NSStatusWindowLevel across spaces
- `src/claude_meter/__main__.py` — `from claude_meter.window import main; raise SystemExit(main())`

## Architecture / patterns
- Two data sources: (1) official `rate-limits.json` written by the pty session's Claude Code statusline hook (authoritative); (2) transcript `.jsonl` files for burn-rate and `WindowStats`
- `MeterWidget` has three timers: `_data_timer` (5s reads data), `_pin_timer` (2s calls `make_always_visible`), `_auto_refresh_timer` (120s fires unconditional pty call), `_anim_timer` (50ms/20fps for comet + pace pulse)
- Refresh flow: `_run_refresh()` → `pty_session.refresh()` → sends `"ok\r"` to headless pty → schedules a cascade of `QTimer.singleShot` polls to detect `captured_at` change. At 7s, `_maybe_recycle_pty()` kills+respawns the pty if still pending. At 130s, `_clear_refresh_pending()` gives up.
- `_refresh_pending` flag gates stale-indicator display; cleared when `captured_at` changes
- Collapsed dot: 42×42 pill showing 5h% fill (pillar) + percent + time-left pill; right-click toggles
- `_verdict_color(delta)`: hue = actual − expected pace. Cyan = under-pace (REST EASY), purple = over-pace (STOP)
- Pace tick: thin crosshair on the ring arc at the "should be here" position
- Comet tail: burn_rate_tpm → arc length behind the fill head
- Position persisted to `~/.claude/state/claude-meter-position.json`
- `BURN_FULL_TPM = 50_000` tokens/min = full comet. Stale warning shown if `captured_at` age > 150s

## Run / test
```bash
cd /Users/casterly/Documents/Dev/claude-meter
python -m claude_meter           # or: uv run python -m claude_meter
```
No automated test suite. UI tested visually.

## Current state & active work
- Working: rings, comet, collapse, pty refresh, recycle, stale warning, pace tick, side panel rail, drag
- `_last_good_official` caches the last valid data so rings don't blank during pty refresh
- `mac_window.py` is the only file not in `__main__` imports — do not remove it
- Do NOT add skip-if-fresh logic to `_auto_refresh_tick`; that was explicitly reverted (#14)
