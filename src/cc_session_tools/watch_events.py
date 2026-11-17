"""The events of ``cc-session-tools watch``: their kinds and groups, the ``--events`` filter, and durations.

Every event is one JSON object with ``kind``, ``seq``, ``observed_at`` and ``data``. A session event names the session
in ``data.session`` with a string that ``inspect`` resolves, and carries only what changed: a reader that needs
more runs ``cc-session-tools inspect <session> --json``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

WATCH_STARTED = "watch_started"
WATCH_ENDED = "watch_ended"
LIFECYCLE_KINDS: tuple[str, ...] = (WATCH_STARTED, WATCH_ENDED)

SESSION_STARTED = "session_started"
SESSION_ENDED = "session_ended"
SESSION_MATCHED = "session_matched"
SESSION_UNMATCHED = "session_unmatched"
STATUS_CHANGED = "status_changed"
TURN_FINISHED = "turn_finished"
WAITING = "waiting"
BUSY_LONG = "busy_long"
WAITING_LONG = "waiting_long"
IDLE_LONG = "idle_long"
RENAMED = "renamed"
CWD_CHANGED = "cwd_changed"
TMUX_MOVED = "tmux_moved"

GROUPS: Mapping[str, tuple[str, ...]] = {
    "membership": (SESSION_STARTED, SESSION_ENDED, SESSION_MATCHED, SESSION_UNMATCHED),
    "status": (STATUS_CHANGED,),
    "turns": (TURN_FINISHED, WAITING),
    "thresholds": (BUSY_LONG, WAITING_LONG, IDLE_LONG),
    "changes": (RENAMED, CWD_CHANGED, TMUX_MOVED),
    "lifecycle": LIFECYCLE_KINDS,
}
"""``turns`` holds the events derived from a status transition, so an agent can take them without ``status_changed``.
``lifecycle`` is always reported."""

SESSION_KINDS: tuple[str, ...] = tuple(kind for name, kinds in GROUPS.items() if name != "lifecycle" for kind in kinds)
ALL_KINDS: frozenset[str] = frozenset(SESSION_KINDS) | frozenset(LIFECYCLE_KINDS)

THRESHOLD_OPTIONS: Mapping[str, str] = {
    BUSY_LONG: "--busy-after",
    WAITING_LONG: "--waiting-after",
    IDLE_LONG: "--idle-after or --idle-every",
}
"""The kinds the watch reports only when their option is given."""

JOIN_LEAVE_KINDS: tuple[str, ...] = (SESSION_STARTED, SESSION_MATCHED, SESSION_UNMATCHED)
"""Kinds that a watch of named sessions never produces: its set only shrinks, as its sessions end."""


class FilterError(ValueError):
    pass


def split_values(values: Iterable[str] | None) -> list[str]:
    """Flatten repeated and comma-separated option values."""
    return [part.strip() for value in values or () for part in value.split(",") if part.strip()]


def produced_kinds(*, enabled_thresholds: Iterable[str] = (), fixed_set: bool = False) -> frozenset[str]:
    """The session event kinds a watch with these options can report."""
    kinds = set(SESSION_KINDS) - (set(THRESHOLD_OPTIONS) - set(enabled_thresholds))
    if fixed_set:
        kinds -= set(JOIN_LEAVE_KINDS)
    return frozenset(kinds)


def _expand(values: Iterable[str], option: str, produced: frozenset[str], why_not: Mapping[str, str]) -> set[str]:
    expanded: set[str] = set()
    for name in values:
        if name in GROUPS:
            if name == "lifecycle":
                expanded |= set(LIFECYCLE_KINDS)
                continue
            members = set(GROUPS[name]) & produced
            if not members:
                reason = why_not.get(name, "")
                raise FilterError(f"{option}: the group {name!r} has no events for this watch ({reason})")
            expanded |= members
        elif name in ALL_KINDS:
            if name not in produced and name not in LIFECYCLE_KINDS:
                raise FilterError(f"{option}: this watch never produces {name!r} ({why_not.get(name, '')})")
            expanded.add(name)
        else:
            raise FilterError(
                f"{option}: unknown event kind or group {name!r} (kinds: {', '.join(sorted(ALL_KINDS))}; "
                f"groups: {', '.join(GROUPS)})"
            )
    return expanded


def _why_not(fixed_set: bool) -> dict[str, str]:
    reasons = {kind: f"it needs {option}" for kind, option in THRESHOLD_OPTIONS.items()}
    reasons["thresholds"] = "it needs --busy-after, --waiting-after, --idle-after or --idle-every"
    if fixed_set:
        for kind in JOIN_LEAVE_KINDS:
            reasons[kind] = "a watch of named sessions has a fixed set"
    return reasons


def resolve_filter(
    include: Iterable[str] | None,
    exclude: Iterable[str] | None,
    *,
    enabled_thresholds: Iterable[str] = (),
    fixed_set: bool = False,
) -> frozenset[str]:
    """The kinds the watch reports. Lifecycle kinds are always in and cannot be excluded.

    A kind or group that is unknown, or that this watch never produces, is an error, so a typo never means "no events".
    A threshold option whose kind the filter removes is an error too, so an option never silently does nothing.

    Raises:
        FilterError: the message names the option and the offending value.
    """
    enabled = frozenset(enabled_thresholds)
    produced = produced_kinds(enabled_thresholds=enabled, fixed_set=fixed_set)
    why_not = _why_not(fixed_set)
    include_list = split_values(include)
    exclude_list = split_values(exclude)
    selected = _expand(include_list, "--events", produced, why_not) if include_list else set(produced)
    if exclude_list:
        excluded = _expand(exclude_list, "--exclude-events", produced, why_not)
        if excluded & set(LIFECYCLE_KINDS):
            raise FilterError("--exclude-events: lifecycle events are always reported and cannot be excluded")
        selected -= excluded
    for kind in sorted(enabled):
        if kind not in selected:
            raise FilterError(f"{THRESHOLD_OPTIONS[kind]}: {kind!r} is filtered out by --events/--exclude-events")
    return frozenset(selected | set(LIFECYCLE_KINDS))


def utc_iso(epoch_seconds: float) -> str:
    """``2026-11-16T17:05:03.120Z``."""
    return datetime.fromtimestamp(epoch_seconds, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass(frozen=True, slots=True)
class WatchEvent:
    """One reported event. ``seq`` is ``0`` until the watch numbers the events it reports."""

    kind: str
    observed_at: str
    data: dict[str, Any] = field(default_factory=dict)
    seq: int = 0

    def to_json(self) -> dict[str, Any]:
        return {"kind": self.kind, "seq": self.seq, "observed_at": self.observed_at, "data": self.data}


_DURATION_RE = re.compile(r"^(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?$")


def parse_duration(value: str) -> float:
    """``90s``, ``30m``, ``2h``, ``1h30m``, or a plain number of seconds.

    Raises:
        ValueError: the text is none of these.
    """
    text = value.strip().lower()
    if text.replace(".", "", 1).isdigit():
        return float(text)
    match = _DURATION_RE.match(text)
    if not text or not match or not any(match.groupdict().values()):
        raise ValueError(f"cannot read the duration {value!r}; use for example 90s, 30m, 2h or 1h30m")
    parts = {k: int(v) if v else 0 for k, v in match.groupdict().items()}
    return float(parts["h"] * 3600 + parts["m"] * 60 + parts["s"])


def format_duration(seconds: float) -> str:
    """``90`` -> ``1m30s``, ``7200`` -> ``2h``: the spelling the duration options accept."""
    total = int(round(seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    text = (f"{hours}h" if hours else "") + (f"{minutes}m" if minutes else "") + (f"{secs}s" if secs else "")
    return text or "0s"
