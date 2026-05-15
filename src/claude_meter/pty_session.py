"""Persistent headless claude TUI managed by the meter.

The refresh button needs the Claude Code statusline to render, which only
happens inside a real interactive TUI (not --print mode). Rather than spawn
a new claude CLI every click — slow, throws away cache, no statusline —
we own one long-lived claude process inside a pseudo-terminal. Each refresh
click writes a tiny prompt to the pty; the TUI handles it, statusline fires,
rate-limits file updates.

Zero visible windows: the pty lives entirely inside this process.

Lifecycle:
- spawn on first click (~5s cold start)
- subsequent clicks send a 3-byte input and return immediately
- if the process dies (logout, OOM, whatever), next click respawns it
- meter exits → pty is cleaned up automatically because we use a child process
"""
from __future__ import annotations

import errno
import os
import pty
import select
import shutil
import signal
import threading
import time
from pathlib import Path
from typing import Optional


CLAUDE_BIN_CANDIDATES = (
    "/usr/local/bin/claude",
    "/opt/homebrew/bin/claude",
    str(Path.home() / ".local/bin/claude"),
)


def _find_claude_bin() -> Optional[str]:
    for c in CLAUDE_BIN_CANDIDATES:
        if os.access(c, os.X_OK):
            return c
    return shutil.which("claude")


class PtySession:
    """One claude TUI running inside a pty, owned by this process."""

    def __init__(self, model: str = "haiku") -> None:
        self._model = model
        self._pid: Optional[int] = None
        self._master_fd: Optional[int] = None
        self._lock = threading.Lock()
        self._drain_thread: Optional[threading.Thread] = None
        self._stop_drain = threading.Event()

    def is_alive(self) -> bool:
        if self._pid is None:
            return False
        try:
            # signal 0 = "does the process still exist?"
            os.kill(self._pid, 0)
        except OSError:
            return False
        return True

    def spawn(self) -> bool:
        """Start the headless claude TUI. Returns True on success."""
        if self.is_alive():
            return True

        binary = _find_claude_bin()
        if not binary:
            return False

        try:
            pid, fd = pty.fork()
        except Exception:
            return False

        if pid == 0:
            # Child: set up environment so claude renders cleanly in a pty
            # with reasonable terminal capabilities, then exec.
            os.environ["TERM"] = "xterm-256color"
            os.environ["COLUMNS"] = "120"
            os.environ["LINES"] = "40"
            os.environ["CLAUDE_CODE_INTERNAL_REFRESH"] = "1"  # marker for our hook to log
            try:
                os.execv(binary, [binary, "--model", self._model])
            except Exception:
                os._exit(127)

        # Parent
        self._pid = pid
        self._master_fd = fd

        # Drain output in the background so the pty buffer doesn't fill up
        # and block the child.
        self._stop_drain.clear()
        self._drain_thread = threading.Thread(target=self._drain, daemon=True)
        self._drain_thread.start()

        # Give the TUI a moment to come up. We don't need to wait for the
        # full ready prompt — the first refresh tick can be slow.
        time.sleep(0.5)
        return True

    def _drain(self) -> None:
        """Read and discard output from the pty so the kernel buffer never fills."""
        fd = self._master_fd
        if fd is None:
            return
        while not self._stop_drain.is_set():
            try:
                r, _, _ = select.select([fd], [], [], 0.5)
                if fd in r:
                    try:
                        data = os.read(fd, 4096)
                        if not data:
                            break  # EOF
                    except OSError as e:
                        if e.errno in (errno.EIO,):
                            break  # child exited
                        if e.errno == errno.EAGAIN:
                            continue
                        break
            except (OSError, ValueError):
                break

    def send_prompt(self, prompt: str = "ok") -> bool:
        """Send a short prompt + Enter. The TUI handles it, statusline fires,
        rate-limits file updates within ~5 seconds."""
        with self._lock:
            if not self.is_alive():
                if not self.spawn():
                    return False
            try:
                # Send prompt + carriage-return (TUI input is line-buffered)
                payload = (prompt + "\r").encode("utf-8")
                os.write(self._master_fd, payload)
                return True
            except OSError:
                # pty died; try one respawn
                self._pid = None
                self._master_fd = None
                if self.spawn():
                    try:
                        os.write(self._master_fd, (prompt + "\r").encode("utf-8"))
                        return True
                    except OSError:
                        return False
                return False

    def shutdown(self) -> None:
        self._stop_drain.set()
        if self._pid is not None:
            try:
                os.kill(self._pid, signal.SIGTERM)
            except OSError:
                pass
            self._pid = None
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None


# Module-level singleton — one pty per running meter, shared across clicks.
_SESSION: Optional[PtySession] = None


def get_session() -> PtySession:
    global _SESSION
    if _SESSION is None:
        _SESSION = PtySession()
    return _SESSION


def refresh() -> bool:
    """Public entry point called by the refresh button.
    Sends a single 'ok' prompt to the persistent claude TUI."""
    return get_session().send_prompt("ok")


def shutdown() -> None:
    """Stop the pty (called on meter exit)."""
    global _SESSION
    if _SESSION is not None:
        _SESSION.shutdown()
        _SESSION = None
