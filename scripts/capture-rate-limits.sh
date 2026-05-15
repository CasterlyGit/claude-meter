#!/bin/bash
# Reads the statusline JSON Claude Code feeds on stdin, extracts the
# rate_limits block, writes it to ~/.claude/state/rate-limits.json.
#
# Two guards against bad writes:
#
# 1. NULL guard. A fresh `claude` session that hasn't made an API call yet
#    feeds a payload where `rate_limits` is null. Don't overwrite the last
#    good reading with that.
#
# 2. STALE-VALUE guard. Multiple claude sessions on the same machine can
#    fire this same hook on their own statusline ticks. They will each
#    report their own per-session rate-limit view, which can be LOWER than
#    a more recent reading from another session. Inside a single 5h
#    window the per-account total only ever goes UP (or stays equal until
#    reset). So if the incoming value would lower the recorded percentage
#    while the existing record's resets_at is still in the future, we treat
#    the incoming reading as a stale per-session echo and ignore it.

set -e

mkdir -p ~/.claude/state

INPUT=$(cat)
STATE_DIR="$HOME/.claude/state"
RL_PATH="$STATE_DIR/rate-limits.json"

# Always update the passthrough copy so we can debug.
echo "$INPUT" > "$STATE_DIR/last-statusline.json"

HAS_RL=$(echo "$INPUT" | jq -r '.rate_limits != null' 2>/dev/null)

if [ "$HAS_RL" = "true" ]; then
  TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)

  # Pull incoming + existing 5h % and reset epoch for the stale guard.
  NEW_5H=$(echo "$INPUT" | jq -r '.rate_limits.five_hour.used_percentage // 0' 2>/dev/null)
  NEW_RESET=$(echo "$INPUT" | jq -r '.rate_limits.five_hour.resets_at // 0' 2>/dev/null)

  ACCEPT=1
  if [ -f "$RL_PATH" ]; then
    EXIST_5H=$(jq -r '.rate_limits.five_hour.used_percentage // 0' "$RL_PATH" 2>/dev/null)
    EXIST_RESET=$(jq -r '.rate_limits.five_hour.resets_at // 0' "$RL_PATH" 2>/dev/null)
    NOW_EPOCH=$(date +%s)

    # Only apply the monotonic guard when we're inside the SAME 5h window
    # (existing resets_at is in the future and matches the new one).
    SAME_WINDOW=$(awk -v a="$EXIST_RESET" -v b="$NEW_RESET" -v now="$NOW_EPOCH" 'BEGIN { print (a == b && a > now) ? 1 : 0 }')

    if [ "$SAME_WINDOW" = "1" ]; then
      # Compare floats. Reject the write if the new value is more than 0.5%
      # below the existing one — that's a per-session stale echo overwriting
      # a higher (more recent) reading. Small downward drift (≤0.5) can
      # happen because of rounding, allow it.
      LOWER=$(awk -v new="$NEW_5H" -v old="$EXIST_5H" 'BEGIN { print (new + 0.5 < old) ? 1 : 0 }')
      if [ "$LOWER" = "1" ]; then
        ACCEPT=0
      fi
    fi
  fi

  if [ "$ACCEPT" = "1" ]; then
    echo "$INPUT" | jq --arg ts "$TS" '{
      captured_at: $ts,
      rate_limits: .rate_limits,
      model: .model,
      cost: .cost,
      context_window: .context_window
    }' > "$RL_PATH" 2>/dev/null
  fi
fi

# Print a minimal status line for the statusline area.
FIVE=$(echo "$INPUT" | jq -r '.rate_limits.five_hour.used_percentage // empty' 2>/dev/null)
WEEK=$(echo "$INPUT" | jq -r '.rate_limits.seven_day.used_percentage // empty' 2>/dev/null)
OUT=""
[ -n "$FIVE" ] && OUT="5h:$(printf '%.0f' "$FIVE")%"
[ -n "$WEEK" ] && OUT="$OUT  wk:$(printf '%.0f' "$WEEK")%"
echo "$OUT"
