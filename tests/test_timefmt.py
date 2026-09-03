from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from cc_session_tools.timefmt import time_ago

NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (timedelta(seconds=0), "just now"),
        (timedelta(seconds=59), "just now"),
        (timedelta(seconds=60), "1m ago"),
        (timedelta(minutes=5), "5m ago"),
        (timedelta(minutes=59), "59m ago"),
        (timedelta(minutes=60), "1h ago"),
        (timedelta(hours=5), "5h ago"),
        (timedelta(hours=23), "23h ago"),
        (timedelta(hours=24), "yesterday"),
        (timedelta(days=1), "yesterday"),
        (timedelta(days=2), "2d ago"),
        (timedelta(days=6), "6d ago"),
    ],
)
def test_relative_buckets(delta: timedelta, expected: str) -> None:
    assert time_ago(NOW - delta, now=NOW) == expected


def test_seven_days_falls_back_to_absolute_date() -> None:
    # 7 days before 2026-07-24 is 2026-07-17.
    assert time_ago(NOW - timedelta(days=7), now=NOW) == "Jul 17"


def test_old_date_uses_month_and_day() -> None:
    when = datetime(2026, 3, 4, 8, 30, tzinfo=UTC)
    assert time_ago(when, now=NOW) == "Mar 4"


def test_future_timestamp_is_just_now() -> None:
    assert time_ago(NOW + timedelta(minutes=5), now=NOW) == "just now"


def test_now_defaults_to_current_clock() -> None:
    # No injected ``now``: a fresh timestamp must land in the "just now" bucket.
    assert time_ago(datetime.now(UTC)) == "just now"
