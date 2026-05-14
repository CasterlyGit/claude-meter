# claude-meter

A small always-on-top dashboard that shows your live Claude Code token usage. Two concentric rings (Apple Fitness style), every visual property encodes information, no extra widgets.

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

## How it computes usage

For every assistant response in your transcripts, Claude logs a `usage` record:

```json
{
  "input_tokens": 1,
  "cache_creation_input_tokens": 999,
  "cache_read_input_tokens": 264645,
  "output_tokens": 514
}
```

The "billed" total (what counts toward rate limits) is approximated as:

```
billed = input + output + cache_creation + (cache_read × 0.1)
```

Cache reads are heavily discounted at the rate-limit level — that's the 10% multiplier. We sum these across all transcripts whose mtime falls in the rolling window (5 hours for the outer ring, 7 days for the inner).

## Calibrating the ceilings

The plan tiers ship with rough estimates of token ceilings, calibrated against the Claude Code app's own "% used" gauges. If your in-app % and the meter's % disagree by more than a few points, edit `src/claude_meter/config.py` and adjust the ceiling for your active plan. Math:

```
ceiling = your_current_billed_tokens / (app_percent / 100)
```

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
