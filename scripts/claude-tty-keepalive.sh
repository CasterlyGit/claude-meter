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

# Newer Claude Code versions show a "Is this a project you trust?" dialog
# on first launch in a given cwd. The default option is "Yes, I trust this
# folder" — pressing Enter accepts it. Without this, the headless TUI sits
# on the dialog forever and the statusline never fires, so rate-limits.json
# freezes. We feed an initial newline (to dismiss the dialog at the same
# trust scope a human would grant) and then keep stdin open with `cat` so
# claude doesn't see EOF and exit. Trust scope here is identical to clicking
# "Yes" yourself — no additional permission bypass.
# Send an initial newline (dismiss trust dialog), then send "ok" every
# 3 minutes to trigger an API call so the statusline gets fresh rate-limit
# headers — critical after a 5h window reset when the pty has old data.
# CC 2.1.x opens on a startup screen (trust dialog / "bypass permissions"
# banner) where a bare CR does NOT submit the typed text as a message — the
# turn stalls ("Moseying… 0/4") and the statusline never gets fresh
# rate-limit headers, so rate-limits.json freezes. Fix: after the boot grace
# period, type the prompt characters, pause, THEN send CR on its own so the
# TUI registers it as Enter-to-submit. Repeat every 180s.
exec /usr/bin/script -q -F /dev/null /usr/local/bin/claude \
  < <(sleep 10; printf '\r'; sleep 2; \
      while true; do printf 'ok'; sleep 1; printf '\r'; sleep 180; done) \
  >/tmp/claude-tty-keepalive.log 2>&1
