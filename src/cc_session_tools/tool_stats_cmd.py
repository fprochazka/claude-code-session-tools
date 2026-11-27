"""The ``cc-session-tools tool-stats`` command: where a session's tool time went.

:func:`tool_stats` computes every section as plain data from ``session_time``'s model; ``--json`` prints it as is,
and the markdown renderer only formats it. Stdout carries the output and nothing else; errors go to stderr.

A call's ``wall`` is elapsed time, from when it could start to when its result was written. Calls of one response and
of different lanes overlap, so the totals of a column are sums of elapsed times, not a share of the session's clock.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from .discovery import AmbiguousTarget, projects_dirs_override, resolve_target
from .redact import count, redact, redacting_session, span, tokens
from .session_time import (
    AGENT_TOOLS,
    HANDBACK_TOOL,
    MOVE_REASONS,
    SessionTime,
    Subagent,
    TimedCall,
    analyze,
    crons_by_prompt,
    union,
)
from .timeline_cmd import SUSPECT_WAIT, suspected

if TYPE_CHECKING:
    from .bash_groups import BashClass, BashGrouper

COMMAND_LIMIT = 100
"""Commands are cut to this many characters in the markdown; the JSON keeps 200."""


def tool_stats_command(
    target: str | None = typer.Argument(
        None, help="A .jsonl path, a session id or a prefix of one, or the literal 'latest'."
    ),
    sessions_from: str | None = typer.Option(
        None,
        "--sessions-from",
        metavar="FILE",
        help="Add up the sessions listed in FILE, one session id or .jsonl path per line; '-' reads stdin.",
    ),
    top: int = typer.Option(
        10, "--top", min=1, metavar="<int>", help="How many slowest calls and repeated commands to list, at least 1."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print every section as JSON, durations in seconds."),
) -> None:
    """Show where a session's tool time went: per tool, Bash by purpose, the slowest and the repeated commands,
    permission prompts, and what the subagents cost by role. With --sessions-from, add it up over many sessions."""
    if (target is None) == (sessions_from is None):
        typer.echo("Error: give either a TARGET or --sessions-from FILE", err=True)
        raise typer.Exit(code=1)

    from .bash_groups import BashGrouper, BashGroupsError, load_user_groups

    try:
        grouper = BashGrouper(load_user_groups())
    except BashGroupsError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    if sessions_from is not None:
        try:
            targets = _read_targets(sessions_from)
        except OSError as e:
            typer.echo(f"Error: --sessions-from: {e}", err=True)
            raise typer.Exit(code=1) from e
        data = tool_stats_many(targets, top=top)
        output = json.dumps(data, indent=2, ensure_ascii=False) + "\n" if as_json else render_many(data)
        sys.stdout.write(output)
        return

    assert target is not None
    try:
        transcript = resolve_target(target, cwd=os.getcwd(), projects_dirs=projects_dirs_override())
    except AmbiguousTarget as e:
        typer.echo(e.listing(), err=True)
        raise typer.Exit(code=1) from e
    if transcript is None:
        typer.echo(f"Error: could not resolve session target: {target}", err=True)
        raise typer.Exit(code=1)
    st = analyze(transcript)
    with redacting_session(transcript.cwd):
        data = tool_stats(st, grouper, top=top)
        output = json.dumps(data, indent=2, ensure_ascii=False) + "\n" if as_json else render(data, transcript.title)
    sys.stdout.write(output)


def _read_targets(source: str) -> list[str]:
    """The non-empty lines of ``source`` (``-`` for stdin) that are not ``#`` comments, each a session id or path."""
    text = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    lines = (line.strip() for line in text.splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def _session_stats(target: str, top: int) -> ToolStats:
    """One session's sections, for :func:`tool_stats_many` to add up; it runs in a child process."""
    from .bash_groups import BashGrouper, load_user_groups

    try:
        transcript = resolve_target(target, cwd=os.getcwd(), projects_dirs=projects_dirs_override())
    except AmbiguousTarget as e:
        raise LookupError(str(e)) from None  # a plain exception, so it pickles back from the child process
    if transcript is None:
        raise LookupError("could not resolve the session")
    st = analyze(transcript)
    stats = ToolStats(BashGrouper(load_user_groups()))
    with redacting_session(transcript.cwd):
        stats.add(st, title=transcript.title, top=top)
    return stats.detached()


def tool_stats_many(targets: Sequence[str], *, top: int = 10, isolate: bool = True) -> dict[str, Any]:
    """The sections added up over ``targets``, plus a row per session and totals per kind.

    Each session is analysed in a child process of its own, one at a time, which returns only its sums: a large
    session takes gigabytes while it is analysed, and Python's allocator would keep most of that from the next one.
    ``isolate=False`` analyses them in this process instead. A target that can't be resolved or read is reported on
    stderr, listed in ``skipped`` and left out.
    """
    total = ToolStats(None)
    skipped: list[dict[str, str]] = []
    context = multiprocessing.get_context("fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn")
    for target in targets:
        try:
            if isolate:
                with ProcessPoolExecutor(max_workers=1, mp_context=context) as pool:
                    one = pool.submit(_session_stats, target, top).result()
            else:
                one = _session_stats(target, top)
        except Exception as e:  # noqa: BLE001 - one broken transcript must not end a run over a hundred sessions
            error = redact(f"{type(e).__name__}: {e}", limit=200)
            skipped.append({"target": redact(target), "error": error})
            typer.echo(f"Warning: skipped {redact(target)}: {error}", err=True)
            continue
        total.merge(one)
    return {
        "sessions": [{k: v for k, v in row.items() if k != "bash_groups"} for row in total.sessions],
        "by_kind": total.by_kind(),
        "skipped": skipped,
        **total.result(top=top),
    }


# Data


@dataclass(frozen=True)
class _Call:
    call: TimedCall
    lane_type: str
    """``main`` or the subagent's type."""
    bash: BashClass | None
    command: str | None
    """The redacted command of a Bash call."""
    repeat_key: str | None
    """The normalized command its repeats share."""


def _lane_type(sub: Subagent | None) -> str:
    return "main" if sub is None else (sub.agent_type or "?").split(":")[-1]


def _add_busy(rows: dict[str, list[float]], key: str, turns: float, seconds: float) -> None:
    row = rows.setdefault(key, [0, 0.0])
    row[0] += turns
    row[1] += seconds


def _by_busy(rows: dict[str, list[float]]) -> list[tuple[str, int, float]]:
    """``rows`` of ``[turns, seconds]`` as ``(key, turns, seconds)``, most busy first."""
    return sorted(((k, int(n), s) for k, (n, s) in rows.items()), key=lambda row: (-row[2], -row[1]))


def _summary(walls: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(walls),
        "total": sum(walls),
        "median": statistics.median(walls) if walls else 0.0,
        "max": max(walls, default=0.0),
    }


def _decision_key(c: TimedCall, recorded: bool) -> str | None:
    p = c.permission
    if p is None:
        return "none" if recorded else None
    key = p.source or "?"
    if p.decision == "reject" and p.source != "user_reject":
        key += " reject" if p.reason_type in (None, p.source) else f" reject ({p.reason_type})"
    return key


def _root(sub: Subagent, by_id: dict[str, Subagent]) -> Subagent:
    seen = {sub.agent_id}
    while sub.parent_id is not None and sub.parent_id in by_id and sub.parent_id not in seen:
        sub = by_id[sub.parent_id]
        seen.add(sub.agent_id)
    return sub


@dataclass
class _Walls:
    """The walls of one row of a section and what it counts besides them."""

    walls: list[float] = field(default_factory=list)
    main: int = 0
    subagents: int = 0
    types: Counter[str] = field(default_factory=Counter)
    programs: Counter[str] = field(default_factory=Counter)
    polls: int = 0
    poll_wall: float = 0.0
    sessions: set[str] = field(default_factory=set)
    group: str | None = None

    def merge(self, other: _Walls) -> None:
        self.walls += other.walls
        self.main += other.main
        self.subagents += other.subagents
        self.types.update(other.types)
        self.programs.update(other.programs)
        self.polls += other.polls
        self.poll_wall += other.poll_wall
        self.sessions |= other.sessions
        self.group = self.group or other.group


@dataclass
class _Role:
    active: list[float] = field(default_factory=list)
    guessed: bool = False
    rounds: int = 0
    tokens_total: int = 0
    tokens_new: int = 0
    contexts: list[int] = field(default_factory=list)
    models: Counter[str] = field(default_factory=Counter)

    def merge(self, other: _Role) -> None:
        self.active += other.active
        self.guessed |= other.guessed
        self.rounds += other.rounds
        self.tokens_total += other.tokens_total
        self.tokens_new += other.tokens_new
        self.contexts += other.contexts
        self.models.update(other.models)


@dataclass
class _Nested:
    n: int = 0
    active: float = 0.0
    elapsed: float = 0.0
    tokens_total: int = 0
    types: Counter[str] = field(default_factory=Counter)

    def merge(self, other: _Nested) -> None:
        self.n += other.n
        self.active += other.active
        self.elapsed += other.elapsed
        self.tokens_total += other.tokens_total
        self.types.update(other.types)


_TOTALS = (
    "calls", "main_calls", "subagent_calls", "tool_wall", "user_wait_calls", "user_wait_wall", "main_busy",
    "batched_calls", "background_bash", "suspect_waits", "suspect_wait_wall", "subagents", "subagents_elapsed",
    "stalls", "stalled", "context_full_errors", "background_bash_run_wall", "moved_bash",
    "moved_bash_run_wall", "moved_bash_unfinished",
)  # fmt: skip


_COUNTS = frozenset(
    ("calls", "main_calls", "subagent_calls", "user_wait_calls", "batched_calls", "background_bash", "suspect_waits",
     "subagents", "stalls", "context_full_errors", "moved_bash",
     "moved_bash_unfinished")
)  # fmt: skip


class ToolStats:
    """The sections of ``tool-stats``, added up session by session.

    :meth:`add` reads one analysed session and keeps only sums, walls and counts, never the session itself, so a list
    of large sessions takes the memory of the largest one. Call it inside the session's :func:`redacting_session`.
    """

    def __init__(self, grouper: BashGrouper | None) -> None:
        self._grouper = grouper
        self.totals: dict[str, float] = dict.fromkeys(_TOTALS, 0.0)
        self.sessions: list[dict[str, Any]] = []
        self._tools: defaultdict[str, _Walls] = defaultdict(_Walls)
        self._groups: defaultdict[str, _Walls] = defaultdict(_Walls)
        self._repeated: defaultdict[str, _Walls] = defaultdict(_Walls)
        self._similar: Counter[tuple[str, str | None]] = Counter()
        self._slowest: dict[tuple[str, str | None], dict[str, Any]] = {}
        self._recorded: list[bool] = []
        self._decisions: Counter[str] = Counter()
        self._user_waits: list[dict[str, Any]] = []
        self._denials: Counter[str] = Counter()
        self._moved_reasons: Counter[str] = Counter()
        # [turns, seconds]; plain dicts, so the stats pickle back from a child process
        self._busy_by_trigger: dict[str, list[float]] = {}
        self._crons: dict[str, list[float]] = {}
        self._roles: defaultdict[tuple[str, str, bool], _Role] = defaultdict(_Role)
        self._nested: defaultdict[str, _Nested] = defaultdict(_Nested)

    def add(self, st: SessionTime, *, title: str | None = None, top: int = 10) -> None:
        from .bash_groups import group_label, normalize

        assert self._grouper is not None, "a detached ToolStats only merges"

        by_id = {s.agent_id: s for s in st.subagents}
        calls: list[_Call] = []
        for lane_id, lane_calls in (("main", st.main.calls), *((s.agent_id, s.lane.calls) for s in st.subagents)):
            lane_type = _lane_type(by_id.get(lane_id))
            for tc in lane_calls:
                command = tc.input.get("command")
                if tc.name == "Bash" and isinstance(command, str):
                    cleaned = redact(command)
                    found = self._grouper.classify(command)
                    calls.append(_Call(tc, lane_type, found, cleaned, normalize(cleaned, home="")))
                else:
                    calls.append(_Call(tc, lane_type, None, None, None))

        # A call the user answered a prompt or dialog for waited on the user for an unknown part of its wall: the
        # transcript doesn't say when the answer came. It is left out of the tool sections and shown with the prompts.
        tool_calls = [c for c in calls if not c.call.user_wait]
        user_waits = [c for c in calls if c.call.user_wait]
        timed = [c for c in tool_calls if c.call.name not in AGENT_TOOLS]
        bash = [c for c in tool_calls if c.bash is not None]
        suspects = [c for c in calls if suspected(c.call)]
        moved = [c for c in bash if c.call.auto_backgrounded]
        totals: dict[str, float] = {
            "calls": len(calls),
            "main_calls": sum(1 for c in calls if c.call.lane == "main"),
            "subagent_calls": sum(1 for c in calls if c.call.lane != "main"),
            "tool_wall": sum(c.call.run_wall for c in timed),
            "user_wait_calls": len(user_waits),
            "user_wait_wall": sum(c.call.wall for c in user_waits),
            "main_busy": st.totals.main_busy,
            "batched_calls": st.totals.batched_calls,
            "background_bash": sum(1 for c in bash if c.call.background),
            "background_bash_run_wall": sum(c.call.run_wall for c in bash if c.call.background),
            "moved_bash": len(moved),
            "moved_bash_run_wall": sum(c.call.run_wall for c in moved),
            "moved_bash_unfinished": sum(1 for c in moved if c.call.finished_at is None),
            "suspect_waits": len(suspects),
            "suspect_wait_wall": sum(c.call.wall for c in suspects),
            "subagents": len(st.subagents),
            "subagents_elapsed": union(b for s in st.subagents for b in s.bursts),
            "stalls": st.totals.stalls,
            "stalled": st.totals.stalled,
            "context_full_errors": st.totals.context_full_errors,
        }
        for key, value in totals.items():
            self.totals[key] += value

        for c in tool_calls:
            row = self._tools[c.call.name]
            row.walls.append(c.call.run_wall)
            if c.call.lane == "main":
                row.main += 1
            else:
                row.types[c.lane_type] += 1
        group_walls: defaultdict[str, float] = defaultdict(float)
        for c in bash:
            assert c.bash is not None
            name = group_label(c.bash)
            group_walls[name] += c.call.run_wall
            row = self._groups[name]
            row.walls.append(c.call.run_wall)
            if c.call.lane == "main":
                row.main += 1
            else:
                row.subagents += 1
            if c.bash.program:
                row.programs[c.bash.program] += 1
            if c.bash.waits_on is not None:
                row.polls += 1
                row.poll_wall += c.call.run_wall
            if c.repeat_key:
                repeat = self._repeated[c.repeat_key]
                repeat.walls.append(c.call.run_wall)
                repeat.types[c.lane_type] += 1
                repeat.sessions.add(st.session_id)
                repeat.group = repeat.group or name
        self._add_slowest(st.session_id, timed, top)

        recorded = st.has_permission_decisions
        self._recorded.append(recorded)
        self._decisions.update(d for c in calls if (d := _decision_key(c.call, recorded)) is not None)
        self._user_waits += [
            {
                "session_id": st.session_id,
                "tool": c.call.name,
                "lane": c.call.lane,
                "start": c.call.start.isoformat(),
                "wall": c.call.wall,
                "decision": (c.call.permission.source if c.call.permission else None) or c.call.denial,
            }
            for c in user_waits
        ]
        self._denials.update(c.call.denial for c in calls if c.call.denial)
        self._moved_reasons.update(c.call.moved_reason for c in moved if c.call.moved_reason)
        for trigger, busy in st.totals.busy_by_trigger.items():
            _add_busy(self._busy_by_trigger, trigger, busy.turns, busy.seconds)
        for cron in crons_by_prompt(st.turns):
            _add_busy(self._crons, redact(cron.prompt, limit=200), cron.turns, cron.seconds)

        for s in st.subagents:
            role = self._roles[(s.role, _lane_type(s), s.parent_id is not None)]
            role.active.append(s.active)
            role.guessed |= s.role_source != "prefix"
            role.rounds += s.rounds
            role.tokens_total += s.lane.tokens_total
            role.tokens_new += s.lane.tokens_new
            if s.lane.first_context:
                role.contexts.append(s.lane.first_context)
            role.models[(s.lane.model or "?").removeprefix("claude-")] += 1
        nested_by_role: defaultdict[str, list[Subagent]] = defaultdict(list)
        for s in st.subagents:
            if s.parent_id is not None:
                nested_by_role[_root(s, by_id).role].append(s)
        for name, subs in nested_by_role.items():
            nested = self._nested[name]
            nested.n += len(subs)
            nested.active += sum(s.active for s in subs)
            nested.elapsed += union(b for s in subs for b in s.bursts)
            nested.tokens_total += sum(s.lane.tokens_total for s in subs)
            nested.types.update(_lane_type(s) for s in subs)

        top_group = max(group_walls.items(), key=lambda kv: kv[1], default=(None, 0.0))
        self.sessions.append(
            {
                "session_id": st.session_id,
                "title": redact(title, limit=60) if title else None,
                "kind": st.kind,
                "start": st.wall.start.isoformat(),
                "wall": st.totals.wall,
                "active": st.totals.active,
                "main_busy": st.totals.main_busy,
                "tool_wall": totals["tool_wall"],
                "top_group": top_group[0],
                "top_group_wall": top_group[1],
                "suspect_wait_wall": totals["suspect_wait_wall"],
                "user_wait_wall": totals["user_wait_wall"],
                "stalled": st.totals.stalled,
                "compactions": st.totals.compactions,
                "subagent_compactions": st.totals.subagent_compactions,
                "subagents": len(st.subagents),
                "bash_groups": dict(group_walls),
            }
        )

    def _add_slowest(self, session_id: str, timed: Sequence[_Call], top: int) -> None:
        """Count the Bash calls per normalized command, and keep the details of a session's ``top`` slowest.

        Every other call stands alone, under a key of its own that is never counted, so it has no similar calls.
        The overall ``top`` are always among them: a command slower than the session's ``top`` ranks above it
        overall too."""
        from .bash_groups import group_label

        kept: dict[tuple[str, str | None], _Call] = {}
        for c in sorted(timed, key=lambda c: -c.call.run_wall):
            if c.call.name == HANDBACK_TOOL or c.call.run_wall <= 0:
                continue
            if c.repeat_key is None:
                alone = f"call {session_id} {c.call.id or c.call.start.isoformat()}"
                key: tuple[str, str | None] = (c.call.name, alone)
            else:
                key = (c.call.name, c.repeat_key)
                self._similar[key] += 1
            if key not in kept and len(kept) < top:
                kept[key] = c
        for key, c in kept.items():
            best = self._slowest.get(key)
            if best is None or c.call.run_wall > best["run_wall"]:
                self._slowest[key] = {
                    "session_id": session_id,
                    "tool": c.call.name,
                    "lane": c.call.lane,
                    "lane_type": c.lane_type,
                    "start": c.call.start.isoformat(),
                    "wall": c.call.wall,
                    "run_wall": c.call.run_wall,
                    "auto_backgrounded": c.call.auto_backgrounded,
                    "background": c.call.name == "Bash" and c.call.background,
                    "suspect_wait": suspected(c.call),
                    "bash_group": group_label(c.bash) if c.bash is not None else None,
                    "command": c.command[:200] if c.command is not None else None,
                }

    def result(self, *, top: int = 10) -> dict[str, Any]:
        """Every section as JSON-ready data: durations in seconds, nothing rounded."""
        t: dict[str, float | int] = {k: int(v) if k in _COUNTS else v for k, v in self.totals.items()}
        bash_wall = sum(sum(row.walls) for row in self._groups.values())
        tools = [
            {
                "tool": name,
                **_summary(row.walls),
                "share": sum(row.walls) / t["tool_wall"] if t["tool_wall"] and name not in AGENT_TOOLS else None,
                "main": row.main,
                "subagents_by_type": dict(row.types.most_common()),
            }
            for name, row in self._tools.items()
        ]
        groups = [
            {
                "group": name,
                **_summary(row.walls),
                "share": sum(row.walls) / bash_wall if bash_wall else None,
                "main": row.main,
                "subagents": row.subagents,
                "programs": dict(row.programs.most_common(4)),
                "waits_on_other_builds": row.polls,
                "waits_on_other_builds_wall": row.poll_wall,
            }
            for name, row in self._groups.items()
        ]
        slowest = sorted(self._slowest.items(), key=lambda kv: -kv[1]["run_wall"])[:top]
        repeated = [
            {
                "command": key[:200],
                **_summary(row.walls),
                "sessions": len(row.sessions),
                "bash_group": row.group,
                "lanes_by_type": dict(row.types.most_common()),
            }
            for key, row in self._repeated.items()
            if len(row.walls) > 1
        ]
        user_waits = sorted(self._user_waits, key=lambda w: -w["wall"])
        by_tool: dict[str, dict[str, Any]] = {}
        for w in user_waits:
            row = by_tool.setdefault(w["tool"], {"n": 0, "wall": 0.0})
            row["n"] += 1
            row["wall"] += w["wall"]
        roles = [
            {
                "role": role,
                "guessed": row.guessed,
                "type": kind,
                "nested": nested,
                "n": len(row.active),
                "active": sum(row.active),
                "median": statistics.median(row.active),
                "max": max(row.active),
                "rounds": row.rounds,
                "tokens_total": row.tokens_total,
                "tokens_new": row.tokens_new,
                "first_context_median": statistics.median(row.contexts) if row.contexts else None,
                "models": [model for model, _ in row.models.most_common()],
            }
            for (role, kind, nested), row in self._roles.items()
        ]
        nested_rows = [
            {
                "parent_role": role,
                "n": row.n,
                "active": row.active,
                "elapsed": row.elapsed,
                "tokens_total": row.tokens_total,
                "by_type": dict(row.types.most_common()),
            }
            for role, row in self._nested.items()
        ]
        return {
            "totals": {
                **t,
                "moved_bash_by_reason": {r: n for r in MOVE_REASONS if (n := self._moved_reasons[r])},
                "busy_by_trigger": {k: {"turns": n, "seconds": s} for k, n, s in _by_busy(self._busy_by_trigger)},
                "crons": [{"prompt": k, "turns": n, "seconds": s} for k, n, s in _by_busy(self._crons)],
            },
            "tools": sorted(tools, key=lambda r: (-r["total"], -r["n"])),
            "bash_groups": sorted(groups, key=lambda r: (-r["total"], -r["n"])),
            "slowest": [{**row, "similar": max(self._similar[key] - 1, 0)} for key, row in slowest],
            "repeated": sorted(repeated, key=lambda r: (-r["total"], -r["n"]))[:top],
            "permissions": {
                "recorded": all(self._recorded) if self._recorded else False,
                "sessions_without_decisions": sum(1 for recorded in self._recorded if not recorded),
                "decisions": dict(self._decisions.most_common()),
                "user_answered": len(user_waits),
                "user_wait": sum(w["wall"] for w in user_waits),
                "user_waits_by_tool": dict(sorted(by_tool.items(), key=lambda kv: (-kv[1]["wall"], -kv[1]["n"]))),
                "user_waits": user_waits,
                "denials": dict(self._denials.most_common()),
            },
            "subagent_roles": sorted(roles, key=lambda r: -r["active"]),
            "nested_by_parent_role": sorted(nested_rows, key=lambda r: -r["active"]),
        }

    def detached(self) -> ToolStats:
        """This without its Bash grouper, so it pickles small for the trip back from a child process."""
        self._grouper = None
        return self

    def merge(self, other: ToolStats) -> None:
        """Add the sessions ``other`` has seen to this one."""
        for key, value in other.totals.items():
            self.totals[key] += value
        self.sessions += other.sessions
        for mine, theirs in (
            (self._tools, other._tools),
            (self._groups, other._groups),
            (self._repeated, other._repeated),
        ):
            for key, row in theirs.items():
                mine[key].merge(row)
        self._similar.update(other._similar)
        for slow_key, best in other._slowest.items():
            if slow_key not in self._slowest or best["run_wall"] > self._slowest[slow_key]["run_wall"]:
                self._slowest[slow_key] = best
        self._recorded += other._recorded
        self._decisions.update(other._decisions)
        self._user_waits += other._user_waits
        self._denials.update(other._denials)
        self._moved_reasons.update(other._moved_reasons)
        for mine_rows, their_rows in ((self._busy_by_trigger, other._busy_by_trigger), (self._crons, other._crons)):
            for key, (turns, seconds) in their_rows.items():
                _add_busy(mine_rows, key, turns, seconds)
        for role_key, role in other._roles.items():
            self._roles[role_key].merge(role)
        for nested_key, nested in other._nested.items():
            self._nested[nested_key].merge(nested)

    def by_kind(self) -> dict[str, dict[str, Any]]:
        kinds: dict[str, dict[str, Any]] = {}
        for row in self.sessions:
            kind = kinds.setdefault(
                row["kind"],
                {"sessions": 0, "wall": 0.0, "active": 0.0, "main_busy": 0.0, "tool_wall": 0.0,
                 "suspect_wait_wall": 0.0, "user_wait_wall": 0.0, "stalled": 0.0, "subagents": 0,
                 "bash_groups": Counter()},
            )  # fmt: skip
            kind["sessions"] += 1
            sums = ("wall", "active", "main_busy", "tool_wall", "suspect_wait_wall", "user_wait_wall", "stalled")
            for key in (*sums, "subagents"):
                kind[key] += row[key]
            kind["bash_groups"].update(row["bash_groups"])
        for kind in kinds.values():
            kind["bash_groups"] = dict(kind["bash_groups"].most_common())
        return dict(sorted(kinds.items(), key=lambda kv: -kv[1]["active"]))


def tool_stats(st: SessionTime, grouper: BashGrouper, *, top: int = 10) -> dict[str, Any]:
    """Every section of ``tool-stats`` for one session as JSON-ready data: durations in seconds, nothing rounded."""
    stats = ToolStats(grouper)
    stats.add(st, top=top)
    return {"session_id": st.session_id, **stats.result(top=top)}


# Markdown


def _share(value: float | None) -> str:
    return "" if value is None else f"{round(value * 100)}"


def _table(head: Sequence[str], right: Sequence[bool], rows: Iterable[Sequence[str]]) -> list[str]:
    lines = ["| " + " | ".join(head) + " |", "|" + "|".join("---:" if r else "---" for r in right) + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return lines


def _types(counts: dict[str, int], limit: int = 3) -> str:
    return ", ".join(f"{k} {v}" for k, v in list(counts.items())[:limit])


def _cut(text: str | None, limit: int = COMMAND_LIMIT) -> str:
    text = (text or "").replace("`", "'")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def render(data: dict[str, Any], title: str | None) -> str:
    header = f"# Tool stats {data['session_id'][:8]}" + (f" — {redact(title, limit=80)}" if title else "")
    return "\n".join([header, "", *_sections(data, many=False)]) + "\n"


def render_many(data: dict[str, Any]) -> str:
    """The markdown of ``--sessions-from``: a row per session, the totals per kind, then the sections added up."""
    sessions = data["sessions"]
    out = [f"# Tool stats over {count(len(sessions), 'session')}", ""]
    if data["skipped"]:
        out += [f"Skipped {count(len(data['skipped']), 'session')}:"]
        out += [f"- {s['target']}: {s['error']}" for s in data["skipped"]]
        out.append("")
    out += ["## Sessions, by active time", ""]
    out += _table(
        ("session", "start", "kind", "active", "main busy", "tool wall", "top Bash group", "⌛ suspect", "user waits",
         "✖ stalled"),
        (False, False, False, True, True, True, False, True, True, True),
        (
            (s["session_id"][:8], f"{datetime.fromisoformat(s['start']).astimezone():%Y-%m-%d}", s["kind"],
             span(s["active"]), span(s["main_busy"]), span(s["tool_wall"]),
             f"{s['top_group']} {span(s['top_group_wall'])}" if s["top_group"] else "", span(s["suspect_wait_wall"]),
             span(s["user_wait_wall"]), span(s["stalled"]))
            for s in sorted(sessions, key=lambda s: -s["active"])
        ),
    )  # fmt: skip
    out += ["", "## By session kind", ""]
    out += _table(
        ("kind", "sessions", "active", "main busy", "tool wall", "⌛ suspected", "user waits", "✖ stalled",
         "top Bash groups"),
        (False, True, True, True, True, True, True, True, False),
        (
            (kind, str(k["sessions"]), span(k["active"]), span(k["main_busy"]), span(k["tool_wall"]),
             span(k["suspect_wait_wall"]), span(k["user_wait_wall"]), span(k["stalled"]),
             ", ".join(f"{g} {span(w)}" for g, w in list(k["bash_groups"].items())[:3]))
            for kind, k in data["by_kind"].items()
        ),
    )  # fmt: skip
    out.append("")
    return "\n".join([*out, *_sections(data, many=True)]) + "\n"


_MOVE_REASON_TEXT = {
    "cap": "at the subagent cap",
    "timeout": "at their own timeout",
    "deliver_message": "to deliver a message",
    "user": "by the user",
    "other": "for another reason",
}


def _sections(data: dict[str, Any], *, many: bool) -> list[str]:
    t = data["totals"]
    out = [
        f"{count(t['calls'], 'tool call')} ({t['main_calls']} main, {t['subagent_calls']} in subagents). A call's wall"
        " runs from when it could start to its result; calls overlap, so the totals are sums of elapsed times, not"
        f" shares of the clock. `%` is the share of all non-Agent tool wall ({span(t['tool_wall'])}). Main agent busy"
        f" {span(t['main_busy'])}.",
    ]
    if t["busy_by_trigger"]:
        busy = " · ".join(
            f"{k} {count(v['turns'], 'turn')} {span(v['seconds'])}" for k, v in t["busy_by_trigger"].items()
        )
        out += ["", f"Main agent busy by what opened the turn: {busy}."]
    if t["crons"]:
        crons = " · ".join(f'"{_cut(c["prompt"], 60)}" ×{c["turns"]} {span(c["seconds"])}' for c in t["crons"][:5])
        more = f" · {len(t['crons']) - 5} more" if len(t["crons"]) > 5 else ""
        out += ["", f"Cron turns by prompt: {crons}{more}."]
    if t["user_wait_calls"]:
        out += [
            "",
            f"{count(t['user_wait_calls'], 'call')} waited on the user, {span(t['user_wait_wall'])} in all: a"
            " permission prompt the user answered, or a dialog. The transcript doesn't say when the answer came, so"
            " they are left out of the tool sections and shown under Permission decisions.",
        ]
    if t["context_full_errors"]:
        out += [
            "",
            f"✖ {count(t['context_full_errors'], 'prompt')} refused on a full context (all lanes); the main agent"
            f" stalled {count(t['stalls'], 'time')}, {span(t['stalled'])} in all.",
        ]
    if t["batched_calls"]:
        out += ["", f"{t['batched_calls']} results were held back behind a subagent; their runtime is unknown, so 0."]
    if t["suspect_waits"]:
        out += [
            "",
            f"⌛ {count(t['suspect_waits'], 'call')} over {int(SUSPECT_WAIT.total_seconds() // 3600)} h,"
            f" {span(t['suspect_wait_wall'])} in all, probably waited on something the transcript doesn't show;"
            " they are counted and marked `⌛`.",
        ]
    out += ["", "## Per tool", ""]
    out += _table(
        ("tool", "n", "total", "median", "max", "%", "main", "in subagents (by type)"),
        (False, True, True, True, True, True, True, False),
        (
            (r["tool"], str(r["n"]), span(r["total"]), span(r["median"]), span(r["max"]), _share(r["share"]),
             str(r["main"]), _types(r["subagents_by_type"]))
            for r in data["tools"]
        ),
    )  # fmt: skip
    out += ["", "## Bash by group (a command counts for its highest-priority group)", ""]
    out += _table(
        ("group", "n", "total", "median", "max", "%", "main", "subagents", "top programs"),
        (False, True, True, True, True, True, True, True, False),
        (
            (r["group"], str(r["n"]), span(r["total"]), span(r["median"]), span(r["max"]), _share(r["share"]),
             str(r["main"]), str(r["subagents"]), ", ".join(r["programs"]))
            for r in data["bash_groups"]
        ),
    )  # fmt: skip
    polls = [r for r in data["bash_groups"] if r["waits_on_other_builds"]]
    if polls:
        out += [
            "",
            "Polls on a build or test tool's process (another session's job, or the agent's own): "
            + ", ".join(
                f"{r['waits_on_other_builds']} in {r['group']}, {span(r['waits_on_other_builds_wall'])}" for r in polls
            ),
        ]
    if t["moved_bash"]:
        unfinished = t["moved_bash_unfinished"]
        reasons = ", ".join(f"{n} {_MOVE_REASON_TEXT[reason]}" for reason, n in t["moved_bash_by_reason"].items() if n)
        out += [
            "",
            f"↷ Moved to the background: {count(t['moved_bash'], 'Bash call')} ({reasons}); they ran"
            f" {span(t['moved_bash_run_wall'])} until their completion notices"
            + (f", {unfinished} without a notice, counted until the move" if unfinished else "")
            + ". The tool sections count that runtime; the lane and busy totals count only the time until the move.",
        ]
    if t["background_bash"]:
        out += [
            "",
            f"Background Bash: {count(t['background_bash'], 'call')}, ran {span(t['background_bash_run_wall'])} until"
            " their completion notices or, without one, their launch.",
        ]
    out += ["", "## Slowest calls", ""]
    for r in data["slowest"]:
        group = f" [{r['bash_group']}]" if r["bash_group"] else ""
        similar = f" (+{r['similar']} similar)" if r["similar"] else ""
        mark = (" ⌛" if r["suspect_wait"] else "") + (" ↷" if r["auto_backgrounded"] else "")
        command = f": `{_cut(r['command'])}`" if r["command"] else ""
        when = datetime.fromisoformat(r["start"]).astimezone()
        where = f"{r['session_id'][:8]} " if many else ""
        took = span(r["run_wall"])
        out.append(f"- {took}{mark} {where}{when:%m-%d %H:%M} {r['lane_type']} {r['tool']}{group}{similar}{command}")
    out += ["", "## Repeated commands (the `cd` prefix dropped, the scratchpad and runs of 4+ digits replaced)", ""]
    for r in data["repeated"]:
        sessions = f" in {count(r['sessions'], 'session')}" if many else ""
        out.append(
            f"- ×{r['n']}{sessions} total {span(r['total'])} (max {span(r['max'])}) [{r['bash_group']}] in"
            f" {_types(r['lanes_by_type'])}: `{_cut(r['command'])}`"
        )
    if not data["repeated"]:
        out.append("none")
    p = data["permissions"]
    out += ["", "## Permission decisions (all lanes)", ""]
    if p["recorded"]:
        out.append(" · ".join(f"{k} {v}" for k, v in p["decisions"].items()) or "none")
    elif not p["decisions"]:
        out.append("not recorded: this Claude Code version wrote no permission decisions, so only dialogs count below")
    else:
        out += [
            " · ".join(f"{k} {v}" for k, v in p["decisions"].items()),
            "",
            f"{count(p['sessions_without_decisions'], 'session')} ran on a Claude Code version that wrote no"
            " permission decisions; only their dialogs count below.",
        ]
    waits = ", ".join(f"{w['tool']} {span(w['wall'])}" for w in p["user_waits"][:6])
    rest = len(p["user_waits"]) - 6
    out += [
        "",
        f"Prompts the user answered: {p['user_answered']}, total wait {span(p['user_wait'])}"
        + (f": {waits}" if waits else "")
        + (f" and {rest} shorter" if rest > 0 else ""),
    ]
    if len(p["user_waits_by_tool"]) > 1:
        out.append(
            "By tool: "
            + " · ".join(f"{tool} {row['n']} {span(row['wall'])}" for tool, row in p["user_waits_by_tool"].items())
        )
    if p["denials"]:
        out.append("Denials: " + ", ".join(f"{k} {v}" for k, v in p["denials"].items()))
    out += [
        "",
        "## Subagent cost by role",
        "",
        f"{t['subagents']} subagents, elapsed {span(t['subagents_elapsed'])}"
        + (" (each session's union of their active time, added up)" if many else " (the union of their active time)")
        + ". `active`"
        " is each lane's own time; a parent's includes its wait on its nested subagents, so the column adds up to more"
        " than the elapsed time.",
        "",
    ]
    out += _table(
        ("role", "type", "n", "active", "median", "max", "rounds", "tokens", "new tokens", "first context", "model"),
        (False, False, True, True, True, True, True, True, True, True, False),
        (
            (f"{r['role']}{'?' if r['guessed'] else ''}", f"{r['type']}{' nested' if r['nested'] else ''}",
             str(r["n"]), span(r["active"]), span(r["median"]), span(r["max"]), str(r["rounds"]),
             tokens(r["tokens_total"]), tokens(r["tokens_new"]),
             tokens(int(r["first_context_median"])) if r["first_context_median"] is not None else "?",
             ", ".join(r["models"][:2]))
            for r in data["subagent_roles"]
        ),
    )  # fmt: skip
    if data["nested_by_parent_role"]:
        out += ["", "Nested subagents by the role of the top-level subagent above them (elapsed is their union):", ""]
        out += [
            f"- {r['parent_role']}: {r['n']} nested ({_types(r['by_type'])}), elapsed {span(r['elapsed'])},"
            f" {tokens(r['tokens_total'])} tok"
            for r in data["nested_by_parent_role"]
        ]
    return out
