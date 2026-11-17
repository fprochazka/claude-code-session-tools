"""The polling loop of ``cc-session-tools watch``: session registry records in, events out.

Claude Code rewrites ``<config dir>/sessions/<pid>.json`` on every status change, rename and ``/cd``, and deletes it on
a clean exit. The watcher stats every record of every profile about once a second, each shared ``sessions/`` dir once.
A record whose mtime and size did not move costs that stat and nothing else.

- A changed record of a session the watcher already knows is read again, and only the fields it changed are applied.
- A new record, or a changed one that the watcher does not know, makes it list the sessions through cc-blackbox, which
  decides liveness and the profile of a record in a shared dir. That is the expensive path, taken when a session
  starts.
- A known session whose record is gone, or whose process no longer runs, has ended.
- The tmux location of every pane comes from one ``tmux list-panes -a`` call every few seconds.

A status that lasts shorter than one poll can be missed: the watcher compares what it reads, not every write.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import signal
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType, MappingProxyType
from typing import Any

from cc_blackbox import ActiveSession, EventSink, list_active_sessions, list_profiles

from .active import (
    Filters,
    TmuxPane,
    TmuxState,
    list_tmux_panes,
    pane_id_of,
    read_last_response,
    read_role,
    waiting_for,
)
from .watch_events import (
    BUSY_LONG,
    CWD_CHANGED,
    IDLE_LONG,
    RENAMED,
    SESSION_ENDED,
    SESSION_MATCHED,
    SESSION_STARTED,
    SESSION_UNMATCHED,
    STATUS_CHANGED,
    THRESHOLD_OPTIONS,
    TMUX_MOVED,
    TURN_FINISHED,
    WAITING,
    WAITING_LONG,
    WATCH_ENDED,
    WATCH_STARTED,
    WatchEvent,
    format_duration,
    utc_iso,
)

POLL_INTERVAL = 1.0
TMUX_INTERVAL = 3.0
ROLE_SETTLE = 60.0
"""For this long after a session's start, a missing role is looked for again at every tmux call: the auth-switch
start hook logs it a moment after the session's record appears, next to a transcript the session may not have yet."""

TURN_ENDS_FROM = frozenset({"busy", "waiting"})
"""A move to ``idle`` from one of these statuses is ``turn_finished``."""

_RECORD_NAME = re.compile(r"^[1-9][0-9]*\.json$")

Stamp = tuple[int, int]
"""A record's mtime in nanoseconds and its size."""


def scan_records(dirs: Sequence[Path]) -> dict[Path, Stamp]:
    """The stamp of every ``<pid>.json`` record in ``dirs``. A missing dir, or a record that vanishes, is skipped."""
    stamps: dict[Path, Stamp] = {}
    for directory in dirs:
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            if not _RECORD_NAME.match(entry.name):
                continue
            try:
                stat = entry.stat()
            except OSError:
                continue
            stamps[Path(entry.path)] = (stat.st_mtime_ns, stat.st_size)
    return stamps


def sessions_dirs() -> list[Path]:
    """The ``sessions/`` dir of every profile, each real dir once."""
    seen: set[str] = set()
    dirs: list[Path] = []
    for profile in list_profiles():
        real = os.path.realpath(profile.sessions_dir)
        if real not in seen:
            seen.add(real)
            dirs.append(Path(real))
    return dirs


def process_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _str(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    return value if isinstance(value, str) and value else None


def _epoch(data: dict[str, Any], key: str) -> float | None:
    value = data.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return value / 1000


def refreshed(session: ActiveSession, data: dict[str, Any]) -> ActiveSession:
    """``session`` with the fields a running session rewrites taken from its new record.

    Everything cc-blackbox derived once, such as the profile and the launch dir, stays, except the transcript path
    when the session id changed: ``/clear`` and an in-process ``/resume`` give the running process another session
    id, and the old transcript belongs to the session it left. The readers look the new one up by session id.
    """
    cwd = _str(data, "cwd")
    socket = _str(data, "messagingSocketPath")
    updated = _epoch(data, "updatedAt")
    session_id = _str(data, "sessionId")
    if session_id != session.session_id:
        session = replace(session, transcript_path=None, _transcript_projects_dir=None)
    return replace(
        session,
        session_id=session_id,
        name=_str(data, "name"),
        name_source=_str(data, "nameSource"),
        cwd=Path(cwd) if cwd and cwd != "?" else None,
        status=_str(data, "status"),
        updated_at=datetime.fromtimestamp(updated, UTC) if updated is not None else None,
        tmux=_str(data, "tmux"),
        messaging_socket=Path(socket) if socket else None,
        raw=MappingProxyType(data),
    )


def _status_since(session: ActiveSession, fallback: float) -> float:
    """When the session's current status began: the record's ``statusUpdatedAt``, else ``updatedAt``, else now."""
    data = dict(session.raw)
    return _epoch(data, "statusUpdatedAt") or _epoch(data, "updatedAt") or fallback


def _location(live: TmuxPane | None) -> str | None:
    """``<tmux session>:<window index>``, or ``None`` when the pane is not there."""
    return f"{live.session}:{live.window_index}" if live is not None else None


def _name_key(name: str) -> str:
    """A name the way Claude Code compares names, near enough: case and runs of whitespace do not count."""
    return "-".join(name.casefold().split())


@dataclass
class Tracked:
    """What the watcher knows about one running session."""

    session: ActiveSession
    stamp: Stamp
    matched: bool
    status_since: float
    tmux_state: TmuxState | None = None
    tmux_live: TmuxPane | None = None
    threshold_sent: bool = False
    """Whether ``busy_long`` or ``waiting_long`` was reported for the current status stretch."""
    idle_since: float | None = None
    """The start of the current idle stretch: the newest of the last model response, the start of the session's
    process, and :attr:`session_switched`."""
    session_switched: float | None = None
    """When the watch saw the session id change, by ``/clear`` or an in-process ``/resume``."""
    idle_checked: bool = False
    """Whether :attr:`idle_since` was read from the transcript since the status last changed."""
    idle_reported: float = 0.0
    """The longest ``idle_long`` threshold reported in the current idle stretch, ``0`` for none."""
    role: str | None = None
    """The auth-switch role, read only when the selection filters by role."""
    role_settled: bool = True
    """``False`` while a missing role is still looked for at every tmux call."""

    @property
    def pid(self) -> int:
        return self.session.pid


@dataclass(frozen=True)
class Selection:
    """Which sessions the watch reports.

    Without ``pids`` the filters decide, again on every change, so sessions join and leave the watch. With ``pids``
    the set is fixed: those sessions, until they end.
    """

    filters: Filters = field(default_factory=Filters)
    pids: frozenset[int] | None = None
    exclude_pid: int | None = None
    """The watcher's own session, which never matches: its status changes with every event it receives."""

    def matches(self, session: ActiveSession, tmux_live: TmuxPane | None = None, role: str | None = None) -> bool:
        if self.pids is not None:
            return session.pid in self.pids
        if session.pid == self.exclude_pid:
            return False
        return self.filters.matches(session, tmux_live, role)


@dataclass(frozen=True)
class Thresholds:
    busy_after: float | None = None
    waiting_after: float | None = None
    idle_after: tuple[float, ...] = ()
    """``--idle-after``: each is reported once per idle stretch."""
    idle_every: float | None = None
    """``--idle-every``: every multiple is reported once per idle stretch."""

    def enabled(self) -> list[str]:
        kinds = []
        if self.busy_after is not None:
            kinds.append(BUSY_LONG)
        if self.waiting_after is not None:
            kinds.append(WAITING_LONG)
        if self.idles:
            kinds.append(IDLE_LONG)
        return kinds

    @property
    def idles(self) -> bool:
        return bool(self.idle_after) or self.idle_every is not None

    def idle_due(self, idle_for: float) -> float | None:
        """The longest idle threshold that ``idle_for`` seconds have reached, or ``None``."""
        reached = [after for after in self.idle_after if after <= idle_for]
        if self.idle_every is not None and idle_for >= self.idle_every:
            reached.append((idle_for // self.idle_every) * self.idle_every)
        return max(reached, default=None)

    def next_idle(self, reported: float) -> float | None:
        """The shortest idle threshold above ``reported``, or ``None`` when no threshold is left."""
        ahead = [after for after in self.idle_after if after > reported]
        if self.idle_every is not None:
            ahead.append((reported // self.idle_every + 1) * self.idle_every)
        return min(ahead, default=None)

    def for_status(self, status: str | None) -> tuple[str, float] | None:
        if status == "busy" and self.busy_after is not None:
            return BUSY_LONG, self.busy_after
        if status == "waiting" and self.waiting_after is not None:
            return WAITING_LONG, self.waiting_after
        return None

    def describe(self) -> str:
        parts = []
        if self.busy_after is not None:
            parts.append(f"{BUSY_LONG} after {format_duration(self.busy_after)}")
        if self.waiting_after is not None:
            parts.append(f"{WAITING_LONG} after {format_duration(self.waiting_after)}")
        idle = [format_duration(after) for after in self.idle_after]
        if self.idle_every is not None:
            idle.append(f"every {format_duration(self.idle_every)}")
        if idle:
            parts.append(f"{IDLE_LONG} after {', '.join(idle)} since the last model response")
        return ", ".join(parts)


ListSessions = Callable[[], list[ActiveSession]]
ListPanes = Callable[[], dict[str, TmuxPane] | None]
ReadLastResponse = Callable[[ActiveSession], datetime | None]
ReadRole = Callable[[ActiveSession], str | None]


class SessionTracker:
    """Keeps the state of every running session and turns each poll into events, before filtering by kind."""

    def __init__(
        self,
        selection: Selection,
        thresholds: Thresholds,
        *,
        dirs: Sequence[Path] | None = None,
        list_sessions: ListSessions = list_active_sessions,
        list_panes: ListPanes = list_tmux_panes,
        is_running: Callable[[int], bool] = process_running,
        last_response: ReadLastResponse = read_last_response,
        role_of_session: ReadRole = read_role,
        tmux_interval: float = TMUX_INTERVAL,
    ) -> None:
        self.selection = selection
        self.thresholds = thresholds
        self.dirs = list(dirs) if dirs is not None else sessions_dirs()
        self._list_sessions = list_sessions
        self._list_panes = list_panes
        self._is_running = is_running
        self._last_response = last_response
        self._read_role_of = role_of_session
        self.tmux_interval = tmux_interval
        self.tracked: dict[int, Tracked] = {}
        self._stamps: dict[Path, Stamp] = {}
        self._panes: dict[str, TmuxPane] | None = None
        self._last_tmux: float | None = None
        self._last_roles: float | None = None

    def select(self, selection: Selection) -> None:
        """Replace the selection, before the watch starts reporting."""
        self.selection = selection
        for tracked in self.tracked.values():
            if self._needs_roles and tracked.role is None:
                self._read_role(tracked, time.time())
            tracked.matched = self._match(tracked)

    @property
    def _needs_roles(self) -> bool:
        return bool(self.selection.filters.role_globs)

    def _match(self, tracked: Tracked) -> bool:
        return self.selection.matches(tracked.session, tracked.tmux_live, tracked.role)

    def _read_role(self, tracked: Tracked, now: float) -> None:
        tracked.role = self._read_role_of(tracked.session)
        started = tracked.session.started_at
        young = started is not None and now - started.timestamp() < ROLE_SETTLE
        tracked.role_settled = tracked.role is not None or not young

    # --- reading ---

    def start(self, now: float) -> None:
        """Read every session without reporting anything; ``watch_started`` describes what this finds."""
        self._stamps = scan_records(self.dirs)
        for session in self._list_sessions():
            self.tracked[session.pid] = self._new_tracked(session, now)
        self._refresh_tmux(now, report=False)

    def _new_tracked(self, session: ActiveSession, now: float) -> Tracked:
        stamp = self._stamps.get(session.record_path, (0, 0))
        tracked = Tracked(session, stamp, False, _status_since(session, now))
        self._place(tracked)
        if self._needs_roles:
            self._read_role(tracked, now)
        tracked.matched = self._match(tracked)
        return tracked

    def identify(self, tracked: Tracked) -> str:
        """The string that names the session in events, which ``inspect <it>`` resolves to exactly this session.

        ``<profile>/<name>``, the session's address. When the session has no name, or another running session of its
        profile has the same name, it is ``<profile>/pid:<pid>``, which is unique on the machine.
        """
        session = tracked.session
        if session.name is not None:
            key = _name_key(session.name)
            shared = any(
                other.pid != session.pid
                and other.session.profile.name == session.profile.name
                and other.session.name is not None
                and _name_key(other.session.name) == key
                for other in self.tracked.values()
            )
            if not shared:
                return f"{session.profile.name}/{session.name}"
        return f"{session.profile.name}/pid:{session.pid}"

    def summary(self, tracked: Tracked) -> dict[str, Any]:
        """A session in ``watch_started``: who it is and its status, with ``waiting_for`` when it waits."""
        data: dict[str, Any] = {"session": self.identify(tracked), "status": tracked.session.status}
        reason = waiting_for(tracked.session)
        if reason is not None:
            data["waiting_for"] = reason
        return data

    def matched(self) -> list[Tracked]:
        return [t for t in self.ordered() if t.matched]

    def ordered(self) -> list[Tracked]:
        return sorted(
            self.tracked.values(),
            key=lambda t: (t.session.profile.name, t.session.started_at or datetime.min.replace(tzinfo=UTC), t.pid),
        )

    def poll(self, now: float) -> list[WatchEvent]:
        """One poll: the events since the previous one, in the order they apply."""
        observed = utc_iso(now)
        events: list[WatchEvent] = []
        stamps = scan_records(self.dirs)
        by_path = {t.session.record_path: t for t in self.tracked.values()}
        reload = False

        for pid, tracked in list(self.tracked.items()):
            path = tracked.session.record_path
            if path not in stamps:
                events += self._end(pid, "record_removed", observed)
            elif not self._is_running(pid):
                events += self._end(pid, "process_gone", observed)

        for path, stamp in stamps.items():
            tracked = by_path.get(path)
            if tracked is None or tracked.pid not in self.tracked:
                if self._stamps.get(path) != stamp:
                    reload = True
                continue
            if tracked.stamp == stamp:
                continue
            data = self._read(path)
            if data is None:
                # A partial write: leave the old stamp, so the next poll reads it again.
                continue
            tracked.stamp = stamp
            events += self._apply(tracked, refreshed(tracked.session, data), observed, now)
        self._stamps = stamps

        if reload:
            events += self._reload(observed, now)
        events += self._refresh_tmux(now, report=True, observed=observed)
        events += self._refresh_roles(now, observed)
        events += self._thresholds(now, observed)
        return events

    @staticmethod
    def _read(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def _reload(self, observed: str, now: float) -> list[WatchEvent]:
        events: list[WatchEvent] = []
        sessions = {s.pid: s for s in self._list_sessions()}
        new: list[ActiveSession] = []
        for pid, session in sessions.items():
            tracked = self.tracked.get(pid)
            if tracked is None:
                new.append(session)
            else:
                tracked.stamp = self._stamps.get(session.record_path, tracked.stamp)
                events += self._apply(tracked, session, observed, now)
        for pid in [pid for pid in self.tracked if pid not in sessions]:
            # cc-blackbox judged the process not running, for example a pid that another process reuses.
            events += self._end(pid, "process_gone", observed)
        known_panes = self._panes or {}
        if any(pane not in known_panes for pane in (pane_id_of(s.tmux) for s in new) if pane is not None):
            # A new session usually runs in a new pane: look it up now, rather than call it gone until the next call.
            # The new sessions are placed after this, so their placement reports nothing.
            events += self._refresh_tmux(now, report=True, observed=observed, force=True)
        added = []
        for session in new:
            tracked = self._new_tracked(session, now)
            self.tracked[session.pid] = tracked
            added.append(tracked)
        if self.selection.pids is None:
            events += [self._event(SESSION_STARTED, t, observed, status=t.session.status) for t in added if t.matched]
        return events

    # --- diffing ---

    def _event(self, kind: str, tracked: Tracked, observed: str, **data: Any) -> WatchEvent:
        return WatchEvent(kind, observed, {"session": self.identify(tracked), **data})

    def _end(self, pid: int, reason: str, observed: str) -> list[WatchEvent]:
        tracked = self.tracked[pid]
        events = [self._event(SESSION_ENDED, tracked, observed, reason=reason)] if tracked.matched else []
        del self.tracked[pid]
        return events

    def _apply(self, tracked: Tracked, new: ActiveSession, observed: str, now: float) -> list[WatchEvent]:
        """Move ``tracked`` to ``new``. Changes are reported for a session that matched before them; a session that
        starts to match is reported once, as ``session_matched``, with its new state."""
        old = tracked.session
        old_identity = self.identify(tracked)
        was_matched = tracked.matched
        tracked.session = new
        if pane_id_of(new.tmux) != pane_id_of(old.tmux):
            self._place(tracked)
        events: list[WatchEvent] = []

        def changed(kind: str, **data: Any) -> None:
            if was_matched:
                events.append(self._event(kind, tracked, observed, **data))

        if new.name != old.name:
            changed(RENAMED, **{"from": old_identity})
        if new.cwd != old.cwd:
            changed(CWD_CHANGED, to=_text(new.cwd))
        if new.session_id != old.session_id:
            # The record's updatedAt is when Claude Code wrote the new id.
            tracked.session_switched = _epoch(dict(new.raw), "updatedAt") or now
            tracked.idle_checked = False
        if new.status != old.status:
            tracked.status_since = _status_since(new, now)
            tracked.threshold_sent = False
            tracked.idle_checked = False
            reason = waiting_for(new)
            change: dict[str, Any] = {"from": old.status, "to": new.status}
            if reason is not None:
                change["waiting_for"] = reason
            changed(STATUS_CHANGED, **change)
            if new.status == "idle" and old.status in TURN_ENDS_FROM:
                changed(TURN_FINISHED, **{"from": old.status})
            if new.status == "waiting":
                changed(WAITING, waiting_for=reason)

        if self._needs_roles and tracked.role is None:
            # A record change is cheap to wait for, and the transcript the role sits next to may have appeared.
            self._read_role(tracked, now)
        tracked.matched = self._match(tracked)
        if self.selection.pids is None:
            if tracked.matched and not was_matched:
                events.append(self._event(SESSION_MATCHED, tracked, observed, status=new.status))
            elif was_matched and not tracked.matched:
                events.append(self._event(SESSION_UNMATCHED, tracked, observed))
        return events

    def _place(self, tracked: Tracked) -> None:
        pane = pane_id_of(tracked.session.tmux)
        if pane is None:
            tracked.tmux_state, tracked.tmux_live = None, None
        elif self._panes is None:
            tracked.tmux_state, tracked.tmux_live = "unknown", None
        else:
            tracked.tmux_live = self._panes.get(pane)
            tracked.tmux_state = "present" if tracked.tmux_live is not None else "gone"

    def _refresh_tmux(self, now: float, *, report: bool, observed: str = "", force: bool = False) -> list[WatchEvent]:
        """Ask the tmux server where every pane is. A move is ``tmux_moved``; under ``--tmux-session`` it can also
        make a session join or leave the watch."""
        if not force and self._last_tmux is not None and now - self._last_tmux < self.tmux_interval:
            return []
        if not force and not any(pane_id_of(t.session.tmux) is not None for t in self.tracked.values()):
            return []
        self._last_tmux = now
        self._panes = self._list_panes()
        events: list[WatchEvent] = []
        for tracked in self.ordered():
            before_state, before = tracked.tmux_state, _location(tracked.tmux_live)
            was_matched = tracked.matched
            self._place(tracked)
            after = _location(tracked.tmux_live)
            # Only a move between two answers of the tmux server counts; an unreachable server says nothing.
            known = before_state in ("present", "gone") and tracked.tmux_state in ("present", "gone")
            if report and known and before != after and was_matched:
                events.append(self._event(TMUX_MOVED, tracked, observed, to=after))
            if tracked.tmux_state == "unknown" and before_state in ("present", "gone"):
                # The server did not answer this time; that says nothing about which tmux session the pane is in.
                continue
            tracked.matched = self._match(tracked)
            if report and self.selection.pids is None:
                if tracked.matched and not was_matched:
                    events.append(self._event(SESSION_MATCHED, tracked, observed, status=tracked.session.status))
                elif was_matched and not tracked.matched:
                    events.append(self._event(SESSION_UNMATCHED, tracked, observed))
        return events

    def _refresh_roles(self, now: float, observed: str) -> list[WatchEvent]:
        """Look again for the role of the young sessions that have none yet; a session whose role now matches joins."""
        if not self._needs_roles:
            return []
        if self._last_roles is not None and now - self._last_roles < self.tmux_interval:
            return []
        self._last_roles = now
        events: list[WatchEvent] = []
        for tracked in self.ordered():
            if tracked.role_settled:
                continue
            self._read_role(tracked, now)
            was_matched = tracked.matched
            tracked.matched = self._match(tracked)
            if self.selection.pids is None and tracked.matched and not was_matched:
                events.append(self._event(SESSION_MATCHED, tracked, observed, status=tracked.session.status))
        return events

    def _read_idle_since(self, tracked: Tracked) -> None:
        """Read the start of the idle stretch from the transcript. A newer model response starts a new stretch.

        The stretch starts no earlier than the session's process, nor than a change of its session id: a resumed
        session's transcript holds the responses of its earlier runs, which say nothing about this one.
        """
        response = self._last_response(tracked.session)
        started = tracked.session.started_at
        times = [
            t
            for t in (
                response.timestamp() if response is not None else None,
                started.timestamp() if started is not None else None,
                tracked.session_switched,
            )
            if t is not None
        ]
        since = max(times, default=tracked.status_since)
        if since != tracked.idle_since:
            tracked.idle_since = since
            tracked.idle_reported = 0.0
        tracked.idle_checked = True

    def _idle_long(self, tracked: Tracked, now: float, observed: str) -> WatchEvent | None:
        """``idle_long`` for the longest threshold the idle stretch has reached and not yet reported.

        The transcript is read when the session becomes idle, and again before a threshold is reported, because a
        response does not always show in the registry: a background subagent answers while its session stays idle, and
        a turn shorter than one poll is missed.
        """
        fresh = not tracked.idle_checked
        if fresh:
            self._read_idle_since(tracked)
        assert tracked.idle_since is not None
        due = self.thresholds.idle_due(now - tracked.idle_since)
        if due is None or due <= tracked.idle_reported:
            return None
        if not fresh:
            self._read_idle_since(tracked)
            due = self.thresholds.idle_due(now - tracked.idle_since)
            if due is None or due <= tracked.idle_reported:
                return None
        tracked.idle_reported = due
        return self._event(IDLE_LONG, tracked, observed, threshold=format_duration(due))

    def _thresholds(self, now: float, observed: str) -> list[WatchEvent]:
        events = []
        for tracked in self.ordered():
            if self.thresholds.idles and tracked.matched and tracked.session.status == "idle":
                event = self._idle_long(tracked, now, observed)
                if event is not None:
                    events.append(event)
            due = self.thresholds.for_status(tracked.session.status)
            if due is None or tracked.threshold_sent or not tracked.matched:
                continue
            kind, after = due
            if now - tracked.status_since >= after:
                tracked.threshold_sent = True
                events.append(self._event(kind, tracked, observed, after=format_duration(after)))
        return events

    def seconds_until_threshold(self, now: float) -> float | None:
        waits = []
        for tracked in self.tracked.values():
            due = self.thresholds.for_status(tracked.session.status)
            if due is not None and tracked.matched and not tracked.threshold_sent:
                waits.append(max(0.0, tracked.status_since + due[1] - now))
            if self.thresholds.idles and tracked.matched and tracked.session.status == "idle":
                if not tracked.idle_checked or tracked.idle_since is None:
                    waits.append(0.0)
                    continue
                ahead = self.thresholds.next_idle(tracked.idle_reported)
                if ahead is not None:
                    waits.append(max(0.0, tracked.idle_since + ahead - now))
        return min(waits) if waits else None


def _text(path: Path | None) -> str | None:
    return str(path) if path is not None else None


@dataclass(frozen=True)
class Ends:
    until: str | None = None
    timeout: float | None = None
    stop_after_quiet: float | None = None

    def describe(self, fixed_set: bool) -> str:
        parts = []
        if self.until is not None:
            parts.append(f"until the first {self.until}")
        if self.timeout is not None:
            parts.append(f"timeout {format_duration(self.timeout)}")
        if self.stop_after_quiet is not None:
            parts.append(f"after {format_duration(self.stop_after_quiet)} quiet")
        if fixed_set:
            parts.append("when every named session has ended")
        return ", ".join(parts)


class Watcher:
    """Runs the tracker until an end, numbering and delivering the events that pass the filter.

    ``clock`` gives epoch seconds and ``wait`` sleeps, so tests can run a watch on a fake clock.
    """

    def __init__(
        self,
        tracker: SessionTracker,
        sink: EventSink[WatchEvent],
        reported: frozenset[str],
        ends: Ends,
        *,
        status: Callable[[str], None],
        record: Callable[[Sequence[WatchEvent]], None] | None = None,
        start_data: dict[str, Any] | None = None,
        poll_interval: float = POLL_INTERVAL,
        clock: Callable[[], float] = time.time,
        wait: Callable[[float], None] | None = None,
    ) -> None:
        self.tracker = tracker
        self.sink = sink
        self.reported = reported
        self.ends = ends
        self.status = status
        self.record = record
        self.start_data = start_data or {}
        self.poll_interval = poll_interval
        self.clock = clock
        self._wakeup = threading.Event()
        self._wait = wait or self._wakeup.wait
        self._seq = 0
        self._stop_reason: str | None = None

    def request_stop(self, reason: str) -> None:
        self._stop_reason = reason
        self._wakeup.set()

    def _deliver(self, events: list[WatchEvent]) -> list[WatchEvent]:
        numbered = []
        for event in events:
            if event.kind in self.reported:
                self._seq += 1
                numbered.append(replace(event, seq=self._seq))
        if numbered:
            if self.record is not None:
                self.record(numbered)
            self.sink.emit(numbered)
        return numbered

    def _finish(self, reason: str, **data: Any) -> None:
        self._deliver([WatchEvent(WATCH_ENDED, utc_iso(self.clock()), {"reason": reason, **data})])
        self.status(f"ended: {reason}")

    @contextlib.contextmanager
    def _signals(self) -> Any:
        if threading.current_thread() is not threading.main_thread():
            yield
            return

        def handler(signum: int, frame: FrameType | None) -> None:
            self.request_stop(f"signal {signal.Signals(signum).name}")

        previous = {sig: signal.signal(sig, handler) for sig in (signal.SIGTERM, signal.SIGINT)}
        try:
            yield
        finally:
            for sig, old in previous.items():
                signal.signal(sig, old)

    def run(self) -> None:
        with self._signals():
            try:
                self._run()
            finally:
                self.sink.close()

    def _run(self) -> None:
        started = self.clock()
        deadline = started + self.ends.timeout if self.ends.timeout is not None else None
        sessions = [self.tracker.summary(t) for t in self.tracker.matched()]
        self._deliver([WatchEvent(WATCH_STARTED, utc_iso(started), {**self.start_data, "sessions": sessions})])
        last_activity = started
        fixed = self.tracker.selection.pids is not None
        if fixed and not self.tracker.matched():
            self._finish("sessions_ended")
            return
        while True:
            if self._stop_reason is not None:
                self._finish(self._stop_reason)
                return
            self.sink.tick()
            now = self.clock()
            delivered = self._deliver(self.tracker.poll(now))
            if any(e.kind not in THRESHOLD_OPTIONS for e in delivered):
                last_activity = now
            hit = next((e for e in delivered if e.kind == self.ends.until), None)
            if hit is not None:
                self._finish("until", event=hit.kind, seq=hit.seq)
                return
            if fixed and not self.tracker.matched():
                self._finish("sessions_ended")
                return
            if not self.sink.alive():
                self._finish("session_gone")
                return
            now = self.clock()
            if deadline is not None and now >= deadline:
                self._finish("timeout")
                return
            quiet = self.ends.stop_after_quiet
            if quiet is not None and now - last_activity >= quiet:
                self._finish("quiet", stop_after_quiet=format_duration(quiet))
                return
            wait = self.poll_interval
            if deadline is not None:
                wait = min(wait, deadline - now)
            if quiet is not None:
                wait = min(wait, last_activity + quiet - now)
            for due in (self.tracker.seconds_until_threshold(now), self.sink.seconds_until_flush()):
                if due is not None:
                    wait = min(wait, due)
            self._wait(max(0.0, wait))
