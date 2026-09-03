"""Relative "time ago" formatting for the last-interaction column.

Ported from the replay UI's ``relativeTime`` (see
``claude-code-session-replay/src/client/Home.tsx``): just now / ``Nm`` / ``Nh`` /
yesterday / ``Nd`` / a short absolute date. The bucket thresholds and the
round-then-compare structure mirror the source; ``now`` is injectable so the
buckets are deterministic under test.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime


def _round_half_up(value: float) -> int:
    """Round half away from zero for positive input, matching JS ``Math.round``.

    Python's built-in ``round`` uses banker's rounding (``round(2.5) == 2``); the
    ported source relies on ``Math.round`` semantics, so the buckets line up at
    the half boundaries only with half-up rounding.
    """
    return math.floor(value + 0.5)


def time_ago(when: datetime, *, now: datetime | None = None) -> str:
    """Format ``when`` as a short relative label against ``now`` (default: now, UTC).

    Both sides are treated as aware UTC. ``now`` is only read from the clock when
    not supplied, keeping callers (and tests) able to pin the reference instant.
    """
    now = now if now is not None else datetime.now(UTC)

    secs = _round_half_up((now - when).total_seconds())
    if secs < 60:
        return "just now"
    mins = _round_half_up(secs / 60)
    if mins < 60:
        return f"{mins}m ago"
    hours = _round_half_up(mins / 60)
    if hours < 24:
        return f"{hours}h ago"
    days = _round_half_up(hours / 24)
    if days == 1:
        return "yesterday"
    if days < 7:
        return f"{days}d ago"
    # Older than a week: a short absolute date like "Jul 24". The day is rendered
    # as a bare int to sidestep the non-portable ``%-d`` strftime directive.
    return f"{when.strftime('%b')} {when.day}"
