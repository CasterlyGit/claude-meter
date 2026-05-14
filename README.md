# claude-meter

[Live demo →](https://casterlygit.github.io/claude-meter/)

A small always-on-top dashboard that mirrors Claude Code's own `/usage` gauges. Two concentric rings, synthwave palette, every visual property encodes information without extra text.

The 5-hour ring lives on the outside. The weekly ring lives on the inside. They share a small dark card pinned to the top-right of your rightmost monitor.

## What the rings tell you

Every visual property is doing work:

| What you see | What it means |
|---|---|
| **Arc length (each ring)** | % of that window's ceiling you've spent |
| **Hue family** | Outer = 5h (cyan → coral → magenta). Inner = weekly (lime → amber → red-orange) |
| **Hue tier** | Calm / warning / danger — kicks in at 65% and 85% |
| **Pace tick on the track** | Where you'd be if you spent the budget linearly. Filled arc past the tick = burning hot |
| **Track opacity** | Brightens as the window winds down — visual time pressure |
| **Comet tail behind the leading edge (outer ring)** | Length is proportional to your tokens-per-minute over the last 5 minutes |
| **Ring thickness asymmetry** | The more-loaded window gets the thicker ring — eye drawn to where the pressure is |
| **Dashed overflow** | Past 100%, the arc continues dashed into a second lap — useful when the ceiling estimate is wrong |
| **Center number** | The 5h % (the urgent one). Tiny `wk·NN` underneath is the weekly % |

## Why this exists

Anthropic doesn't expose your 5-hour / weekly quotas as a queryable number. The Claude Code app itself shows you those gauges, but only when you click into it. claude-meter computes the same numbers from your local session transcripts (`~/.claude/projects/**/*.jsonl`) and keeps them in the corner of your eye while you work.

## Setup

The repo ships with two pieces:

1. **The Python overlay** — `claude-meter` command, installs into a venv.
2. **A statusline hook script** — `scripts/capture-rate-limits.sh`, which you wire into your Claude Code statusline.

After install (see below), register the statusline by adding this to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "/Users/<you>/.claude/scripts/capture-rate-limits.sh",
    "refreshInterval": 30
  }
}
```

Restart any active `claude` TUI sessions. The first refresh writes `~/.claude/state/rate-limits.json` and the overlay starts showing real numbers.

## Getting it out of the way

The widget pins to the top-right of your rightmost monitor — exactly where macOS menu-bar dropdowns and Spotlight render. To free that area:

- **Right-click the widget** → collapses to a tiny urgency-colored dot. The dot still pulses with the live 5-hour color, so it's not blind.
- **Click the dot** → expands back to the full meter.

The dot is 22px and leaves plenty of room for menu items, Spotlight, and notification flyouts.

## Where the numbers come from

claude-meter does **not** estimate. It reads the same numbers Claude Code's own `/usage` panel shows you, via a statusline hook:

1. The included script `~/.claude/scripts/capture-rate-limits.sh` is registered as your Claude Code statusline command.
2. Every time Claude Code renders its statusline (default: every 30s in an interactive TUI), it pipes the full UI-state JSON to that script.
3. The script writes `rate_limits.five_hour.used_percentage` and `rate_limits.seven_day.used_percentage` to `~/.claude/state/rate-limits.json`.
4. claude-meter reads that file.

If no live data is available, the rings stay empty and the center shows "no live data". No fallback estimates, no guessed ceilings.

The statusline only fires in **interactive TUI sessions** — not the VS Code extension, not the desktop Claude.app, only `claude` running in a terminal. Keep at least one terminal-based Claude Code session running and the rings will stay live.

## Install

```bash
git clone https://github.com/CasterlyGit/claude-meter
cd claude-meter
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
claude-meter
```

The window appears in the top-right of your rightmost screen. It stays above all apps on all macOS spaces (uses an NSStatusWindowLevel pin via PyObjC, same approach as curby's overlays).

## Auto-start at login

```bash
# Optional: add a LaunchAgent so it boots when you log in
cp scripts/com.casterly.claude-meter.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.casterly.claude-meter.plist
```

## Configuration

`src/claude_meter/config.py` has the knobs:

- `ACTIVE_PLAN` — `"pro"`, `"max-5x"`, `"max-20x"`, or `"console"`
- `WARN_THRESHOLD` / `DANGER_THRESHOLD` — at what fraction the rings shift hue tier
- `REFRESH_SECONDS` — how often the meter re-reads transcripts (default 5s)
- `BURN_FULL_TAIL_TPM` in `window.py` — what tokens/min counts as a "full comet tail"

## Project layout

```
src/claude_meter/
├── counter.py        # reads ~/.claude/projects/**/*.jsonl, aggregates usage
├── config.py         # plan ceilings + thresholds
├── mac_window.py     # NSWindow pinning shim (curby-derived)
├── window.py         # the Qt widget with all the ring drawing logic
└── __main__.py       # entry point
```

## Status

v0.1. Working on macOS with PyQt5 + PyObjC. Not tested on Linux/Windows (PyObjC is no-op on non-darwin; the rings will draw but the always-on-top pin won't).
