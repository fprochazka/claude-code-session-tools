"""The ``cc-session-tools watch`` command: report changes to the running Claude Code sessions as JSON events.

The selection is the one of ``list-active``. Without SESSION arguments the filters are evaluated again on every change,
so sessions join and leave the watch; with them, the watch follows those sessions until they end. The polling and the
diffing live in ``watcher``, the event kinds in ``watch_events``. cc-blackbox delivers the events: one JSON line each on
stdout, or batched into messages to the Claude Code session the watch runs under.

The startup header, the end line and errors go to stderr, each prefixed ``cc-session-tools watch: ``, so stdout carries
only events, and in merged output every line that starts with ``{`` is an event.
"""

from __future__ import annotations

import enum
import json
import os
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, NoReturn, cast

import typer
from cc_blackbox import (
    ActiveSession,
    EventEnvelope,
    EventSink,
    EventStreamMode,
    MessageEventSink,
    MessagingUnavailable,
    Priority,
    ProfileNotFound,
    StdoutEventSink,
    choose_event_stream,
    current_session_or_none,
)

from .active import Filters, SessionLookupError, TmuxSessionNotFound, lookup_session, resolve_profile_globs
from .discovery import resolve_scope
from .list_active_cmd import TMUX_SESSION_HELP, Status, tmux_session_filter
from .watch_events import (
    GROUPS,
    LIFECYCLE_KINDS,
    WATCH_ENDED,
    WATCH_STARTED,
    FilterError,
    WatchEvent,
    parse_duration,
    produced_kinds,
    resolve_filter,
)
from .watcher import Ends, Selection, SessionTracker, Thresholds, Watcher

STATUS_PREFIX = "cc-session-tools watch: "
ENVELOPE_TAG = "session-watcher"
DETAILS = "cc-session-tools inspect <session> --json"
"""How to get every field of a session that an event names; the events carry only its identity and what changed."""
EXIT_ERROR = 1
EXIT_USAGE = 2


class StreamMode(enum.StrEnum):
    auto = "auto"
    stdout = "stdout"
    claude_message = "claude-message"


class MessagePriority(enum.StrEnum):
    now = "now"
    next = "next"
    later = "later"


def status_line(text: str) -> None:
    sys.stderr.write(f"{STATUS_PREFIX}{text}\n")
    sys.stderr.flush()


def _fail(message: str, code: int = EXIT_USAGE) -> NoReturn:
    status_line(f"error: {message}")
    raise typer.Exit(code)


def _duration(option: str, value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = parse_duration(value)
    except ValueError as exc:
        _fail(f"{option}: {exc}")
    if seconds <= 0:
        _fail(f"{option}: the duration must be longer than zero")
    return seconds


class Changelog:
    """``events.jsonl``: every reported event, one JSON line each, also when nobody reads the stream."""

    def __init__(self, path: Path) -> None:
        self.path = path

    @staticmethod
    def default_path(session: ActiveSession | None) -> Path:
        """Under the scratchpad of the session the watch runs under, else under the temp dir."""
        stamp = time.strftime("%Y%m%dT%H%M%S")
        if session is not None and session.scratchpad_dir is not None:
            base = session.scratchpad_dir / "cc-session-tools"
        else:
            base = Path(tempfile.gettempdir()) / f"cc-session-tools-{os.getuid()}"
        return base / f"watch-{stamp}-{os.getpid()}.events.jsonl"

    def append(self, events: Sequence[WatchEvent]) -> None:
        lines = [json.dumps(e.to_json(), ensure_ascii=False, separators=(",", ":")) + "\n" for e in events]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.writelines(lines)
        except OSError as exc:
            status_line(f"cannot write the changelog {self.path}: {exc}")


def _message_payload(event: WatchEvent) -> Mapping[str, Any]:
    """An event as a message line. The time is of no use to the receiver, which sees the message arrive."""
    return {"kind": event.kind, "seq": event.seq, "data": event.data}


def _sink(
    mode: StreamMode,
    own: ActiveSession | None,
    selection: str,
    changelog: Changelog,
    priority: MessagePriority,
) -> tuple[EventSink[WatchEvent], str | None]:
    try:
        choice = choose_event_stream(cast(EventStreamMode, mode.value), own)
    except MessagingUnavailable as exc:
        _fail(f"--events-stream=claude-message is not possible: {exc}; use --events-stream=stdout", EXIT_ERROR)
    if choice.mode == "stdout":
        return StdoutEventSink(), choice.fallback
    assert choice.session is not None
    envelope = EventEnvelope(
        ENVELOPE_TAG,
        {"filter": selection},
        payload=_message_payload,
        announce_on=WATCH_STARTED,
        announce_attributes={"changelog": str(changelog.path), "details": DETAILS},
        record=str(changelog.path),
    )
    sink: MessageEventSink[WatchEvent] = MessageEventSink(
        choice.session,
        envelope,
        sender=f"cc-session-tools-watch:{os.getpid()}",
        sender_name=f"cc-session-tools watch {selection}",
        status=status_line,
        priority=cast(Priority, priority.value),
        flush_on={WATCH_ENDED},
    )
    return sink, None


def _describe_selection(
    sessions: Sequence[str],
    profiles: Sequence[str],
    names: Sequence[str],
    statuses: Sequence[str],
    here: Path | None,
    tmux: bool | None,
    tmux_session: str | None = None,
) -> str:
    if sessions:
        return "sessions=" + ",".join(sessions)
    parts = [f"profile={','.join(profiles)}"] if profiles else []
    parts += [f"name={name}" for name in names]
    if statuses:
        parts.append(f"status={','.join(statuses)}")
    if here is not None:
        parts.append(f"here={here}")
    if tmux is not None:
        parts.append("tmux" if tmux else "no-tmux")
    if tmux_session is not None:
        parts.append(f"tmux-session={tmux_session}")
    return " ".join(parts) or "all"


def _resolve_fixed(queries: Sequence[str], tracker: SessionTracker, filters: Filters) -> frozenset[int]:
    candidates = [t.session for t in tracker.ordered() if filters.matches(t.session, t.tmux_live)]
    pids = set()
    for query in queries:
        try:
            pids.add(lookup_session(query, candidates).pid)
        except SessionLookupError as exc:
            sys.stderr.write(f"{exc}\n")
            raise typer.Exit(EXIT_ERROR) from exc
    return frozenset(pids)


def watch(
    sessions: list[str] = typer.Argument(
        None,
        help="Watch these sessions until they end, instead of every session that matches the filters: a session id, "
        "a unique session id prefix, a name, or a pid. The filters apply before the lookup.",
        show_default=False,
        metavar="[SESSION]...",
    ),
    profile: list[str] = typer.Option(
        None,
        "--profile",
        "-p",
        help="Only sessions of the profiles whose name matches this glob, for example 'rohlik*'. Repeatable.",
        show_default=False,
    ),
    status: list[Status] = typer.Option(None, "--status", help="Only sessions with this status. Repeatable."),
    here: bool = typer.Option(
        False,
        "--here",
        help="Only sessions whose cwd is in the current git repo or any of its worktrees; outside a repo, the exact "
        "current directory.",
    ),
    tmux: bool = typer.Option(
        None, "--tmux/--no-tmux", help="Only sessions that run in tmux, or only those that do not.", show_default=False
    ),
    name: str = typer.Option(
        None, "--name", help="Only sessions whose name matches this glob, case-insensitively.", show_default=False
    ),
    tmux_session: str = typer.Option(
        None,
        "--tmux-session",
        metavar="<name|current>",
        help=TMUX_SESSION_HELP + " It is resolved once, at the start.",
        show_default=False,
    ),
    include_self: bool = typer.Option(
        False,
        "--include-self",
        help="Also watch the session this command runs under. Left out by default, because every event it receives "
        "changes its own status.",
    ),
    events: list[str] = typer.Option(
        None,
        "--events",
        help="Report only these event kinds or groups (membership, status, turns, thresholds, changes). "
        "Comma-separated, repeatable.",
        show_default=False,
    ),
    exclude_events: list[str] = typer.Option(
        None,
        "--exclude-events",
        help="Do not report these event kinds or groups. Comma-separated, repeatable.",
        show_default=False,
    ),
    until: str = typer.Option(
        None,
        "--until",
        help="Stop after the first reported event of this kind, for example turn_finished.",
        show_default=False,
    ),
    timeout: str = typer.Option(
        None, "--timeout", help="Stop this long after the start, for example 90s, 30m or 1h30m.", show_default=False
    ),
    stop_after_quiet: str = typer.Option(
        None, "--stop-after-quiet", help="Stop when nothing was reported for this long.", show_default=False
    ),
    busy_after: str = typer.Option(
        None,
        "--busy-after",
        help="Report busy_long once per busy stretch of a session that stays busy this long.",
        show_default=False,
    ),
    waiting_after: str = typer.Option(
        None,
        "--waiting-after",
        help="Report waiting_long once per waiting stretch of a session that waits this long.",
        show_default=False,
    ),
    idle_after: list[str] = typer.Option(
        None,
        "--idle-after",
        help="Report idle_long once per idle stretch of a session that is idle this long after its last model "
        "response. A stretch ends at the next model response. Repeatable.",
        show_default=False,
    ),
    idle_every: str = typer.Option(
        None,
        "--idle-every",
        help="Report idle_long at every multiple of this duration within an idle stretch.",
        show_default=False,
    ),
    events_stream: StreamMode = typer.Option(
        StreamMode.auto,
        "--events-stream",
        help="Where events go: auto picks claude-message under a Claude Code session that accepts messages, else "
        "stdout.",
    ),
    message_priority: MessagePriority = typer.Option(
        MessagePriority.next, "--message-priority", help="The priority of claude-message messages."
    ),
) -> None:
    """Report changes to the running Claude Code sessions as JSON events.

    One JSON event per line on stdout, or as messages to the Claude Code session this runs under. The first event is
    watch_started with the matching sessions, the last is watch_ended with a reason. Without SESSION, a session that
    starts to match the filters joins the watch and one that stops matching leaves it.
    """
    thresholds = Thresholds(
        _duration("--busy-after", busy_after),
        _duration("--waiting-after", waiting_after),
        idle_after=tuple(sorted({cast(float, _duration("--idle-after", value)) for value in idle_after or []})),
        idle_every=_duration("--idle-every", idle_every),
    )
    ends = Ends(until, _duration("--timeout", timeout), _duration("--stop-after-quiet", stop_after_quiet))
    fixed = bool(sessions)
    try:
        reported = resolve_filter(events, exclude_events, enabled_thresholds=thresholds.enabled(), fixed_set=fixed)
    except FilterError as exc:
        _fail(str(exc))
    if until is not None and (until in LIFECYCLE_KINDS or until not in reported):
        _fail(f"--until: {until!r} is not an event kind this watch reports")

    try:
        profiles = resolve_profile_globs(profile) if profile else []
    except ProfileNotFound as exc:
        _fail(str(exc), EXIT_ERROR)
    try:
        tmux_session_name, _ = tmux_session_filter(tmux_session, tmux)
    except ValueError as exc:
        _fail(str(exc))
    except TmuxSessionNotFound as exc:
        _fail(str(exc), EXIT_ERROR)
    scope_dir = Path(os.getcwd()) if here else None
    filters = Filters(
        statuses=frozenset(s.value for s in status or []),
        profiles=frozenset(profiles),
        scope=resolve_scope(os.getcwd()) if here else None,
        in_tmux=tmux,
        name_glob=name,
        tmux_session=tmux_session_name,
    )
    own = current_session_or_none()
    exclude = None if include_self or own is None else own.pid
    tracker = SessionTracker(Selection(filters, None, exclude), thresholds)
    tracker.start(time.time())
    if fixed:
        pids = _resolve_fixed(sessions, tracker, filters)
        if exclude is not None and exclude in pids:
            _fail("SESSION names the session this command runs under; pass --include-self to watch it", EXIT_ERROR)
        tracker.select(Selection(filters, pids, exclude))

    selection = _describe_selection(
        sessions or [],
        profile or [],
        [name] if name else [],
        [s.value for s in status or []],
        scope_dir,
        tmux,
        tmux_session_name,
    )
    changelog = Changelog(Changelog.default_path(own))
    sink, fallback = _sink(events_stream, own, selection, changelog, message_priority)

    matched = tracker.matched()
    notes = [f"profiles: {', '.join(profiles)}"] if profile else []
    if tmux_session is not None and tmux_session != tmux_session_name:
        notes.append(f"tmux-session {tmux_session} resolved at the start")
    notes_text = f" ({'; '.join(notes)})" if notes else ""
    status_line(f"selection: {selection}{notes_text}; {len(matched)} matching now")
    if own is not None:
        own_name = own.name or f"pid:{own.pid}"
        if exclude is not None:
            status_line(f"own session: {own_name} (pid {own.pid}) is left out; --include-self watches it")
        else:
            status_line(f"own session: {own_name} (pid {own.pid}) is watched too")
    status_line(f"stream: {sink.describe()}")
    if fallback:
        status_line(f"stream fallback: {fallback}")
    status_line(f"changelog: {changelog.path}")
    status_line(f"details: {DETAILS}")
    session_kinds = [k for k in _ordered_kinds() if k in reported and k not in LIFECYCLE_KINDS]
    status_line(f"events: {','.join(session_kinds) or '-'} (always: {','.join(LIFECYCLE_KINDS)})")
    ignored = [k for k in _ordered_kinds() if k in produced_kinds(fixed_set=fixed) and k not in reported]
    if ignored:
        status_line(f"ignored: {','.join(ignored)}")
    if thresholds.describe():
        status_line(f"thresholds: {thresholds.describe()}")
    if ends.describe(fixed):
        status_line(f"ends: {ends.describe(fixed)}")

    Watcher(
        tracker,
        sink,
        reported,
        ends,
        status=status_line,
        record=changelog.append,
        start_data={"selection": selection},
    ).run()


def _ordered_kinds() -> list[str]:
    return [kind for kinds in GROUPS.values() for kind in kinds]
