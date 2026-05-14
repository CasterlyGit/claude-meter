#!/bin/bash
# Headless claude TTY session — runs `claude` inside a pseudo-TTY so its
# statusline fires every 30s, populating ~/.claude/state/rate-limits.json.
#
# Spawned by launchd at login. If claude exits, launchd respawns this.

export PATH="/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# /usr/bin/script allocates a PTY and runs the given command attached to it.
# -q = quiet (don't print the "Script started" banner)
# -F = flush after every write (so statusline lines flow immediately)
# /dev/null as the typescript file (we don't want to log claude's output)
# stdin from /dev/null keeps claude from waiting on user input but the
# script-allocated PTY keeps it thinking it has a real terminal.

exec /usr/bin/script -q -F /dev/null /usr/local/bin/claude </dev/null >/tmp/claude-tty-keepalive.log 2>&1
