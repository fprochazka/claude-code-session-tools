"""Running Claude Code sessions, with their tmux pane and git branch resolved live.

cc-blackbox reads the session registry of every profile. Two things in a registry record cannot be taken at face value,
so this module looks them up again:

- ``tmux``: Claude Code writes ``<session>:@<window>.%<pane>`` once, at startup. Moving a window to another tmux
  session leaves the record stale. Only the pane id ``%N`` stays valid for the life of the pane, so the session and
  window are read from the running tmux server, with one ``tmux list-panes -a`` call per tmux server. Pane ids are
  numbered per server, so each pane is looked up on the server the session runs on: cc-blackbox reads its socket
  from ``TMUX`` in the session's process environment.
- the git branch: the record has none. It comes from ``git rev-parse`` in the session's current directory.

The record's ``updatedAt`` changes with the status and with local commands such as ``/login``, and can lag behind
responses, so it does not say when the session last worked. That comes
from the transcript: the newest model response in it or in any of its subagents' transcripts.
"""

from __future__ import annotations

import fnmatch
import functools
import os
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from cc_blackbox import (
    ActiveSession,
    AmbiguousSession,
    ClaudeBlackboxError,
    ClaudeProfile,
    ContextWindow,
    CostState,
    ProfileNotFound,
    SessionNotFound,
    SessionTranscript,
    find_session,
    find_session_transcript,
    list_profiles,
    read_session_transcript,
)
from rich.text import Text

from .discovery import Scope
from .gitrepo import GitLocation, git_location
from .screen import Classification, classify

TmuxState = Literal["present", "gone", "unknown"]
"""``present``: the pane exists and runs the session's process. ``gone``: the tmux server runs and has no such pane, or
the pane id belongs to a pane whose process is not the session's process or one of its ancestors. ``unknown``: tmux is
not installed, or ``tmux list-panes`` failed, for example because no server runs on the default socket."""

_FIELD_SEPARATOR = "\t"
_LIST_PANES_FORMAT = _FIELD_SEPARATOR.join(
    (
        "#{pane_id}",
        "#{session_name}",
        "#{window_index}",
        "#{window_id}",
        "#{session_id}",
        "#{pane_pid}",
        "#{pid}",
        "#{socket_path}",
        "#{pane_title}",
    )
)


@dataclass(frozen=True)
class TmuxPane:
    """Where a pane is now, according to the tmux server."""

    pane_id: str
    session: str
    window_index: int | None
    window_id: str
    session_id: str
    """The tmux session's ``$N`` id, which stays when the session is renamed."""
    pane_pid: int | None
    """The process the pane runs, which the session's process must be or descend from for the pane to be its own."""
    server_pid: int | None
    """The pid of the tmux server that has the pane."""
    socket_path: str
    """The socket of the tmux server that has the pane, with symlinks resolved."""
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


@functools.lru_cache(maxsize=256)
def real_socket(path: str) -> str:
    """``path`` with symlinks resolved, so two spellings of one tmux socket compare equal."""
    return os.path.realpath(path)


def _server_args(socket: str | Path | None) -> list[str]:
    """``-S <socket>`` for a tmux server other than the caller's, nothing for the one bare ``tmux`` reaches."""
    return ["-S", os.fspath(socket)] if socket is not None else []


def list_tmux_panes(socket: str | Path | None = None) -> dict[str, TmuxPane] | None:
    """Every pane of a tmux server by pane id, or ``None`` when tmux is not installed or the call fails.

    ``socket`` picks the server; without it, the server bare ``tmux`` reaches: the one of ``$TMUX``, else the default.
    Another server gets a shorter timeout, so a hung server of one session cannot hold up a watch's poll for long.
    """
    try:
        result = subprocess.run(
            ["tmux", *_server_args(socket), "list-panes", "-a", "-F", _LIST_PANES_FORMAT],
            capture_output=True,
            text=True,
            check=False,
            timeout=10 if socket is None else 2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    panes: dict[str, TmuxPane] = {}
    for line in result.stdout.splitlines():
        # The pane title comes last and may itself hold the separator.
        fields = line.split(_FIELD_SEPARATOR, 8)
        if len(fields) != 9 or not fields[0].startswith("%"):
            continue
        pane_id, session, window_index, window_id, session_id, pane_pid, server_pid, socket_path, title = fields
        panes[pane_id] = TmuxPane(
            pane_id=pane_id,
            session=session,
            window_index=int(window_index) if window_index.isdecimal() else None,
            window_id=window_id,
            session_id=session_id,
            pane_pid=int(pane_pid) if pane_pid.isdecimal() else None,
            server_pid=int(server_pid) if server_pid.isdecimal() else None,
            socket_path=real_socket(socket_path),
            pane_title=title,
        )
    return panes


def _tmux(*args: str, socket: str | Path | None = None) -> str | None:
    try:
        result = subprocess.run(
            ["tmux", *_server_args(socket), *args], capture_output=True, text=True, check=False, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def capture_pane(pane_id: str, socket: str | Path | None = None) -> str | None:
    """What the pane shows now, with its colors and attributes as escape sequences, or ``None`` when tmux fails."""
    return _tmux("capture-pane", "-p", "-e", "-t", pane_id, socket=socket)


def _parent_pid(pid: int) -> int | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        return int(stat[stat.rfind(")") + 2 :].split(" ")[1])
    except (OSError, ValueError, IndexError):
        pass
    try:
        result = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return int(value) if result.returncode == 0 and value.isdecimal() else None


@dataclass(frozen=True)
class PaneState:
    """What tmux says about a pane besides its screen."""

    pid: int
    """The process the pane runs."""
    in_mode: bool
    """The pane is in a mode such as copy mode, which takes the keys sent to the pane, not the program in it."""
    cursor: tuple[int, int] | None
    """The cursor's column and line in the pane, counted from 0 at the top left."""


def pane_state(pane_id: str, socket: str | Path | None = None) -> PaneState | None:
    """The pane's process, mode and cursor, or ``None`` when tmux has no such pane or does not answer."""
    output = _tmux(
        "display-message", "-p", "-t", pane_id, "#{pane_pid} #{pane_in_mode} #{cursor_x} #{cursor_y}", socket=socket
    )
    fields = output.split() if output is not None else []
    if len(fields) != 4 or not fields[0].isdecimal():
        return None
    x, y = fields[2], fields[3]
    cursor = (int(x), int(y)) if x.isdecimal() and y.isdecimal() else None
    return PaneState(pid=int(fields[0]), in_mode=fields[1] == "1", cursor=cursor)


def runs_in_pane(pane_pid: int, pid: int) -> bool:
    """Whether ``pid`` is the pane's process ``pane_pid`` or one of its descendants.

    tmux numbers panes anew after its server restarts, so a recorded pane id can name another program's pane.
    """
    current: int | None = pid
    for _ in range(64):
        if current is None or current <= 1:
            return False
        if current == pane_pid:
            return True
        current = _parent_pid(current)
    return False


@dataclass(frozen=True)
class ScreenCapture:
    """One capture of a session's pane, classified, or why there is none."""

    reason: str | None
    """Why nothing was captured: ``tmux_unavailable``, ``pane_gone``, ``pane_not_owned``, ``pane_in_mode`` or
    ``capture_failed``; ``None`` when the pane was captured."""
    classification: Classification
    """``unknown`` when nothing was captured."""
    text: str | None = None
    """The captured text with its escape sequences."""
    captured_at: datetime | None = None


def _check_pane(pane: str, pid: int, socket: str | Path | None) -> tuple[str | None, PaneState | None]:
    """Why the pane cannot be captured for the session ``pid`` now, or ``None`` and the pane's state when it can."""
    state = pane_state(pane, socket)
    if state is None:
        panes = list_tmux_panes(socket)
        if panes is None:
            return "tmux_unavailable", None
        return ("pane_gone" if pane not in panes else "capture_failed"), None
    if not runs_in_pane(state.pid, pid):
        return "pane_not_owned", None
    if state.in_mode:
        return "pane_in_mode", None
    return None, state


def capture_screen(pane: str, pid: int, socket: str | Path | None = None) -> ScreenCapture:
    """Capture the pane of the session ``pid`` on the tmux server of ``socket`` and classify what it shows.

    The pane must run the session's process and be in no tmux mode, before and after the capture: a pane can enter
    copy mode, or close, while it is captured, and then the capture does not count. The cursor counts only when it did
    not move during the capture. Costs three or four tmux calls.
    """
    reason, before = _check_pane(pane, pid, socket)
    if reason is not None or before is None:
        return ScreenCapture(reason, Classification("unknown"))
    captured = capture_pane(pane, socket)
    reason, after = _check_pane(pane, pid, socket)
    if captured is None or after is None:
        return ScreenCapture(reason or "capture_failed", Classification("unknown"))
    cursor = after.cursor if before.cursor == after.cursor else None
    return ScreenCapture(None, classify(captured, cursor), captured, datetime.now(UTC))


@dataclass(frozen=True)
class SessionView:
    """One running session with the parts that are resolved live."""

    session: ActiveSession
    tmux_pane: str | None
    tmux_state: TmuxState | None
    """``None`` when the session does not run in tmux."""
    tmux_live: TmuxPane | None
    """The session's pane where the tmux server has it now; ``None`` unless :attr:`tmux_state` is ``present``."""
    git: GitLocation | None
    tmux_not_owned: bool = False
    """``True`` when :attr:`tmux_state` is ``gone`` because the pane exists but runs another process."""
    tmux_socket: str | None = None
    """The socket of the tmux server the session runs on: the one its environment names, else the caller's server
    when the pane was found and owned there, else ``None``."""
    tmux_other_server: bool = False
    """``True`` when the pane is on a tmux server other than the one bare ``tmux`` reaches."""
    waiting_for: str | None = None
    """What the session waits for, with ``plan approval`` told apart from a permission prompt: see
    :func:`derive_waiting_for`."""

    @property
    def messaging_address(self) -> str | None:
        socket = self.session.messaging_socket
        return f"uds:{socket}" if socket is not None else None


CURRENT_TMUX_SESSION = "current"
"""The ``--tmux-session`` value that stands for the tmux session this command runs in."""


@dataclass(frozen=True)
class TmuxSession:
    """A tmux session that ``--tmux-session`` selected."""

    id: str
    """The ``$N`` id, which the selection matches, so that renaming the tmux session keeps its panes selected."""
    name: str
    """The name at the moment it was resolved."""
    socket: str
    """The socket of the caller's tmux server, which has the tmux session; ``$N`` ids repeat on every server."""


class TmuxSessionNotFound(LookupError):
    """A ``--tmux-session`` value names no tmux session of the running server."""


def resolve_tmux_session(value: str, panes: Mapping[str, TmuxPane] | None) -> TmuxSession:
    """The tmux session that ``--tmux-session`` selects.

    ``current`` is the session of the pane in ``$TMUX_PANE``, the pane this command runs in, where that pane is now.
    Any other value must be the exact name of a session of the tmux server, so a typo is an error instead of an empty
    selection. ``panes`` is the listing of the caller's tmux server, the one bare ``tmux`` reaches; sessions on other
    servers cannot be selected.

    Raises:
        TmuxSessionNotFound: the message says why.
    """
    if panes is None:
        raise TmuxSessionNotFound("--tmux-session: cannot read the tmux server; is tmux installed and running?")
    if value == CURRENT_TMUX_SESSION:
        pane = os.environ.get("TMUX_PANE")
        if not pane:
            raise TmuxSessionNotFound("--tmux-session current: this command does not run in tmux ($TMUX_PANE is unset)")
        if pane not in panes:
            raise TmuxSessionNotFound(f"--tmux-session current: the tmux server has no pane {pane} ($TMUX_PANE)")
        return TmuxSession(panes[pane].session_id, panes[pane].session, panes[pane].socket_path)
    found = {p.session: p for p in panes.values()}
    if value not in found:
        raise TmuxSessionNotFound(
            f"--tmux-session: no tmux session is named {value!r}. tmux sessions: {', '.join(sorted(found)) or 'none'}."
        )
    return TmuxSession(found[value].session_id, value, found[value].socket_path)


@dataclass(frozen=True)
class TmuxServers:
    """The pane listings of the caller's tmux server and of the other servers the sessions run on.

    A listing is ``None`` when its server did not answer: tmux is missing, or no server runs on that socket.
    """

    caller: Mapping[str, TmuxPane] | None
    """The listing of the server bare ``tmux`` reaches."""
    others: Mapping[str, Mapping[str, TmuxPane] | None] = field(default_factory=dict)
    """The listings of the other servers, by socket with symlinks resolved."""

    @property
    def caller_socket(self) -> str | None:
        """The socket of the caller's server, from its listing; ``None`` when it did not answer."""
        return next((p.socket_path for p in self.caller.values()), None) if self.caller else None

    def socket_key(self, session: ActiveSession) -> str | None:
        """The socket whose listing holds the session's pane, or ``None`` for the caller's server.

        A session whose socket is unknown, because its environment could not be read, has no ``TMUX`` or names a
        relative socket, is looked up on the caller's server; the ownership check keeps a foreign pane out.
        """
        if session.tmux_socket is None:
            return None
        real = real_socket(os.fspath(session.tmux_socket))
        return None if real == self.caller_socket else real

    def listing(self, session: ActiveSession) -> Mapping[str, TmuxPane] | None:
        """The listing of the server the session runs on, or ``None`` when that server was not listed or did not
        answer."""
        key = self.socket_key(session)
        return self.caller if key is None else self.others.get(key)

    def has(self, session: ActiveSession) -> bool:
        """Whether the listing of the session's server holds its recorded pane id."""
        pane_id = pane_id_of(session.tmux)
        listing = self.listing(session)
        return pane_id is not None and listing is not None and pane_id in listing


def list_tmux_servers(sessions: Iterable[ActiveSession], caller: Mapping[str, TmuxPane] | None = None) -> TmuxServers:
    """List the caller's tmux server, and each other server a session in tmux runs on, once.

    ``caller`` is a listing of the caller's server the caller already has. A dead socket gives ``None`` for its own
    listing only, so the sessions of the other servers keep their panes.
    """
    in_tmux = [s for s in sessions if pane_id_of(s.tmux) is not None]
    servers = TmuxServers(list_tmux_panes() if caller is None else caller)
    others: dict[str, Mapping[str, TmuxPane] | None] = {}
    for session in in_tmux:
        key = servers.socket_key(session)
        if key is not None and key not in others:
            others[key] = list_tmux_panes(key)
    return replace(servers, others=others)


@dataclass(frozen=True)
class PanePlacement:
    """Where a session's recorded pane is now, and whether it is the session's own."""

    state: TmuxState | None
    """``None`` when the session does not run in tmux."""
    live: TmuxPane | None
    """The pane, only when :attr:`state` is ``present``."""
    not_owned: bool = False
    """The listing has the pane id, but the pane runs another process: :attr:`state` is ``gone``."""


_OWNERSHIP_CACHE_SIZE = 4096
_owned: set[tuple[object, ...]] = set()


def owns_pane(session: ActiveSession, live: TmuxPane) -> bool:
    """Whether the session's process runs in the pane: it is the pane's process or descends from it.

    A yes is kept per session process, pane and tmux server, so a watch walks the parents of a session once per pane
    rather than every few seconds; off Linux each step spawns ``ps``. A no is not kept, so a ``ps`` call that failed
    once does not lose the pane for the rest of a watch. A pid that another process reuses has another ``procStart``,
    and a restarted server another pid, so neither reuses an old answer. A kept yes can be stale only for a record
    without ``procStart`` whose pid is reused by another process in the same pane, or for a dead pane that
    ``remain-on-exit`` keeps while its session was reparented; it never decides a capture, which checks again
    without the cache.
    """
    if live.pane_pid is None:
        return False
    key = (
        session.pid,
        session.raw.get("procStart"),
        live.socket_path,
        live.server_pid,
        live.pane_id,
        live.pane_pid,
    )
    if key in _owned:
        return True
    if not runs_in_pane(live.pane_pid, session.pid):
        return False
    if len(_owned) >= _OWNERSHIP_CACHE_SIZE:
        _owned.clear()
    _owned.add(key)
    return True


def place_pane(session: ActiveSession, servers: TmuxServers | None) -> PanePlacement:
    """Look the session's recorded pane up on the tmux server the session runs on, and hand it out only when it is the
    session's own.

    The pane id alone does not say that: pane ids restart at ``%0`` with every tmux server, so the id a session
    recorded can name another program's pane after a server restart, or on a server other than the one the session
    runs on. The pane is ``gone`` when its server has another pid than the session's ``TMUX`` names, and it counts
    only when its process is the session's process or one of its ancestors; see :func:`owns_pane`.
    """
    pane_id = pane_id_of(session.tmux)
    if pane_id is None:
        return PanePlacement(None, None)
    listing = servers.listing(session) if servers is not None else None
    if listing is None:
        return PanePlacement("unknown", None)
    live = listing.get(pane_id)
    if live is None:
        return PanePlacement("gone", None)
    restarted = session.tmux_server_pid is not None and live.server_pid != session.tmux_server_pid
    if restarted or not owns_pane(session, live):
        return PanePlacement("gone", None, not_owned=True)
    return PanePlacement("present", live)


def live_pane(session: ActiveSession, servers: TmuxServers | None) -> TmuxPane | None:
    """The pane the session runs in, where its tmux server has it now, or ``None``; see :func:`place_pane`."""
    return place_pane(session, servers).live


def session_socket(session: ActiveSession, placement: PanePlacement) -> str | None:
    """The ``tmux_socket`` of ``--json``: the socket the session's environment names, else the caller's server's
    socket when the pane was found and owned there, else ``None``."""
    if session.tmux_socket is not None:
        return os.fspath(session.tmux_socket)
    return placement.live.socket_path if placement.live is not None else None


def build_views(
    sessions: Sequence[ActiveSession],
    *,
    with_git: bool = True,
    servers: TmuxServers | None = None,
) -> list[SessionView]:
    """Resolve the tmux pane of every session with one tmux call per tmux server, and the git location of each
    distinct cwd once.

    A pane counts only when the session runs in it; see :func:`place_pane`.

    A session whose record waits for a permission prompt and that runs in a pane also costs a capture of that pane,
    to tell a plan approval from it; see :func:`derive_waiting_for`.

    ``servers`` holds listings the caller already has; without it, tmux is asked when a session runs in tmux.
    """
    if servers is None and any(pane_id_of(s.tmux) is not None for s in sessions):
        servers = list_tmux_servers(sessions)
    git_cache: dict[Path, GitLocation | None] = {}
    views = []
    for session in sessions:
        placement = place_pane(session, servers)
        git: GitLocation | None = None
        if with_git and session.cwd is not None:
            if session.cwd not in git_cache:
                git_cache[session.cwd] = git_location(session.cwd)
            git = git_cache[session.cwd]
        waits = derive_waiting_for(session) if placement.state == "present" else waiting_for(session)
        views.append(
            SessionView(
                session=session,
                tmux_pane=pane_id_of(session.tmux),
                tmux_state=placement.state,
                tmux_live=placement.live,
                git=git,
                tmux_not_owned=placement.not_owned,
                tmux_socket=session_socket(session, placement),
                tmux_other_server=placement.live is not None
                and servers is not None
                and placement.live.socket_path != servers.caller_socket,
                waiting_for=waits,
            )
        )
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
    profiles: frozenset[str] = frozenset()
    """Profile names; empty for every profile."""
    scope: Scope | None = None
    """``--here``: the session's cwd must belong to it."""
    in_tmux: bool | None = None
    name_glob: str | None = None
    """Matched case-insensitively against the whole name. A session without a name never matches."""
    tmux_session_id: str | None = None
    """The ``$N`` id of the tmux session the session's pane must be in now, by the live tmux server. The id, not the
    name, so a tmux session renamed during a watch keeps its panes. A pane that is not the session's own never
    matches: callers pass only what :func:`place_pane` hands out."""
    tmux_socket: str | None = None
    """The socket of the server that has :attr:`tmux_session_id`, with symlinks resolved; ``$N`` ids repeat on every
    server, so a pane on another server never matches."""
    role_globs: tuple[str, ...] = ()
    """The auth-switch role must match one of these, case-sensitively. A session without a role never matches."""

    def matches(self, session: ActiveSession, tmux_live: TmuxPane | None = None, role: str | None = None) -> bool:
        """``tmux_live`` is where the session's pane is now, and ``role`` its auth-switch role; only
        :attr:`tmux_session_id` and :attr:`role_globs` read them."""
        if self.statuses and session.status not in self.statuses:
            return False
        if self.profiles and session.profile.name not in self.profiles:
            return False
        if self.scope is not None and (session.cwd is None or not self.scope.covers(session.cwd)):
            return False
        if self.in_tmux is not None and (pane_id_of(session.tmux) is not None) != self.in_tmux:
            return False
        if self.tmux_session_id is not None and (
            tmux_live is None
            or tmux_live.session_id != self.tmux_session_id
            or tmux_live.socket_path != self.tmux_socket
        ):
            return False
        if self.role_globs and (role is None or not any(fnmatch.fnmatchcase(role, g) for g in self.role_globs)):
            return False
        return self.name_glob is None or (
            session.name is not None and fnmatch.fnmatchcase(session.name.lower(), self.name_glob.lower())
        )


def apply_filters(
    sessions: Iterable[ActiveSession], filters: Filters, servers: TmuxServers | None = None
) -> list[ActiveSession]:
    """``servers`` holds the tmux listings, needed only by :attr:`Filters.tmux_session_id`. The role is read only for
    :attr:`Filters.role_globs`."""
    return [
        s
        for s in sessions
        if filters.matches(s, live_pane(s, servers), process_role(s) if filters.role_globs else None)
    ]


def sort_sessions(sessions: Iterable[ActiveSession]) -> list[ActiveSession]:
    """By profile name, then start time, then pid. A session without a start time sorts first in its profile."""
    return sorted(sessions, key=lambda s: (s.profile.name, s.started_at.timestamp() if s.started_at else 0.0, s.pid))


class SessionLookupError(LookupError):
    """A SESSION argument matched no session or several; the message lists the candidates, one per line."""

    def __init__(self, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.ambiguous = ambiguous


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
        raise SessionLookupError("\n".join(lines), ambiguous=True) from exc
    except SessionNotFound as exc:
        lines = [f"Error: no active session matches {query!r}."]
        if sessions:
            lines.append("Active sessions:")
            lines += [f"  {describe_session(s)}" for s in sessions]
        raise SessionLookupError("\n".join(lines)) from exc


@dataclass(frozen=True)
class Activity:
    """When a running session last worked: its newest model response, or its start when it has no response yet."""

    at: datetime | None
    started_only: bool
    """``True`` when :attr:`at` is the session's start, because its transcript is missing or holds no response."""


def activity_of(session: ActiveSession) -> Activity:
    """Read the session's newest model response, in its main transcript or any subagent's, falling back to its start.

    Costs a read of the transcript's start and end, and of the subagent transcripts written after that response.
    """
    transcript = session.transcript
    last = transcript.last_model_activity_at if transcript is not None else None
    return Activity(last, False) if last is not None else Activity(session.started_at, True)


def role_of(transcript: SessionTranscript | None) -> str | None:
    """The auth-switch role of the session's newest start, from the start log next to its transcript.

    For a session that has ended. ``None`` when the session has no transcript or no start log, or its newest run was
    started without a role. Reads the start log, a few lines, on every call.
    """
    if transcript is None:
        return None
    starts = transcript.profile_starts()
    return max(starts, key=lambda s: s.started_at_ms).role if starts else None


def process_role(session: ActiveSession) -> str | None:
    """The auth-switch role that the running session's process was started with, from the start log.

    Not the newest start of the session: two processes can run the same session, each with its own role. A session
    waiting for its first prompt has the log, though not its transcript yet. ``/clear`` and an in-process ``/resume``
    keep the process, and the start hook logs its role under the new session id. ``None`` when the process logged no
    start or was started without a role. Reads the start log, a few lines, on every call, and looks it up by the
    current session id, so it stays right for a session that :func:`~cc_session_tools.watcher.refreshed` updated.
    """
    start = session.process_start()
    return start.role if start is not None else None


def read_last_response(session: ActiveSession) -> datetime | None:
    """The session's newest model response, in its main transcript or any subagent's, read from the files now.

    :attr:`ActiveSession.transcript` is a snapshot of the moment the session was listed; a watch needs the current
    value. A transcript that is not at :attr:`ActiveSession.transcript_path`, because the session wrote none when it was
    listed or moved it on a cwd change, is looked up by session id. ``None`` when there is no transcript or no response.
    """
    try:
        path = session.transcript_path
        if path is not None and path.is_file():
            transcript = read_session_transcript(path)
        elif session.session_id is not None:
            transcript = find_session_transcript(
                session.session_id, [session.profile], cwd=session.launch_cwd or session.cwd
            )
        else:
            return None
        return transcript.last_model_activity_at
    except (ClaudeBlackboxError, OSError):
        return None


def find_transcript_path(session: ActiveSession) -> Path | None:
    """The session's main transcript as it is now: :attr:`ActiveSession.transcript_path` when that file exists, else
    looked up by session id, as after a ``/clear`` or a cwd change. ``None`` when the session has written none yet."""
    try:
        path = session.transcript_path
        if path is not None and path.is_file():
            return path
        if session.session_id is None:
            return None
        found = find_session_transcript(session.session_id, [session.profile], cwd=session.launch_cwd or session.cwd)
        return found.path
    except (ClaudeBlackboxError, OSError):
        return None


def read_context_window(session: ActiveSession, transcript: Path) -> ContextWindow | None:
    """How full the session's context is now, read from ``transcript``, with the session's environment applied.

    :attr:`ActiveSession.context_window` is a snapshot of the moment the session was listed; a watch needs the current
    value, so this reads it on a copy of the session. Reading costs a millisecond or two, whatever the transcript's
    size: it reads backwards from the end to the newest response.
    """
    try:
        return replace(session, transcript_path=transcript, _transcript_projects_dir=None).context_window
    except (ClaudeBlackboxError, OSError):
        return None


def active_since(sessions: Iterable[ActiveSession], since: datetime) -> list[ActiveSession]:
    """The sessions whose :func:`activity_of` is at or after ``since``. A session without any time never matches."""
    kept = []
    for session in sessions:
        at = activity_of(session).at
        if at is not None and at >= since:
            kept.append(session)
    return kept


PERMISSION_PROMPT = "permission prompt"
"""The record's ``waitingFor`` for a tool's permission prompt, and for the plan approval dialog too."""
PLAN_APPROVAL = "plan approval"
"""The ``waiting_for`` this tool reports when the pane of a session waiting for a permission prompt shows the plan
approval dialog."""


def waiting_for(session: ActiveSession) -> str | None:
    """What a ``waiting`` session waits for, from the record's ``waitingFor``; ``None`` in any other status."""
    value = session.raw.get("waitingFor")
    return value if session.status == "waiting" and isinstance(value, str) and value else None


def screen_state(session: ActiveSession) -> str | None:
    """The state of what the session's pane shows now, or ``None`` when it does not run in tmux or the pane cannot be
    captured."""
    pane = pane_id_of(session.tmux)
    if pane is None:
        return None
    capture = capture_screen(pane, session.pid, session.tmux_socket)
    return capture.classification.state if capture.reason is None else None


def derive_waiting_for(
    session: ActiveSession, screen: Callable[[ActiveSession], str | None] = screen_state
) -> str | None:
    """:func:`waiting_for`, with ``plan approval`` for a session whose pane shows the plan approval dialog.

    Claude Code's record says ``permission prompt`` for that dialog too, so a session in that state that runs in tmux
    costs one capture of its pane. A session outside tmux, a pane that cannot be captured and a screen that reads
    anything else keep the record's value.
    """
    value = waiting_for(session)
    if value == PERMISSION_PROMPT and screen(session) == "plan_approval":
        return PLAN_APPROVAL
    return value


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _path(value: Path | None) -> str | None:
    return str(value) if value is not None else None


def _seconds(value: timedelta | None) -> float | None:
    return value.total_seconds() if value is not None else None


def cost_json(cost: CostState | None) -> dict[str, Any] | None:
    """The ``cost`` object of ``--json``: the totals Claude Code recorded when a process of the session last exited."""
    if cost is None:
        return None
    return {
        "total_cost_usd": cost.total_cost_usd,
        "has_unknown_model_cost": cost.has_unknown_model_cost,
        "started_at": _iso(cost.started_at),
        "duration_seconds": _seconds(cost.duration),
        "api_duration_seconds": _seconds(cost.api_duration),
        "api_duration_without_retries_seconds": _seconds(cost.api_duration_without_retries),
        "tool_duration_seconds": _seconds(cost.tool_duration),
        "lines_added": cost.lines_added,
        "lines_removed": cost.lines_removed,
        "model_usage": {
            model: {
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "thinking_tokens": usage.thinking_tokens,
                "cache_read_input_tokens": usage.cache_read_input_tokens,
                "cache_creation_input_tokens": usage.cache_creation_input_tokens,
                "web_search_requests": usage.web_search_requests,
                "cost_usd": usage.cost_usd,
            }
            for model, usage in cost.model_usage.items()
        },
    }


def context_window_json(window: ContextWindow | None) -> dict[str, Any] | None:
    """The ``context_window`` object of ``--json``: how full the context was at the newest response of the main
    conversation, as the session's status line shows it."""
    if window is None:
        return None
    return {
        "size": window.size,
        "used": window.used,
        "percent": window.percent,
        "compacted": window.compacted,
        "model": window.model,
        "size_source": window.size_source,
        "measured_at": _iso(window.measured_at),
    }


def _tokens(count: int) -> str:
    # Thousands are cut off, as the status line cuts them off.
    return f"{count // 1_000_000}M" if count >= 1_000_000 and count % 1_000_000 == 0 else f"{count // 1000}k"


def context_window_text(window: Mapping[str, Any] | None) -> Text:
    """``312k/1M 31%``, or ``0k/1M compacted`` after a compaction. The size is dimmed when it is Claude Code's default
    for an unknown model, a guess."""
    if window is None:
        return Text("-", style="dim")
    text = Text(f"{_tokens(window['used'])}/")
    text.append(_tokens(window["size"]), style="dim" if window["size_source"] == "default" else "")
    text.append(" compacted" if window["compacted"] else f" {window['percent']}%")
    return text


def transcript_json(transcript: SessionTranscript | None) -> dict[str, Any]:
    """The keys that come from the transcript: ``last_activity_at``, ``last_record_at``, ``cost`` and
    ``context_window``."""
    if transcript is None:
        return {"last_activity_at": None, "last_record_at": None, "cost": None, "context_window": None}
    return {
        "last_activity_at": _iso(transcript.last_model_activity_at),
        "last_record_at": _iso(transcript.last_record_at),
        "cost": cost_json(transcript.cost_state),
        "context_window": context_window_json(transcript.context_window),
    }


def _plain(value: Any) -> Any:
    """A JSON-ready copy of a raw registry value; the record is JSON already, wrapped in read-only mappings."""
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


def to_json(view: SessionView) -> dict[str, Any]:
    """The JSON object of one session. The keys are the contract that ``--json`` documents; keep them stable.

    Reads the session's transcript, the whole file when it has no ``cost-state`` record, which is the case for a running
    session that was never resumed.
    """
    s = view.session
    from_transcript = transcript_json(s.transcript)
    return {
        "profile": s.profile.name,
        "profile_config_dir": str(s.profile.config_dir),
        "profile_source": s.profile_source,
        "address": s.address,
        "pid": s.pid,
        "session_id": s.session_id,
        "name": s.name,
        "name_source": s.name_source,
        "role": process_role(s),
        "status": s.status,
        "waiting_for": view.waiting_for,
        "kind": s.kind,
        "entrypoint": s.entrypoint,
        "version": s.version,
        "started_at": _iso(s.started_at),
        "updated_at": _iso(s.updated_at),
        "last_activity_at": from_transcript["last_activity_at"],
        "last_record_at": from_transcript["last_record_at"],
        "cwd": _path(s.cwd),
        "launch_cwd": _path(s.launch_cwd),
        "git_branch": view.git.branch if view.git else None,
        "git_worktree_root": _path(view.git.worktree_root) if view.git else None,
        "git_repo_root": _path(view.git.repo_root) if view.git else None,
        "tmux_pane": view.tmux_pane,
        "tmux_socket": view.tmux_socket,
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
        "transcript_path": _path(s.transcript_path),
        "cost": from_transcript["cost"],
        # The running session's own value applies CLAUDE_CODE_DISABLE_1M_CONTEXT, which the transcript cannot see.
        "context_window": context_window_json(s.context_window),
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
