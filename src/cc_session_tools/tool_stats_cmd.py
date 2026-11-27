"""The ``cc-session-tools tool-stats`` command: where a session's tool time went.

:func:`tool_stats` computes every section as plain data from ``session_time``'s model; ``--json`` prints it as is,
and the markdown renderer only formats it. Stdout carries the output and nothing else; errors go to stderr.

A call's ``wall`` is elapsed time, from when it could start to when its result was written. Calls of one response and
of different lanes overlap, so the totals of a column are sums of elapsed times, not a share of the session's clock.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

import typer

from .discovery import projects_dirs_override, resolve_target
from .redact import count, redact, redacting_session, span, tokens
from .session_time import AGENT_TOOLS, HANDBACK_TOOL, SessionTime, Subagent, TimedCall, analyze, union
from .timeline_cmd import SUSPECT_WAIT, suspected

if TYPE_CHECKING:
    from .bash_groups import BashClass, BashGrouper

COMMAND_LIMIT = 100
"""Commands are cut to this many characters in the markdown; the JSON keeps 200."""


def tool_stats_command(
    target: str = typer.Argument(..., help="A .jsonl path, a session UUID, or the literal 'latest'."),
    top: int = typer.Option(
        10, "--top", min=1, metavar="<int>", help="How many slowest calls and repeated commands to list, at least 1."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print every section as JSON, durations in seconds."),
) -> None:
    """Show where a session's tool time went: per tool, Bash by purpose, the slowest and the repeated commands,
    permission prompts, and what the subagents cost by role."""
    transcript = resolve_target(target, cwd=os.getcwd(), projects_dirs=projects_dirs_override())
    if transcript is None:
        typer.echo(f"Error: could not resolve session target: {target}", err=True)
        raise typer.Exit(code=1)

    from .bash_groups import BashGrouper, BashGroupsError, load_user_groups

    try:
        grouper = BashGrouper(load_user_groups())
    except BashGroupsError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    st = analyze(transcript)
    with redacting_session(transcript.cwd):
        data = tool_stats(st, grouper, top=top)
        output = json.dumps(data, indent=2, ensure_ascii=False) + "\n" if as_json else render(data, transcript.title)
    sys.stdout.write(output)


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


def _bash_group(found: BashClass) -> str:
    if found.group != "test" or found.targeted is None:
        return found.group
    return "test (targeted)" if found.targeted else "test (full)"


def _summary(walls: Sequence[float]) -> dict[str, Any]:
    return {
        "n": len(walls),
        "total": sum(walls),
        "median": statistics.median(walls) if walls else 0.0,
        "max": max(walls, default=0.0),
    }


def _models(subs: Iterable[Subagent]) -> list[str]:
    counts = Counter((s.lane.model or "?").removeprefix("claude-") for s in subs)
    return [model for model, _ in counts.most_common()]


def tool_stats(st: SessionTime, grouper: BashGrouper, *, top: int = 10) -> dict[str, Any]:
    """Every section of ``tool-stats`` as JSON-ready data: durations in seconds, nothing rounded."""
    from .bash_groups import normalize

    by_id = {s.agent_id: s for s in st.subagents}
    calls: list[_Call] = []
    for lane_id, lane_calls in (("main", st.main.calls), *((s.agent_id, s.lane.calls) for s in st.subagents)):
        lane_type = _lane_type(by_id.get(lane_id))
        for c in lane_calls:
            command = c.input.get("command")
            if c.name == "Bash" and isinstance(command, str):
                cleaned = redact(command)
                calls.append(_Call(c, lane_type, grouper.classify(command), cleaned, normalize(cleaned, home="")))
            else:
                calls.append(_Call(c, lane_type, None, None, None))

    # A call the user answered a prompt or dialog for waited on the user for an unknown part of its wall: the
    # transcript doesn't say when the answer came. It is left out of the tool sections and shown with the prompts.
    tool_calls = [c for c in calls if not c.call.user_wait]
    user_waits = [c for c in calls if c.call.user_wait]
    timed = [c for c in tool_calls if c.call.name not in AGENT_TOOLS]
    tool_wall = sum(c.call.wall for c in timed)
    bash = [c for c in tool_calls if c.bash is not None]
    bash_wall = sum(c.call.wall for c in bash)
    suspects = [c for c in calls if suspected(c.call)]
    elapsed = union(b for s in st.subagents for b in s.bursts)
    return {
        "session_id": st.session_id,
        "totals": {
            "calls": len(calls),
            "main_calls": sum(1 for c in calls if c.call.lane == "main"),
            "subagent_calls": sum(1 for c in calls if c.call.lane != "main"),
            "tool_wall": tool_wall,
            "user_wait_calls": len(user_waits),
            "user_wait_wall": sum(c.call.wall for c in user_waits),
            "main_busy": st.totals.main_busy,
            "batched_calls": st.totals.batched_calls,
            "background_bash": sum(1 for c in bash if c.call.background),
            "suspect_waits": len(suspects),
            "suspect_wait_wall": sum(c.call.wall for c in suspects),
            "subagents": len(st.subagents),
            "subagents_elapsed": elapsed,
        },
        "tools": _per_tool(tool_calls, tool_wall),
        "bash_groups": _bash_groups(bash, bash_wall),
        "slowest": _slowest(timed, top),
        "repeated": _repeated(bash, top),
        "permissions": _permissions(st, calls),
        "subagent_roles": _roles(st),
        "nested_by_parent_role": _nested(st, by_id),
    }


def _per_tool(calls: Sequence[_Call], tool_wall: float) -> list[dict[str, Any]]:
    by_tool: defaultdict[str, list[_Call]] = defaultdict(list)
    for c in calls:
        by_tool[c.call.name].append(c)
    rows = []
    for name, group in by_tool.items():
        walls = [c.call.wall for c in group]
        rows.append(
            {
                "tool": name,
                **_summary(walls),
                "share": sum(walls) / tool_wall if tool_wall and name not in AGENT_TOOLS else None,
                "main": sum(1 for c in group if c.call.lane == "main"),
                "subagents_by_type": dict(Counter(c.lane_type for c in group if c.call.lane != "main").most_common()),
            }
        )
    return sorted(rows, key=lambda r: (-r["total"], -r["n"]))


def _bash_groups(bash: Sequence[_Call], bash_wall: float) -> list[dict[str, Any]]:
    by_group: defaultdict[str, list[_Call]] = defaultdict(list)
    for c in bash:
        assert c.bash is not None
        by_group[_bash_group(c.bash)].append(c)
    rows = []
    for group, members in by_group.items():
        walls = [c.call.wall for c in members]
        waits = [c for c in members if c.bash is not None and c.bash.waits_on is not None]
        rows.append(
            {
                "group": group,
                **_summary(walls),
                "share": sum(walls) / bash_wall if bash_wall else None,
                "main": sum(1 for c in members if c.call.lane == "main"),
                "subagents": sum(1 for c in members if c.call.lane != "main"),
                "programs": dict(
                    Counter(c.bash.program for c in members if c.bash is not None and c.bash.program).most_common(4)
                ),
                "waits_on_other_builds": len(waits),
                "waits_on_other_builds_wall": sum(c.call.wall for c in waits),
            }
        )
    return sorted(rows, key=lambda r: (-r["total"], -r["n"]))


def _call_fields(c: _Call) -> dict[str, Any]:
    return {
        "tool": c.call.name,
        "lane": c.call.lane,
        "lane_type": c.lane_type,
        "start": c.call.start.isoformat(),
        "wall": c.call.wall,
        "suspect_wait": suspected(c.call),
        "bash_group": _bash_group(c.bash) if c.bash is not None else None,
        "command": c.command[:200] if c.command is not None else None,
    }


def _slowest(timed: Sequence[_Call], top: int) -> list[dict[str, Any]]:
    """The slowest calls, one per tool and normalized command, with how many similar calls it stands for."""
    seen: dict[tuple[str, str | None], dict[str, Any]] = {}
    for c in sorted(timed, key=lambda c: -c.call.wall):
        if c.call.name == HANDBACK_TOOL or c.call.wall <= 0:
            continue
        key = (c.call.name, c.repeat_key)
        if key in seen:
            seen[key]["similar"] += 1
        elif len(seen) < top:
            seen[key] = {**_call_fields(c), "similar": 0}
    return list(seen.values())


def _repeated(bash: Sequence[_Call], top: int) -> list[dict[str, Any]]:
    by_key: defaultdict[str, list[_Call]] = defaultdict(list)
    for c in bash:
        if c.repeat_key:
            by_key[c.repeat_key].append(c)
    rows = []
    for key, members in by_key.items():
        if len(members) < 2:
            continue
        walls = [c.call.wall for c in members]
        first = members[0]
        rows.append(
            {
                "command": key[:200],
                **_summary(walls),
                "bash_group": _bash_group(first.bash) if first.bash is not None else None,
                "lanes_by_type": dict(Counter(c.lane_type for c in members).most_common()),
            }
        )
    return sorted(rows, key=lambda r: (-r["total"], -r["n"]))[:top]


def _decision_key(c: TimedCall, recorded: bool) -> str | None:
    p = c.permission
    if p is None:
        return "none" if recorded else None
    key = p.source or "?"
    if p.decision == "reject" and p.source != "user_reject":
        key += " reject" if p.reason_type in (None, p.source) else f" reject ({p.reason_type})"
    return key


def _permissions(st: SessionTime, calls: Sequence[_Call]) -> dict[str, Any]:
    decisions = Counter(key for c in calls if (key := _decision_key(c.call, st.has_permission_decisions)) is not None)
    answered = [c for c in calls if c.call.user_wait]
    return {
        "recorded": st.has_permission_decisions,
        "decisions": dict(decisions.most_common()),
        "user_answered": len(answered),
        "user_wait": sum(c.call.wall for c in answered),
        "user_waits": [
            {
                "tool": c.call.name,
                "lane": c.call.lane,
                "start": c.call.start.isoformat(),
                "wall": c.call.wall,
                "decision": (c.call.permission.source if c.call.permission else None) or c.call.denial,
            }
            for c in sorted(answered, key=lambda c: -c.call.wall)
        ],  # fmt: skip
        "denials": dict(Counter(c.call.denial for c in calls if c.call.denial).most_common()),
    }


def _roles(st: SessionTime) -> list[dict[str, Any]]:
    groups: defaultdict[tuple[str, str, bool], list[Subagent]] = defaultdict(list)
    for s in st.subagents:
        groups[(s.role, _lane_type(s), s.parent_id is not None)].append(s)
    rows = []
    for (role, kind, nested), subs in groups.items():
        active = [s.active for s in subs]
        contexts = [s.lane.first_context for s in subs if s.lane.first_context]
        rows.append(
            {
                "role": role,
                "guessed": any(s.role_source != "prefix" for s in subs),
                "type": kind,
                "nested": nested,
                "n": len(subs),
                "active": sum(active),
                "median": statistics.median(active),
                "max": max(active),
                "rounds": sum(s.rounds for s in subs),
                "tokens_total": sum(s.lane.tokens_total for s in subs),
                "tokens_new": sum(s.lane.tokens_new for s in subs),
                "first_context_median": statistics.median(contexts) if contexts else None,
                "models": _models(subs),
            }
        )
    return sorted(rows, key=lambda r: -r["active"])


def _root(sub: Subagent, by_id: dict[str, Subagent]) -> Subagent:
    seen = {sub.agent_id}
    while sub.parent_id is not None and sub.parent_id in by_id and sub.parent_id not in seen:
        sub = by_id[sub.parent_id]
        seen.add(sub.agent_id)
    return sub


def _nested(st: SessionTime, by_id: dict[str, Subagent]) -> list[dict[str, Any]]:
    groups: defaultdict[str, list[Subagent]] = defaultdict(list)
    for s in st.subagents:
        if s.parent_id is not None:
            groups[_root(s, by_id).role].append(s)
    rows = [
        {
            "parent_role": role,
            "n": len(subs),
            "active": sum(s.active for s in subs),
            "elapsed": union(b for s in subs for b in s.bursts),
            "tokens_total": sum(s.lane.tokens_total for s in subs),
            "by_type": dict(Counter(_lane_type(s) for s in subs).most_common()),
        }
        for role, subs in groups.items()
    ]
    return sorted(rows, key=lambda r: -r["active"])


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
    t = data["totals"]
    out = [
        f"# Tool stats {data['session_id'][:8]}" + (f" — {redact(title, limit=80)}" if title else ""),
        "",
        f"{count(t['calls'], 'tool call')} ({t['main_calls']} main, {t['subagent_calls']} in subagents). A call's wall"
        " runs from when it could start to its result; calls overlap, so the totals are sums of elapsed times, not"
        f" shares of the clock. `%` is the share of all non-Agent tool wall ({span(t['tool_wall'])}). Main agent busy"
        f" {span(t['main_busy'])}.",
    ]
    if t["user_wait_calls"]:
        out += [
            "",
            f"{count(t['user_wait_calls'], 'call')} waited on the user, {span(t['user_wait_wall'])} in all: a"
            " permission prompt the user answered, or a dialog. The transcript doesn't say when the answer came, so"
            " they are left out of the tool sections and shown under Permission decisions.",
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
    if t["background_bash"]:
        out += ["", f"Background Bash: {count(t['background_bash'], 'call')}; their wall is the launch only."]
    out += ["", "## Slowest calls", ""]
    for r in data["slowest"]:
        group = f" [{r['bash_group']}]" if r["bash_group"] else ""
        similar = f" (+{r['similar']} similar)" if r["similar"] else ""
        mark = " ⌛" if r["suspect_wait"] else ""
        command = f": `{_cut(r['command'])}`" if r["command"] else ""
        when = datetime.fromisoformat(r["start"]).astimezone()
        out.append(
            f"- {span(r['wall'])}{mark} {when:%m-%d %H:%M} {r['lane_type']} {r['tool']}{group}{similar}{command}"
        )
    out += ["", "## Repeated commands (the `cd` prefix dropped, the scratchpad and runs of 4+ digits replaced)", ""]
    for r in data["repeated"]:
        out.append(
            f"- ×{r['n']} total {span(r['total'])} (max {span(r['max'])}) [{r['bash_group']}] in"
            f" {_types(r['lanes_by_type'])}: `{_cut(r['command'])}`"
        )
    if not data["repeated"]:
        out.append("none")
    p = data["permissions"]
    out += ["", "## Permission decisions (all lanes)", ""]
    if p["recorded"]:
        out.append(" · ".join(f"{k} {v}" for k, v in p["decisions"].items()) or "none")
    else:
        out.append("not recorded: this Claude Code version wrote no permission decisions, so only dialogs count below")
    waits = ", ".join(f"{w['tool']} {span(w['wall'])}" for w in p["user_waits"][:6])
    out += [
        "",
        f"Prompts the user answered: {p['user_answered']}, total wait {span(p['user_wait'])}"
        + (f": {waits}" if waits else ""),
    ]
    if p["denials"]:
        out.append("Denials: " + ", ".join(f"{k} {v}" for k, v in p["denials"].items()))
    out += [
        "",
        "## Subagent cost by role",
        "",
        f"{t['subagents']} subagents, elapsed {span(t['subagents_elapsed'])} (the union of their active time). `active`"
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
    return "\n".join(out) + "\n"
