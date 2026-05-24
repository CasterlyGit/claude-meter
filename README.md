# claude-meter

[Live demo →](https://casterlygit.github.io/claude-meter/)

A tiny always-on-top dashboard that keeps your Claude Code rate-limit windows in the corner of your eye. Two concentric rings — 5-hour outside, weekly inside — synthwave palette, every visual property doing real work. No labels cluttering the widget, no estimates: it reads the same numbers Claude Code's own `/usage` panel pulls from Anthropic.

**Status:** v0.3 — wall-clock reset times on the left panel, `ON PACE` promoted to the center of the rings, collapsed view is now a fill-from-bottom progress pillar (not an Apple-style ring), and a 10-minute self-refresh keeps the numbers live without you clicking anything.

## What the rings actually say

Every visual property carries information:

| What you see | What it means |
|---|---|
| **Arc length** | % of that window's ceiling you've spent |
| **Hue family** | Outer = 5h (cyan → coral → magenta). Inner = weekly (lime → amber → red-orange) |
| **Hue tier** | Calm / warning / danger — shifts at 65% and 85% |
| **Pace tick on the track** | Where you'd be at linear pace. Arc past the tick = burning hot |
| **Comet tail (outer ring)** | Length is proportional to your tokens-per-minute over the last 5 minutes |
| **Dashed overflow** | Past 100%, arc continues dashed into a second lap |
| **Center stack** | `NN% USED` / `ON PACE` / `4h 43m` — same color, three weight tiers, verdict in the middle |
| **Pills inside each ring** | The literal % for that window, color-matched, near the bottom of the ring |
| **Side panel (left)** | Two rails (5h, weekly) with the colored fill + a white tick for pace; under each rail the wall-clock reset time (`resets 11:43 pm`, `resets Sun 11:43 pm`) so you know *when*, not just *how long* |

## Why this exists

Anthropic doesn't expose your 5-hour / weekly quota as a queryable API. The Claude Code app shows the gauge when you click into it; the desktop app and VS Code extension don't write it anywhere external processes can read. claude-meter pulls it from the statusline hook (the one path Anthropic does expose) and pins it where you can glance at it.

The whole reason this exists: knowing how much of your 5h window is left changes how you plan a session. If you're at 70% with 90 minutes to go, you slow down. If you're at 12% with 30 minutes left, you push.

## Getting it out of the way

The widget pins to the top-right of your rightmost monitor — right where macOS menu-bar dropdowns and Spotlight render. So:

- **Click the chevron** (top-right of the widget) → collapses to a small **progress pillar**: a dark circle that fills bottom-up by your 5-hour percentage, framed in the urgency color. Climbs visibly as you spend, so the collapsed view is enough on its own — no need to re-expand to know where you are.
- **Click the pillar** → expands back to the full meter.

42px when collapsed. Plenty of room for menu items, Spotlight, notification flyouts.

## Refresh — automatic every 10 min, or click for an instant one

The numbers only update when *something* makes a Claude API call (a terminal `claude` rendering its statusline, the desktop Claude.app, or the VS Code extension). If you're working in the apps but not the terminal, the file freezes.

The meter fixes that itself: a background timer fires a refresh every `AUTO_REFRESH_SECONDS` (default 600s = 10 min), and *only* when the captured data is actually stale — if a real interactive session is keeping the statusline warm, the timer rides along for free and spends nothing. So the collapsed pillar keeps climbing whether you've expanded the widget or not.

For instant updates: **click the circular-arrow button** (left of the chevron). The meter owns a hidden, headless `claude` TUI running inside a pseudo-terminal — no visible window, no dock icon — and the click sends one tiny prompt to it. The TUI re-renders, the statusline hook writes fresh numbers, the meter picks them up within a few seconds. First click after a meter restart cold-boots the TUI (~5–8s). Every subsequent click is fast (~1–2s) because the TUI stays warm in the background.

Cost: roughly half a cent of Haiku tokens per click. Tiny against any 5h budget.

Guards:
- The button is **disabled while one refresh is already in flight** — can't double-spend.
- The capture script enforces a **monotonic guard** on the rate-limits file: within a single 5h window, writes that would *lower* the recorded percentage are rejected as stale per-session echoes (multiple claude sessions can race on the same file; only the highest reading wins until window reset). When the window genuinely rolls over (`resets_at` advances), the lower value is accepted as the new baseline.

## Setup

The repo ships two pieces:

1. The Python overlay — `claude-meter` command, installs into a venv.
2. A statusline hook script — `scripts/capture-rate-limits.sh`, registered with Claude Code.

```bash
git clone https://github.com/CasterlyGit/claude-meter
cd claude-meter
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Register the statusline by adding this to `~/.claude/settings.json`:

```json
{
  "statusLine": {
    "type": "command",
    "command": "/Users/<you>/.claude/scripts/capture-rate-limits.sh",
    "refreshInterval": 30
  }
}
```

The hook only overwrites the file when the payload actually contains rate-limit data — so a fresh terminal `claude` session that hasn't made an API call yet can't blank out your last good numbers.

Restart any active `claude` TUI sessions. The first time it fires it writes `~/.claude/state/rate-limits.json`. Then:

```bash
claude-meter
```

## Where the numbers come from (the honest version)

The fields are real and structured:

```json
{
  "rate_limits": {
    "five_hour": {"used_percentage": 38, "resets_at": 1778812800},
    "seven_day": {"used_percentage": 12, "resets_at": 1779037200}
  }
}
```

Both `used_percentage` and `resets_at` come straight from Anthropic — the time-left readout uses `resets_at` as the source of truth, not a guess from your transcript timestamps. The 5h window is a fixed slot with a hard reset, not a rolling-from-first-use window, and the meter respects that.

If no live data is available, the rings stay empty and the center shows "no live data." No estimates, no guessed ceilings.

**The statusline only fires inside interactive terminal sessions** — not the VS Code extension, not the desktop Claude.app. Either keep a terminal `claude` session active, or use the refresh button when you want a fresh read.

## Auto-start at login

```bash
cp scripts/com.casterly.claude-meter.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.casterly.claude-meter.plist
```

## Configuration

`src/claude_meter/config.py`:

- `ACTIVE_PLAN` — `"pro"`, `"max-5x"`, `"max-20x"`, or `"console"`
- `WARN_THRESHOLD` / `DANGER_THRESHOLD` — fraction at which the rings shift hue tier
- `REFRESH_SECONDS` — how often the meter re-reads the captured rate-limit file (default 5s)
- `AUTO_REFRESH_SECONDS` — how often the meter fires its own statusline refresh when the file has gone stale (default 600s = 10 min)
- `BURN_FULL_TAIL_TPM` in `window.py` — what tokens/min counts as a "full comet tail"

## Project layout

```
src/claude_meter/
├── counter.py        # reads ~/.claude/projects/**/*.jsonl, aggregates usage
├── config.py         # plan ceilings + thresholds
├── mac_window.py     # NSWindow pinning shim
├── pty_session.py    # persistent headless claude TUI for the refresh button
├── window.py         # the Qt widget with all the ring drawing logic
└── __main__.py       # entry point
scripts/
├── capture-rate-limits.sh           # statusline hook with monotonic guard
└── com.casterly.claude-meter.plist  # LaunchAgent template
```

## Roadmap

- [x] v0.1 — two-ring layout, statusline-driven data, synthwave palette
- [x] v0.2 — refresh button, collapse-to-dot, `resets_at`-based time, per-ring % pills, weight-graded center stack
- [x] v0.2.1 — refresh button uses a headless pty so it actually works (no popup terminal); monotonic guard prevents stale per-session writes from flicker-overwriting fresh data
- [x] v0.3 — wall-clock reset times in the side panel; verdict promoted to the center of the rings; collapsed view is a fill-from-bottom progress pillar instead of a static colored dot; 10-min self-refresh so the numbers stay live without a click
- [ ] Optional `ANTHROPIC_API_KEY` mode — one tiny ping/minute reads the rate-limit headers off the response. Costs roughly nothing in tokens, no terminal session needed. ([#1](https://github.com/CasterlyGit/claude-meter/issues/1))
- [ ] Multi-monitor positioning preference (currently pins to rightmost; some setups want primary)
- [ ] Linux support — the rings draw fine on PyQt5, but the always-on-top pin uses PyObjC which is darwin-only
- [ ] curby integration — meter overlays a tiny status puck when curby is running
