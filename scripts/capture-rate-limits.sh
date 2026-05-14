#!/bin/bash
# Reads the statusline JSON Claude Code feeds on stdin, extracts the
# rate_limits block, writes it to ~/.claude/state/rate-limits.json.
# Also prints a short status line so the terminal's statusline area
# isn't blank.

mkdir -p ~/.claude/state

INPUT=$(cat)
# Capture rate-limits block (passthrough whole input; consumers can pick fields)
echo "$INPUT" > ~/.claude/state/last-statusline.json

# Extract just rate_limits + a timestamp into a smaller dedicated file
TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "$INPUT" | jq --arg ts "$TS" '{
  captured_at: $ts,
  rate_limits: (.rate_limits // null),
  model: .model,
  cost: .cost,
  context_window: .context_window
}' > ~/.claude/state/rate-limits.json 2>/dev/null

# Print a minimal status line so something is visible (avoids ugly blank).
FIVE=$(echo "$INPUT" | jq -r '.rate_limits.five_hour.used_percentage // empty' 2>/dev/null)
WEEK=$(echo "$INPUT" | jq -r '.rate_limits.seven_day.used_percentage // empty' 2>/dev/null)
OUT=""
[ -n "$FIVE" ] && OUT="5h:$(printf '%.0f' "$FIVE")%"
[ -n "$WEEK" ] && OUT="$OUT  wk:$(printf '%.0f' "$WEEK")%"
echo "$OUT"
