"""One session, running or ended: finding it by any name it had, and the fields ``inspect`` prints.

A running session comes from the registry, through the same lookup as ``list-active`` filters use. An ended session
exists only as its transcript, so the lookup falls back to the transcripts of every profile, in this order:

1. a session id, or a prefix of one: file names only, across every transcript, so it costs a directory listing;
2. a name, or any title the transcript recorded, so a renamed session is found under an old name too: the transcripts
   active within the window are listed through cc-blackbox, ripgrep (or cc-blackbox's scan) keeps those that contain
   the name, and only those are read for their title records;
3. a pid, from the claude-code-auth-switch start logs next to the transcripts active within the window.

``<profile>/<rest>`` restricts each step to sessions that profile ran last. A step that finds several sessions is an
error that lists them; a step that finds none hands over to the next.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cc_blackbox import (
    ActiveSession,
    ClaudeProfile,
    ProfileStart,
    SessionTranscript,
    active_session_for,
    is_session_file_name,
    list_active_sessions,
    list_profiles,
    list_projects_dirs,
    list_session_transcripts,
    read_session_transcript,
)

from .active import (
    PaneState,
    SessionLookupError,
    build_views,
    capture_pane,
    find_transcript_path,
    list_tmux_panes,
    lookup_session,
    pane_state,
    role_of,
    runs_in_pane,
    to_json,
    transcript_json,
)
from .plan import plan_json, read_plan_file
from .screen import classify, plain_lines
from .search import search_session_ids

DEFAULT_DAYS = 14
MAX_LISTED = 20
_ID_PREFIX = re.compile(r"^[0-9a-f][0-9a-f-]{3,}$")
_TITLE_RECORDS = {"custom-title": "customTitle", "agent-name": "agentName", "ai-title": "aiTitle"}


def name_key(name: str) -> str:
    """A name the way Claude Code compares names, near enough: case and runs of whitespace do not count."""
    return "-".join(name.casefold().split())


@dataclass(frozen=True)
class Found:
    """The session a query names: ``live`` while it runs, and its transcript when it has one."""

    live: ActiveSession | None
    transcript: SessionTranscript | None


def _fail(query: str, transcripts: Sequence[SessionTranscript], what: str) -> SessionLookupError:
    lines = [f"Error: {len(transcripts)} ended sessions match {query!r} by {what}:"]
    for t in transcripts[:MAX_LISTED]:
        when = t.last_record_at.isoformat(timespec="minutes") if t.last_record_at else "-"
        lines.append(f"  {t.session_id}  {when}  {t.title or '-'}")
    if len(transcripts) > MAX_LISTED:
        lines.append(f"  … and {len(transcripts) - MAX_LISTED} more")
    return SessionLookupError("\n".join(lines), ambiguous=True)


class _Ended:
    """The lookup among transcripts. Each listing is made once, and only when a step needs it."""

    def __init__(self, profile: ClaudeProfile | None, since: datetime) -> None:
        self.profile = profile
        self.since = since
        self._window: list[SessionTranscript] | None = None

    def window(self) -> list[SessionTranscript]:
        if self._window is None:
            self._window = list_session_transcripts(active_since=self.since)
        return self._window

    def _owned(self, transcripts: list[SessionTranscript]) -> list[SessionTranscript]:
        if self.profile is None:
            return transcripts
        kept = []
        for t in transcripts:
            owner = t.owner()
            if owner is not None and owner[0].config_dir == self.profile.config_dir:
                kept.append(t)
        return kept

    def by_id(self, query: str) -> list[SessionTranscript]:
        if not _ID_PREFIX.match(query):
            return []
        exact: list[SessionTranscript] = []
        prefixed: list[SessionTranscript] = []
        for projects_dir in list_projects_dirs():
            try:
                projects = [p for p in projects_dir.path.iterdir() if p.is_dir()]
            except OSError:
                continue
            for project in projects:
                try:
                    names = [n for n in os.listdir(project) if n.startswith(query) and is_session_file_name(n)]
                except OSError:
                    continue
                for file_name in names:
                    try:
                        transcript = read_session_transcript(project / file_name, projects_dir)
                    except OSError:
                        continue
                    (exact if transcript.session_id == query else prefixed).append(transcript)
        return self._owned(exact) or self._owned(prefixed)

    def by_name(self, query: str) -> list[SessionTranscript]:
        key = name_key(query)
        window = self.window()
        hits = search_session_ids([query], window)
        matched = [t for t in window if t.session_id in hits and key in {name_key(n) for n in recorded_titles(t)}]
        return self._owned(matched)

    def by_pid(self, pid: int) -> list[SessionTranscript]:
        matched = []
        for t in self.window():
            for start in t.profile_starts():
                if start.pid != pid:
                    continue
                if self.profile is not None and start.find_profile([self.profile]) is None:
                    continue
                matched.append(t)
                break
        return matched


def recorded_titles(transcript: SessionTranscript) -> list[str]:
    """Every name and title the transcript recorded, oldest first, and its current title.

    Claude Code appends a record whenever the session is named or renamed, or gets a generated title. Only the lines
    that hold such a record are parsed.
    """
    titles: list[str] = []
    markers = tuple(f'"{kind}"'.encode() for kind in _TITLE_RECORDS)
    try:
        with open(transcript.path, "rb") as fh:
            for line in fh:
                if not any(m in line for m in markers):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                key = _TITLE_RECORDS.get(record.get("type", "")) if isinstance(record, dict) else None
                value = record.get(key) if key else None
                if isinstance(value, str) and value.strip():
                    titles.append(value.strip())
    except OSError:
        pass
    if transcript.title:
        titles.append(transcript.title)
    return titles


def _split_profile(query: str) -> tuple[ClaudeProfile | None, str]:
    head, sep, rest = query.partition("/")
    if sep and rest:
        for profile in list_profiles():
            if profile.name == head:
                return profile, rest
    return None, query


def find(query: str, *, days: int = DEFAULT_DAYS, live: Sequence[ActiveSession] | None = None) -> Found:
    """The session ``query`` names, running or ended. See the module docstring for the order.

    Raises:
        SessionLookupError: nothing matches, or a step matches several sessions.
    """
    sessions = list(live) if live is not None else list_active_sessions()
    try:
        session = lookup_session(query, sessions)
        return Found(session, None)
    except SessionLookupError as exc:
        if exc.ambiguous:
            raise

    profile, rest = _split_profile(query)
    ended = _Ended(profile, datetime.now(UTC) - timedelta(days=days))
    pid_text = rest[4:] if rest.startswith("pid:") else rest
    steps: list[tuple[str, Any]] = []
    if not rest.startswith("pid:"):
        steps.append(("session id", lambda: ended.by_id(rest)))
        if not rest.isdecimal():
            steps.append(("name", lambda: ended.by_name(rest)))
    if pid_text.isdecimal():
        steps.append(("pid", lambda: ended.by_pid(int(pid_text))))
    for what, step in steps:
        matched = {t.session_id: t for t in step()}
        if len(matched) > 1:
            raise _fail(query, list(matched.values()), what)
        if matched:
            transcript = next(iter(matched.values()))
            return Found(active_session_for(transcript, sessions=sessions), transcript)
    raise SessionLookupError(
        f"Error: no session matches {query!r}: no running session, no transcript with that session id, and no "
        f"transcript active in the last {days} days with that name, title or auth-switch pid."
    )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _path(value: Path | None) -> str | None:
    return str(value) if value is not None else None


def _starts(starts: Sequence[ProfileStart]) -> list[dict[str, Any]]:
    return [
        {
            "ts": _iso(s.started_at),
            "pid": s.pid,
            "source": s.source,
            "profile": s.profile,
            "role": s.role,
            "role_prompt": _path(s.role_prompt),
            "role_prompt_mode": s.role_prompt_mode,
        }
        for s in sorted(starts, key=lambda s: s.started_at_ms)
    ]


def resume_command(profile: ClaudeProfile | None, session_id: str | None) -> str | None:
    """``claude-<profile> --resume <id>`` for an auth-switch profile, else ``claude --resume <id>``."""
    if session_id is None:
        return None
    command = f"claude-{profile.auth_switch_name}" if profile is not None and profile.auth_switch_name else "claude"
    return f"{command} --resume {session_id}"


LIVE_ONLY_KEYS = (
    "pid",
    "name_source",
    "status",
    "waiting_for",
    "kind",
    "updated_at",
    "git_worktree_root",
    "git_repo_root",
    "tmux_pane",
    "tmux_state",
    "tmux_live",
    "tmux_recorded",
    "messaging_socket",
    "messaging_address",
    "can_receive_messages",
    "peer_protocol",
    "peer_features",
    "bridge_session_id",
    "scratchpad_dir",
    "scratchpad_dir_inferred",
    "record_path",
    "raw",
)
"""Fields of ``list-active --json`` that only a running session has; ``null`` for an ended one."""


INSPECT_KEYS = (
    "state",
    "profile",
    "profile_config_dir",
    "profile_source",
    "address",
    "pid",
    "session_id",
    "name",
    "name_source",
    "role",
    "title",
    "title_source",
    "status",
    "waiting_for",
    "kind",
    "entrypoint",
    "version",
    "started_at",
    "updated_at",
    "last_activity_at",
    "last_record_at",
    "cwd",
    "launch_cwd",
    "git_branch",
    "git_worktree_root",
    "git_repo_root",
    "tmux_pane",
    "tmux_state",
    "tmux_live",
    "tmux_recorded",
    "messaging_socket",
    "messaging_address",
    "can_receive_messages",
    "peer_protocol",
    "peer_features",
    "bridge_session_id",
    "scratchpad_dir",
    "scratchpad_dir_inferred",
    "transcript_path",
    "cost",
    "context_window",
    "record_path",
    "alive",
    "profile_starts",
    "resume_command",
    "plan",
    "raw",
)
"""The keys of ``inspect --json``, a stable contract: every key of ``list-active --json``, plus ``state``, the title,
the auth-switch starts, the resume command and the plan file."""


SCREEN_LINES = 20
"""How many lines at the bottom of the pane ``inspect --screen`` prints, after runs of empty lines collapse to one."""


def _collapse_blank(lines: list[str]) -> list[str]:
    """The lines with every run of empty lines reduced to one: a tall pane is mostly empty above the prompt box."""
    return [line for index, line in enumerate(lines) if line or index == 0 or lines[index - 1]]


def _check_pane(pane: str, pid: int) -> tuple[str | None, PaneState | None]:
    """Why the pane cannot be captured for the session ``pid`` now, or ``None`` and the pane's state when it can."""
    state = pane_state(pane)
    if state is None:
        panes = list_tmux_panes()
        if panes is None:
            return "tmux_unavailable", None
        return ("pane_gone" if pane not in panes else "capture_failed"), None
    if not runs_in_pane(state.pid, pid):
        return "pane_not_owned", None
    if state.in_mode:
        return "pane_in_mode", None
    return None, state


def screen_json(data: dict[str, Any]) -> dict[str, Any]:
    """The ``screen`` object of ``inspect --screen``: what the session's tmux pane shows now, classified.

    ``reason`` says why nothing was captured, with ``state`` ``unknown`` and ``lines`` ``null``: ``ended``,
    ``not_in_tmux``, ``tmux_unavailable``, ``pane_gone``, ``pane_not_owned`` (the pane id now belongs to another
    process, as after a tmux server restart), ``pane_in_mode`` (copy mode or another tmux mode would take the keys) or
    ``capture_failed``. When the pane was captured, ``reason`` is ``null`` and an unrecognised screen is ``unknown``
    too. ``options`` lists the choices of a ``dialog``, ``permission_prompt`` or ``plan_approval``, and is ``null`` for
    every other state.
    """
    pane = data["tmux_pane"]
    result: dict[str, Any] = {
        "state": "unknown",
        "title": None,
        "options": None,
        "reason": None,
        "pane": pane,
        "captured_at": None,
        "lines": None,
    }
    if data["state"] != "running":
        result["reason"] = "ended"
    elif pane is None:
        result["reason"] = "not_in_tmux"
    elif data["tmux_state"] != "present":
        result["reason"] = "pane_gone" if data["tmux_state"] == "gone" else "tmux_unavailable"
    elif (checked := _check_pane(pane, data["pid"]))[0] is not None:
        result["reason"] = checked[0]
    else:
        captured = capture_pane(pane)
        # A pane can enter copy mode, or close, while it is captured; then the capture does not count.
        reason, after = _check_pane(pane, data["pid"])
        if captured is None or after is None:
            result["reason"] = reason or "capture_failed"
        else:
            # A cursor that moved during the capture may not belong to the captured screen.
            before = checked[1]
            cursor = after.cursor if before is not None and before.cursor == after.cursor else None
            classification = classify(captured, cursor)
            result["state"] = classification.state
            result["title"] = classification.title
            if classification.options is not None:
                result["options"] = [
                    {"key": o.key, "label": o.label, "selected": o.selected} for o in classification.options
                ]
            result["captured_at"] = _iso(datetime.now(UTC))
            result["lines"] = _collapse_blank(plain_lines(captured))[-SCREEN_LINES:]
    return result


def to_inspect_json(found: Found, *, screen: bool = False) -> dict[str, Any]:
    """The object ``inspect --json`` prints: ``state``, every key of ``list-active --json``, then what the transcript
    adds. The keys are the same for a running and an ended session. ``screen`` adds the ``screen`` key last."""
    transcript = found.transcript
    if found.live is not None:
        view = build_views([found.live])[0]
        data: dict[str, Any] = {"state": "running", **to_json(view)}
        if transcript is None:
            transcript = found.live.transcript
        profile: ClaudeProfile | None = found.live.profile
        session_id = found.live.session_id
    else:
        assert transcript is not None
        owner = transcript.owner()
        profile = owner[0] if owner is not None else None
        name = transcript.name
        session_id = transcript.session_id
        data = {
            "state": "ended",
            "profile": profile.name if profile is not None else None,
            "profile_config_dir": str(profile.config_dir) if profile is not None else None,
            "profile_source": owner[1] if owner is not None else None,
            "address": f"{profile.name}/{name}" if profile is not None and name else None,
            "session_id": session_id,
            "name": name,
            "role": role_of(transcript),
            "entrypoint": transcript.entrypoint,
            "version": transcript.version,
            "started_at": _iso(transcript.started_at),
            "cwd": _path(transcript.last_cwd or transcript.cwd),
            "launch_cwd": str(transcript.cwd) if transcript.cwd else None,
            "git_branch": transcript.last_git_branch or transcript.git_branch,
            "transcript_path": str(transcript.path),
            "alive": False,
            **transcript_json(transcript),
        }
        data.update(dict.fromkeys(LIVE_ONLY_KEYS))
    data["title"] = transcript.title if transcript is not None else None
    data["title_source"] = transcript.title_source if transcript is not None else None
    data["profile_starts"] = _starts(transcript.profile_starts()) if transcript is not None else []
    data["resume_command"] = resume_command(profile, session_id)
    path = find_transcript_path(found.live) if found.live is not None else None
    if path is None and transcript is not None:
        path = transcript.path
    data["plan"] = plan_json(read_plan_file(path)) if path is not None else None
    result = {key: data.get(key) for key in INSPECT_KEYS}
    if screen:
        result["screen"] = screen_json(result)
    return result
