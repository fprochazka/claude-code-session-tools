"""Running Claude Code sessions, with their tmux pane and git branch resolved live.

cc-blackbox reads the session registry of every profile. Two things in a registry record cannot be taken at face value,
so this module looks them up again:

- ``tmux``: Claude Code writes ``<session>:@<window>.%<pane>`` once, at startup. Moving a window to another tmux
  session leaves the record stale. Only the pane id ``%N`` stays valid for the life of the pane, so the session and
  window are read from the running tmux server, with one ``tmux list-panes -a`` call for all sessions.
- the git branch: the record has none. It comes from ``git rev-parse`` in the session's current directory.
"""

from __future__ import annotations

import fnmatch
import os
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from cc_blackbox import (
    ActiveSession,
    AmbiguousSession,
    ClaudeProfile,
    ProfileNotFound,
    SessionNotFound,
    TranscriptNotFound,
    find_session,
    find_session_transcript,
    list_profiles,
)

from .discovery import Scope
from .gitrepo import GitLocation, git_location

TmuxState = Literal["present", "gone", "unknown"]
"""``present``: the pane exists. ``gone``: the tmux server runs and has no such pane. ``unknown``: tmux is not
installed, or ``tmux list-panes`` failed, for example because no server runs on the default socket."""

_FIELD_SEPARATOR = "\t"
_LIST_PANES_FORMAT = _FIELD_SEPARATOR.join(
    ("#{pane_id}", "#{session_name}", "#{window_index}", "#{window_id}", "#{pane_title}")
)


@dataclass(frozen=True)
class TmuxPane:
    """Where a pane is now, according to the tmux server."""

    pane_id: str
    session: str
    window_index: int | None
    window_id: str
    pane_title: str

    def to_json(self) -> dict[str, Any]:
        return {
            "session": self.session,
            "window_index": self.window_index,
            "window_id": self.window_id,
            "pane_title": self.pane_title,
        }


def pane_id_of(recorded: str | None) -> str | None:
    """The ``%N`` pane id at the end of a recorded ``<session>:@<window>.%<pane>``, or ``None``.

    tmux forbids ``.`` and ``:`` in session names, so the last ``.`` separates the pane.
    """
    if not recorded:
        return None
    _, _, pane = recorded.rpartition(".")
    return pane if pane.startswith("%") and pane[1:].isdecimal() else None


def list_tmux_panes() -> dict[str, TmuxPane] | None:
    """Every pane of the tmux server by pane id, or ``None`` when tmux is not installed or the call fails."""
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", _LIST_PANES_FORMAT],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    panes: dict[str, TmuxPane] = {}
    for line in result.stdout.splitlines():
        # The pane title comes last and may itself hold the separator.
        fields = line.split(_FIELD_SEPARATOR, 4)
        if len(fields) != 5 or not fields[0].startswith("%"):
            continue
        pane_id, session, window_index, window_id, title = fields
        panes[pane_id] = TmuxPane(
            pane_id=pane_id,
            session=session,
            window_index=int(window_index) if window_index.isdecimal() else None,
            window_id=window_id,
            pane_title=title,
        )
    return panes


@dataclass(frozen=True)
class SessionView:
    """One running session with the parts that are resolved live."""

    session: ActiveSession
    tmux_pane: str | None
    tmux_state: TmuxState | None
    """``None`` when the session does not run in tmux."""
    tmux_live: TmuxPane | None
    git: GitLocation | None

    @property
    def messaging_address(self) -> str | None:
        socket = self.session.messaging_socket
        return f"uds:{socket}" if socket is not None else None


def build_views(sessions: Sequence[ActiveSession], *, with_git: bool = True) -> list[SessionView]:
    """Resolve the tmux pane of every session with one tmux call, and the git location of each distinct cwd once."""
    needs_tmux = any(pane_id_of(s.tmux) is not None for s in sessions)
    panes = list_tmux_panes() if needs_tmux else None
    git_cache: dict[Path, GitLocation | None] = {}
    views = []
    for session in sessions:
        pane_id = pane_id_of(session.tmux)
        state: TmuxState | None = None
        live: TmuxPane | None = None
        if pane_id is not None:
            if panes is None:
                state = "unknown"
            else:
                live = panes.get(pane_id)
                state = "present" if live is not None else "gone"
        git: GitLocation | None = None
        if with_git and session.cwd is not None:
            if session.cwd not in git_cache:
                git_cache[session.cwd] = git_location(session.cwd)
            git = git_cache[session.cwd]
        views.append(SessionView(session=session, tmux_pane=pane_id, tmux_state=state, tmux_live=live, git=git))
    return views


def resolve_profile_globs(patterns: Iterable[str], profiles: Sequence[ClaudeProfile] | None = None) -> list[str]:
    """The names of the profiles that match any of the globs, in the order of :func:`list_profiles`.

    A glob is matched case-sensitively against the whole name, so a plain profile name matches only itself.

    Raises:
        ProfileNotFound: a glob matches no profile. The message names the known profiles.
    """
    known = list_profiles() if profiles is None else list(profiles)
    names = [p.name for p in known]
    selected: set[str] = set()
    for pattern in patterns:
        matched = [name for name in names if name == pattern or fnmatch.fnmatchcase(name, pattern)]
        if not matched:
            raise ProfileNotFound(
                f"No Claude Code profile matches {pattern!r}. Known profiles: {', '.join(names) or 'none'}."
            )
        selected.update(matched)
    return [name for name in names if name in selected]


@dataclass(frozen=True)
class Filters:
    """The ``list-active`` filters. Every one that is set must match."""

    statuses: frozenset[str] = frozenset()
    scope: Scope | None = None
    """``--here``: the session's cwd must belong to it."""
    in_tmux: bool | None = None
    name_glob: str | None = None
    """Matched case-insensitively against the whole name. A session without a name never matches."""

    def matches(self, session: ActiveSession) -> bool:
        if self.statuses and session.status not in self.statuses:
            return False
        if self.scope is not None and (session.cwd is None or not self.scope.covers(session.cwd)):
            return False
        if self.in_tmux is not None and (pane_id_of(session.tmux) is not None) != self.in_tmux:
            return False
        return self.name_glob is None or (
            session.name is not None and fnmatch.fnmatchcase(session.name.lower(), self.name_glob.lower())
        )


def apply_filters(sessions: Iterable[ActiveSession], filters: Filters) -> list[ActiveSession]:
    return [s for s in sessions if filters.matches(s)]


def sort_sessions(sessions: Iterable[ActiveSession]) -> list[ActiveSession]:
    """By profile name, then start time, then pid. A session without a start time sorts first in its profile."""
    return sorted(sessions, key=lambda s: (s.profile.name, s.started_at.timestamp() if s.started_at else 0.0, s.pid))


class SessionLookupError(LookupError):
    """A SESSION argument matched no session or several; the message lists the candidates, one per line."""


def describe_session(session: ActiveSession) -> str:
    return f"{session.address}  pid {session.pid}  {session.session_id or '-'}  {session.status or '-'}"


def lookup_session(query: str, sessions: Sequence[ActiveSession]) -> ActiveSession:
    """Match a full session id, an exact name, a pid, or a unique session id prefix, in that order of preference.

    An all-digit query is a pid when a session has that pid, so it never falls through to a session id prefix.

    Raises:
        SessionLookupError: nothing or more than one session matches.
    """
    if query.isdecimal() and any(s.pid == int(query) for s in sessions):
        query = f"pid:{query}"
    try:
        return find_session(query, sessions=sessions)
    except AmbiguousSession as exc:
        lines = [f"Error: {len(exc.candidates)} sessions match {query!r}:"]
        lines += [f"  {describe_session(s)}" for s in exc.candidates]
        raise SessionLookupError("\n".join(lines)) from exc
    except SessionNotFound as exc:
        lines = [f"Error: no active session matches {query!r}."]
        if sessions:
            lines.append("Active sessions:")
            lines += [f"  {describe_session(s)}" for s in sessions]
        raise SessionLookupError("\n".join(lines)) from exc


def transcript_path(session: ActiveSession) -> Path | None:
    """The session's transcript, looked up first in the project dirs of its launch dir, or ``None`` when it has none.

    A session that has not written a transcript yet costs a stat of every project dir of its profile.
    """
    if session.session_id is None:
        return None
    try:
        return find_session_transcript(
            session.session_id, [session.profile], cwd=session.launch_cwd or session.cwd
        ).path
    except TranscriptNotFound:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _path(value: Path | None) -> str | None:
    return str(value) if value is not None else None


def _plain(value: Any) -> Any:
    """A JSON-ready copy of a raw registry value; the record is JSON already, wrapped in read-only mappings."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def to_json(view: SessionView, *, transcript: Path | None) -> dict[str, Any]:
    """The JSON object of one session. The keys are the contract that ``--json`` documents; keep them stable."""
    s = view.session
    return {
        "profile": s.profile.name,
        "profile_config_dir": str(s.profile.config_dir),
        "profile_source": s.profile_source,
        "address": s.address,
        "pid": s.pid,
        "session_id": s.session_id,
        "name": s.name,
        "name_source": s.name_source,
        "status": s.status,
        "kind": s.kind,
        "entrypoint": s.entrypoint,
        "version": s.version,
        "started_at": _iso(s.started_at),
        "updated_at": _iso(s.updated_at),
        "cwd": _path(s.cwd),
        "launch_cwd": _path(s.launch_cwd),
        "git_branch": view.git.branch if view.git else None,
        "git_worktree_root": _path(view.git.worktree_root) if view.git else None,
        "git_repo_root": _path(view.git.repo_root) if view.git else None,
        "tmux_pane": view.tmux_pane,
        "tmux_state": view.tmux_state,
        "tmux_live": view.tmux_live.to_json() if view.tmux_live else None,
        "tmux_recorded": s.tmux,
        "messaging_socket": _path(s.messaging_socket),
        "messaging_address": view.messaging_address,
        "can_receive_messages": s.can_receive_messages,
        "peer_protocol": s.peer_protocol,
        "peer_features": sorted(s.peer_features),
        "bridge_session_id": s.bridge_session_id,
        "scratchpad_dir": _path(s.scratchpad_dir),
        "scratchpad_dir_inferred": s.scratchpad_dir_inferred,
        "transcript_path": _path(transcript),
        "record_path": str(s.record_path),
        "alive": True,
        "raw": _plain(s.raw),
    }


def shorten_home(path: Path | str) -> str:
    """``path`` with the home directory replaced by ``~``."""
    text = os.fspath(path)
    home = os.path.expanduser("~")
    if home and home != "/" and (text == home or text.startswith(home + os.sep)):
        return "~" + text[len(home) :]
    return text
