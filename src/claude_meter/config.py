"""User-tunable thresholds and constants."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PlanLimits:
    """Approximate token ceilings per Claude plan tier.

    Calibrated against the in-app "% used" gauges shown by Claude Code itself.
    On 2026-05-14, with 11.9M billed tokens reported as 49% of the 5-hour
    window and 5% of the weekly window, the implied ceilings are:
      - 5-hour:  ~24.3M tokens
      - weekly:  ~238M tokens

    Edit these if your in-app gauges deviate. The point is "look at the bar,
    don't look at the absolute numbers" — calibration is loose by design.
    """

    name: str
    five_hour_ceiling: int
    weekly_ceiling: int


PLANS = {
    "pro": PlanLimits("Claude Pro", 1_200_000, 12_000_000),
    "max-5x": PlanLimits("Claude Max 5x", 6_000_000, 60_000_000),
    "max-20x": PlanLimits("Claude Max 20x", 24_300_000, 238_000_000),
    "console": PlanLimits("Console API", 999_999_999, 999_999_999),
}

# User's plan. Change if needed.
ACTIVE_PLAN = "max-20x"

# Visual thresholds as fraction of either ceiling.
WARN_THRESHOLD = 0.65
DANGER_THRESHOLD = 0.85

# How often the dashboard re-reads transcripts.
REFRESH_SECONDS = 5

# Window sizes.
FIVE_HOUR_WINDOW = 5.0  # hours
WEEKLY_WINDOW = 7 * 24.0  # hours


def active_limit() -> PlanLimits:
    return PLANS[ACTIVE_PLAN]
