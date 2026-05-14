"""Reads Claude Code session transcripts and aggregates token usage."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


PROJECTS_DIR = Path.home() / ".claude" / "projects"
RATE_LIMITS_FILE = Path.home() / ".claude" / "state" / "rate-limits.json"


def read_official_rate_limits() -> dict | None:
    """Return the rate-limits dict Claude Code itself reports, or None.

    Lives at ~/.claude/state/rate-limits.json (populated by the statusline
    hook). Shape:
        {
          "captured_at": "2026-05-14T07:34:39Z",
          "rate_limits": {
            "five_hour": {"used_percentage": 71.0, "resets_at": "..."},
            "seven_day": {"used_percentage": 8.5, "resets_at": "..."}
          },
          ...
        }

    Treated as authoritative — these are the numbers the in-app gauge shows.
    """
    if not RATE_LIMITS_FILE.exists():
        return None
    try:
        with RATE_LIMITS_FILE.open() as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None


@dataclass(frozen=True)
class TokenSample:
    """One assistant-response usage record from a transcript."""

    when: datetime
    input_tokens: int
    output_tokens: int
    cache_creation_input_tokens: int
    cache_read_input_tokens: int
    model: str
    session_id: str

    @property
    def total_billed_tokens(self) -> int:
        """Best approximation of what counts against your 5-hour quota.

        Cache-read tokens are cheap but they DO count toward rate limits at
        a discounted rate (10% on Anthropic's published rate-limit math).
        Cache-creation tokens count at full input rate.
        """
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + int(self.cache_read_input_tokens * 0.1)
        )

    @property
    def raw_total_tokens(self) -> int:
        """Sum of all tokens regardless of cache discount — for the 'big number'."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


def _parse_jsonl_line(line: str) -> TokenSample | None:
    try:
        record = json.loads(line)
    except json.JSONDecodeError:
        return None

    msg = record.get("message") or {}
    usage = msg.get("usage") or {}
    if not usage:
        return None

    ts_str = record.get("timestamp")
    if not ts_str:
        return None
    try:
        when = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except ValueError:
        return None

    return TokenSample(
        when=when,
        input_tokens=usage.get("input_tokens", 0) or 0,
        output_tokens=usage.get("output_tokens", 0) or 0,
        cache_creation_input_tokens=usage.get("cache_creation_input_tokens", 0) or 0,
        cache_read_input_tokens=usage.get("cache_read_input_tokens", 0) or 0,
        model=msg.get("model", "unknown"),
        session_id=record.get("sessionId", ""),
    )


def iter_recent_samples(since: datetime, root: Path = PROJECTS_DIR) -> Iterator[TokenSample]:
    """Yield every TokenSample with .when >= since across all transcripts.

    Walks every .jsonl under ~/.claude/projects/. Skips files whose mtime
    is older than `since` for speed.
    """
    if not root.exists():
        return

    since_ts = since.timestamp()
    for path in root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < since_ts:
                continue
        except OSError:
            continue
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    sample = _parse_jsonl_line(line)
                    if sample is not None and sample.when >= since:
                        yield sample
        except OSError:
            continue


@dataclass(frozen=True)
class WindowStats:
    """Aggregated stats for one rolling window."""

    total_tokens: int
    billed_tokens: int
    sample_count: int
    earliest: datetime | None
    latest: datetime | None
    tokens_per_minute: float

    @property
    def window_minutes(self) -> float:
        if not (self.earliest and self.latest):
            return 0.0
        return max((self.latest - self.earliest).total_seconds() / 60.0, 1.0)


def burn_rate_last_n_minutes(now: datetime, minutes: float = 30.0) -> float:
    """Average tokens-per-minute over the last N minutes.

    Uses BILLED tokens (matches what counts against the rate limit), not
    raw. Default window is 30 minutes — long enough to smooth out bursts
    so a single big message doesn't make 'time until cap' read like 1m.
    """
    since = now - timedelta(minutes=minutes)
    samples = list(iter_recent_samples(since))
    if not samples:
        return 0.0
    total = sum(s.total_billed_tokens for s in samples)
    return total / minutes


def stats_for_window(now: datetime, hours: float = 5.0) -> WindowStats:
    """Compute stats for the rolling window ending at `now`."""
    since = now - timedelta(hours=hours)
    samples = list(iter_recent_samples(since))

    if not samples:
        return WindowStats(0, 0, 0, None, None, 0.0)

    total = sum(s.raw_total_tokens for s in samples)
    billed = sum(s.total_billed_tokens for s in samples)
    earliest = min(s.when for s in samples)
    latest = max(s.when for s in samples)
    minutes = max((latest - earliest).total_seconds() / 60.0, 1.0)
    tpm = total / minutes

    return WindowStats(
        total_tokens=total,
        billed_tokens=billed,
        sample_count=len(samples),
        earliest=earliest,
        latest=latest,
        tokens_per_minute=tpm,
    )


def now_utc() -> datetime:
    return datetime.now(timezone.utc)
