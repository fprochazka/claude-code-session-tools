"""The ``cc-session-tools timeline`` command: where a session's time went, turn by turn.

The analysis lives in ``session_time``; this module only selects and formats. The markdown collapses short turns and
idle gaps until the output fits ``--max-lines``; ``--json`` prints everything, collapsed nothing. Stdout carries the
output and nothing else; errors go to stderr.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from .discovery import AmbiguousTarget, projects_dirs_override, resolve_target
from .redact import count, redact, redacting_session, span, tokens
from .session_time import (
    AGENT_TOOLS,
    AWAY,
    ApiError,
    CompactionEvent,
    Interval,
    SessionTime,
    Stall,
    Subagent,
    TimedCall,
    Totals,
    Turn,
    analyze,
    clip,
    crons_by_prompt,
    normalize_description,
    union,
)
from .watch_events import parse_duration

if TYPE_CHECKING:
    from .bash_groups import BashGrouper

SUSPECT_WAIT = timedelta(hours=1)
"""A call longer than this probably waited on something the transcript doesn't show: an unanswered prompt, a machine
that slept. It is flagged, and its time is still counted."""
USER_WAIT_SHOWN = 30.0
"""A user wait inside a turn gets its own line from this many seconds."""
PROMPT_LIMIT = 50
BASE_TURN_LIMIT = 300.0
"""Turns shorter than this are merged first; the limit grows until the output fits ``--max-lines``."""
MAX_TURN_LIMIT = 6 * 3600.0
MAX_GAP_LIMIT = 24 * 3600.0
"""The limits stop growing here: a session that still doesn't fit says so instead of hiding whole days."""

Classify = Callable[[str], str]
_COMMAND = re.compile(r"<command-(?:name|message)>([^<]+)</command-(?:name|message)>")
_TAG = re.compile(r"<[^>]+>")


def timeline(
    target: str = typer.Argument(..., help="A .jsonl path, a session id or a prefix of one, or the literal 'latest'."),
    max_lines: int = typer.Option(
        150, "--max-lines", help="Collapse short turns and gaps until the markdown fits about this many lines."
    ),
    gap: str = typer.Option("20m", "--gap", help="Idle gaps shorter than this are merged into the turns around them."),
    from_: str | None = typer.Option(
        None, "--from", help="Show from this time in full detail: HH:MM on the session's first day, or an ISO time."
    ),
    to: str | None = typer.Option(None, "--to", help="Show up to this time in full detail; the same forms as --from."),
    subagent: str | None = typer.Option(
        None, "--subagent", help="Show the timeline of one subagent, by agent id or its file name (agent-<id>)."
    ),
    as_json: bool = typer.Option(
        False, "--json", help="Print everything as JSON, durations in seconds, nothing merged."
    ),
) -> None:
    """Show where a session's time went: its turns, subagents, waits on the user, idle gaps and compactions.

    The header also gives busy time by what opened the turns, the crons by prompt, stalls on a full context, and the
    Bash calls moved to the background."""
    if _is_subagent_file(target):
        typer.echo(
            f"Error: {target} is a subagent's transcript; pass its session and --subagent {Path(target).stem}", err=True
        )
        raise typer.Exit(code=1)
    try:
        transcript = resolve_target(target, cwd=os.getcwd(), projects_dirs=projects_dirs_override())
    except AmbiguousTarget as e:
        typer.echo(e.listing(), err=True)
        raise typer.Exit(code=1) from e
    if transcript is None:
        typer.echo(f"Error: could not resolve session target: {target}", err=True)
        raise typer.Exit(code=1)
    try:
        merge_gap = parse_duration(gap)
    except ValueError as e:
        typer.echo(f"Error: --gap: {e}", err=True)
        raise typer.Exit(code=1) from e

    from .bash_groups import BashGrouper, BashGroupsError, load_user_groups

    try:
        grouper = BashGrouper(load_user_groups())
    except BashGroupsError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e

    st = analyze(transcript)
    try:
        window = _window(st, from_, to)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(code=1) from e
    selected = None
    if subagent is not None:
        wanted = subagent.removesuffix(".jsonl").removeprefix("agent-")
        selected = next((s for s in st.subagents if s.agent_id in (subagent, wanted)), None)
        if selected is None:
            typer.echo(f"Error: the session has no subagent {subagent}", err=True)
            raise typer.Exit(code=1)

    with redacting_session(transcript.cwd):
        if as_json:
            data = (
                session_json(st, grouper, window) if selected is None else subagent_json(st, selected, grouper, window)
            )
            output = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        elif selected is not None:
            output = render_subagent(st, selected, grouper, window)
        else:
            output = render_session(
                st, transcript.title, grouper, max_lines=max_lines, merge_gap=merge_gap, window=window
            )
    sys.stdout.write(output)


def _stall_line(stall: Stall) -> str:
    ended = {"compaction": " → compaction", "activity": " → answered again"}.get(stall.ended_by, ", never recovered")
    return (
        f"{_hm(stall.start)}-{_hm(stall.end)} ✖ context full: {count(stall.refused, 'prompt')} refused,"
        f" stalled {span(stall.seconds)}{ended}"
    )


def _api_error(e: ApiError) -> str:
    """``! API error <error> <status>: <message>``, each part only when the record has it."""
    head = f"! API error {redact(e.error or '', limit=60)} {e.status or ''}".rstrip()
    return f"{head}: {redact(e.message, limit=100)}" if e.message else head


def _is_subagent_file(target: str) -> bool:
    path = Path(target)
    return path.suffix == ".jsonl" and (path.name.startswith("agent-") or path.parent.name == "subagents")


# Times


def _local(moment: datetime) -> datetime:
    return moment.astimezone()


def _zones(wall: Interval) -> str:
    """The local time zone of the session, as ``CEST → CET`` when it crossed a daylight saving change."""
    start, end = f"{_local(wall.start):%Z}", f"{_local(wall.end):%Z}"
    return start if start == end else f"{start} → {end}"


def _hm(moment: datetime) -> str:
    return f"{_local(moment):%H:%M}"


def _window(st: SessionTime, start: str | None, end: str | None) -> Interval | None:
    if start is None and end is None:
        return None
    first_day = _local(st.wall.start).date()
    low = _parse_time(start, first_day, "--from") if start else st.wall.start
    high = _parse_time(end, first_day, "--to") if end else st.wall.end
    if high < low and end and len(end) <= 5:
        high += timedelta(days=1)  # --to 02:00 after --from 22:00 means the next morning
    if start and not end and low > st.wall.end:
        raise ValueError(f"--from {start} is after the session ends ({_local(st.wall.end):%Y-%m-%d %H:%M})")
    if end and not start and high < low:
        raise ValueError(f"--to {end} is before the session starts ({_local(st.wall.start):%Y-%m-%d %H:%M})")
    if high < low:
        raise ValueError("--to is before --from")
    return Interval(low, high)


def _parse_time(value: str, day: date, option: str) -> datetime:
    try:
        if len(value) <= 5 and ":" in value:
            hours, minutes = value.split(":")
            return datetime.combine(day, time(int(hours), int(minutes))).astimezone()
        moment = datetime.fromisoformat(value)
    except ValueError as e:
        raise ValueError(f"{option}: cannot read the time {value!r}; use HH:MM or an ISO time") from e
    return moment if moment.tzinfo is not None else moment.astimezone()


# Suspected waits


def suspected(call: TimedCall) -> bool:
    return call.end is not None and not call.batched and call.end - call.start > SUSPECT_WAIT


def _suspect_busy(st: SessionTime) -> tuple[int, float, float]:
    """The count of suspected waits, the main agent's busy time inside them, and the subagents' active time inside
    theirs, overlaps between subagents counted once."""
    calls = [c for c in _all_calls(st) if suspected(c)]
    main = [Interval(c.start, c.end) for c in calls if c.lane == "main" and c.end is not None]
    busy = sum(union(clip(main, Interval(t.start, t.end))) for t in st.turns)
    inside: list[Interval] = []
    for sub in st.subagents:
        own = [Interval(c.start, c.end) for c in calls if c.lane == sub.agent_id and c.end is not None]
        inside += [i for burst in sub.bursts for i in clip(own, burst)]
    return len(calls), busy, union(inside)


def subagent_active(st: SessionTime) -> tuple[float, float]:
    """The subagents' active time: the union of their bursts, as the clock ran, and the sum over their lanes, in which
    a parent's active time includes its wait on its nested subagents."""
    return union(b for s in st.subagents for b in s.bursts), sum(s.active for s in st.subagents)


def _all_calls(st: SessionTime) -> list[TimedCall]:
    return [*st.main.calls, *(c for s in st.subagents for c in s.lane.calls)]


# Markdown


def _tool_mix(calls: Iterable[TimedCall], classify: Classify, limit: int = 4) -> str:
    counts: Counter[str] = Counter()
    for call in calls:
        command = call.input.get("command")
        counts[f"Bash:{classify(command)}" if call.name == "Bash" and isinstance(command, str) else call.name] += 1
    parts = [f"{name}×{n}" if n > 1 else name for name, n in counts.most_common(limit)]
    rest = sum(counts.values()) - sum(n for _, n in counts.most_common(limit))
    return " ".join(parts) + (f" +{rest}" if rest else "")


def _model(sub: Subagent) -> str:
    return (sub.lane.model or "?").removeprefix("claude-")


def _role(sub: Subagent) -> str:
    return f"[{sub.role}{'' if sub.role_source == 'prefix' else '?'}]"


def _shape(sub: Subagent) -> str:
    return {"foreground": "fg", "background": "bg"}.get(sub.shape or "", "?")


def _subagent_line(st: SessionTime, sub: Subagent, indent: str = "    └ ") -> str:
    nested = [s for s in st.subagents if s.parent_id == sub.agent_id]
    fold = ""
    if nested:
        kinds = Counter((s.agent_type or "?").split(":")[-1] for s in nested)
        fold = (
            f" +{len(nested)} nested ({', '.join(f'{k}×{v}' for k, v in kinds.most_common())},"
            f" {span(sum(s.active for s in nested))}, {tokens(sum(s.lane.tokens_total for s in nested))})"
        )
    when = f"{_hm(sub.span.start)}-{_hm(sub.span.end)}" if sub.span else "?"
    rounds = f" {sub.rounds} rounds" if sub.rounds > 1 else ""
    bursts = f" {len(sub.bursts)} bursts" if len(sub.bursts) > 1 else ""
    kind = (sub.agent_type or "?").split(":")[-1]
    description = redact(sub.description or "", limit=40)
    return (
        f"{indent}{_shape(sub)} {kind} {_model(sub)} {_role(sub)} {when} {span(sub.active)}{bursts}{rounds}"
        f' {tokens(sub.lane.tokens_total)} tok "{description}"{fold}'
        + (" ✖ context full" if _died_context_full(sub) else "")
    )


CRONS_SHOWN = 5


def _cron_line(st: SessionTime) -> list[str]:
    """The header line of the cron turns by prompt, the busiest :data:`CRONS_SHOWN`; none without cron turns."""
    rows = crons_by_prompt(st.turns, redact)
    if not rows:
        return []
    shown = " · ".join(f'"{redact(r.prompt, limit=60)}" ×{r.turns} {span(r.seconds)}' for r in rows[:CRONS_SHOWN])
    more = f" · {len(rows) - CRONS_SHOWN} more" if len(rows) > CRONS_SHOWN else ""
    return [f"- crons by prompt: {shown}{more}"]


def _compaction_counts(t: Totals) -> str:
    """``38 main (21 manual, 17 auto) · 14 in 13 subagents``: the main agent's first, the subagents' beside them."""
    kinds = ", ".join(
        f"{n} {kind}" for kind, n in (("manual", t.compactions_manual), ("auto", t.compactions_auto)) if n
    )
    text = f"{t.compactions} main" + (f" ({kinds})" if kinds else "")
    if t.subagent_compactions:
        text += f" · {t.subagent_compactions} in {count(t.subagents_compacted, 'subagent')}"
    return text


def _moved_note(st: SessionTime) -> str:
    """`` · ↷ N Bash calls moved to the background, ran X`` when Claude Code moved any; empty otherwise."""
    moved = [c for c in _all_calls(st) if c.auto_backgrounded]
    if not moved:
        return ""
    return f" · ↷ {count(len(moved), 'Bash call')} moved to the background, ran {span(sum(c.run_wall for c in moved))}"


def _died_context_full(sub: Subagent) -> bool:
    return bool(sub.lane.stalls) and sub.lane.stalls[-1].ended_by == "never"


def _prompt_label(turn: Turn) -> str:
    text = turn.prompt.strip()
    command = _COMMAND.search(text)
    if command is not None:
        return "/" + command.group(1).strip().lstrip("/")
    if turn.trigger in ("human", "sdk") and text:
        return '"' + redact(_TAG.sub(" ", text), limit=PROMPT_LIMIT) + '"'
    return ""


def _header(st: SessionTime, title: str | None) -> list[str]:
    t = st.totals
    top = [s for s in st.subagents if s.parent_id is None]
    foreground = sum(1 for s in top if s.shape == "foreground")
    sub_model = [s.lane.model_seconds for s in st.subagents if s.lane.model_seconds is not None]
    idle = " · ".join(f"{k} {span(v)}" for k, v in sorted(t.idle_by_trigger.items(), key=lambda kv: -kv[1]))
    busy = " · ".join(f"{k} {count(v.turns, 'turn')} {span(v.seconds)}" for k, v in t.busy_by_trigger.items())
    suspects, busy_in, active_in = _suspect_busy(st)
    api_errors = t.api_errors + sum(len(s.lane.api_errors) for s in st.subagents)
    elapsed, lane_time = subagent_active(st)
    lanes = f" (lane time {span(lane_time)})" if span(lane_time) != span(elapsed) else ""
    lines = [
        f"# Timeline {st.session_id[:8]}" + (f" — {redact(title, limit=80)}" if title else ""),
        "",
        f"{_local(st.wall.start):%Y-%m-%d %H:%M} → {_local(st.wall.end):%Y-%m-%d %H:%M}"
        f" (local time, {_zones(st.wall)}) · kind {st.kind} ({st.kind_source})",
        "",
        f"- wall {span(t.wall)} · active {span(t.active)} (main turns ∪ subagent activity)"
        f" · away {span(t.away)} (gaps over {int(AWAY.total_seconds() // 3600)} h before a human or cron prompt)",
        f"- main agent busy {span(t.main_busy)} in {count(len(st.turns), 'turn')}: model"
        f" {span(t.model) if t.model is not None else 'n/a (no api-request records)'} · tools {span(t.tools)}"
        f" · foreground subagents {span(t.foreground_subagents)} · waiting on the user {span(t.user_waits)}"
        + ("" if st.has_permission_decisions else " (dialogs only: no permission decisions recorded)")
        + f" · blocking compactions {span(t.compaction_waits)}",
        f"- busy by what opened the turn: {busy or 'none'}",
        *_cron_line(st),
        f"- idle between turns, by what ended it: {idle or 'none'}; subagents were active for"
        f" {span(t.idle_background)} of it",
        f"- subagents {len(st.subagents)} ({len(top)} top-level: {foreground} fg / {len(top) - foreground} bg;"
        f" {len(st.subagents) - len(top)} nested) · subagent active {span(elapsed)}{lanes}"
        f" · subagent model {span(sum(sub_model)) if sub_model else 'n/a'} · tokens main {tokens(st.main.tokens_total)}"
        f" / subagents {tokens(sum(s.lane.tokens_total for s in st.subagents))} (with cache reads; new"
        f" {tokens(st.main.tokens_new + sum(s.lane.tokens_new for s in st.subagents))})",
        f"- compactions {_compaction_counts(t)} · API errors {api_errors} · interruptions {t.interruptions}"
        f" · results held behind a subagent {t.batched_calls}" + _moved_note(st),
    ]
    if t.stalls or t.subagents_died_context_full:
        died = t.subagents_died_context_full
        lines.append(
            f"- ✖ stalls {t.stalls} ({span(t.stalled)}, context full: {count(t.context_full_errors, 'prompt')} refused)"
            + (f" · {count(died, 'subagent')} stopped on a full context" if died else "")
        )
    if suspects:
        lines.append(
            f"- ⌛ {suspects} calls over {int(SUSPECT_WAIT.total_seconds() // 3600)} h, suspected waits: main busy"
            f" {span(busy_in)} and subagent active {span(active_in)} are inside them"
        )
    lines += [
        "",
        "Legend: `T` turn (trigger: human/peer/handback/task-notification/cron/sdk/other) · `└ fg|bg type model [role]"
        ' span active tokens "description"` subagent launched in that turn (`?` after a guessed role) · `…` idle ·'
        " `⏸` waiting on the user inside a turn · `⌛` a call over 1 h, suspected wait · `≡` compaction (segment) ·"
        " `!` interruption or API error · `✖` stalled on a full context · `⋯` merged short turns and gaps.",
        "",
    ]
    return lines


@dataclass
class _Bucket:
    start: datetime
    end: datetime
    turns: int = 0
    triggers: Counter[str] = field(default_factory=Counter)
    busy: float = 0.0
    idle: float = 0.0
    calls: list[TimedCall] = field(default_factory=list)


@dataclass(frozen=True)
class _Item:
    time: datetime
    order: int
    kind: str
    value: Any


def _items(st: SessionTime, window: Interval | None) -> list[_Item]:
    items = [_Item(t.start, 1, "turn", t) for t in st.turns]
    items += [_Item(g.start, 0, "gap", g) for g in st.gaps]
    items += [_Item(c.time, 2, "compaction", c) for c in st.compactions if c.lane == "main"]
    items += [_Item(e.time, 2, "api-error", e) for e in st.main.api_errors if not e.context_full]
    items += [_Item(s.start, 2, "stall", s) for s in st.main.stalls]  # in place of its context-full errors
    if window is not None:
        items = [i for i in items if window.start <= i.time <= window.end]
    return sorted(items, key=lambda i: (i.time, i.order))


def _render_items(
    st: SessionTime, items: Sequence[_Item], classify: Classify, *, turn_limit: float, gap_limit: float, compact: bool
) -> list[str]:
    by_id = {s.agent_id: s for s in st.subagents}
    out: list[str] = []
    bucket: _Bucket | None = None
    day: date | None = None

    def flush() -> None:
        nonlocal bucket
        b, bucket = bucket, None
        if b is None or (b.turns == 0 and b.idle < 60) or compact:
            return
        mix = _tool_mix(b.calls, classify, 3)
        mix = f": {mix}" if mix else ""
        if b.turns == 0:
            if b.idle >= 300:
                out.append(f"{_hm(b.start)}-{_hm(b.end)} … idle {span(b.idle)}")
        elif b.turns == 1 and b.idle < 60:
            out.append(f"{_hm(b.start)} T {next(iter(b.triggers))} {span(b.busy)}{mix}")
        else:
            kinds = ", ".join(f"{k}×{v}" for k, v in b.triggers.most_common())
            idle = f", idle {span(b.idle)}" if b.idle >= 60 else ""
            out.append(
                f"{_hm(b.start)}-{_hm(b.end)} ⋯ {count(b.turns, 'turn')} ({kinds}) busy {span(b.busy)}{idle}{mix}"
            )

    def add(start: datetime, end: datetime, trigger: str | None = None, busy: float = 0.0, idle: float = 0.0,
            calls: Sequence[TimedCall] = ()) -> None:  # fmt: skip
        nonlocal bucket
        if bucket is None:
            bucket = _Bucket(start, end)
        bucket.end = max(bucket.end, end)
        if trigger is not None:
            bucket.turns += 1
            bucket.triggers[trigger] += 1
        bucket.busy += busy
        bucket.idle += idle
        bucket.calls.extend(calls)

    for item in items:
        today = _local(item.time).date()
        if today != day:
            flush()
            out.append("")
            out += _day_summary(st, today) if compact else [f"## {today:%a %Y-%m-%d}"]
            day = today
        if item.kind == "turn":
            turn: Turn = item.value
            waits = [c for c in turn.calls if c.user_wait and c.wall >= USER_WAIT_SHOWN]
            suspects = [c for c in turn.calls if suspected(c)]
            launched = sorted(
                (by_id[i] for i in turn.subagents if i in by_id), key=lambda s: s.span.start if s.span else turn.start
            )
            marked = waits or suspects or turn.interrupted
            if turn.seconds < turn_limit and not launched and not marked:
                add(turn.start, turn.end, turn.trigger, busy=turn.seconds, calls=turn.calls)
                continue
            if compact and turn.seconds < turn_limit and not marked:
                add(turn.start, turn.end, turn.trigger, busy=turn.seconds, calls=turn.calls)
                # Only the subagents that worked long enough to matter at this zoom; the day header has the rest.
                worth = [s for s in launched if s.active >= turn_limit / 2]
                out += _subagent_lines(st, worth, f"{_hm(turn.start)} {turn.trigger}→", group_over=3)
                continue
            flush()
            label = _prompt_label(turn)
            label = f" {label}" if label else ""
            note = "" if turn.ended_by_record else " (no turn-end record)"
            mix = _tool_mix(turn.calls, classify)
            out.append(
                f"{_hm(turn.start)}-{_hm(turn.end)} T {turn.trigger}{label} {span(turn.seconds)}{note}"
                + (f": {mix}" if mix else "")
            )
            for c in waits:
                decided = (c.permission.source if c.permission is not None else None) or c.denial or "answered"
                out.append(f"    ⏸ {_hm(c.start)} user {span(c.wall)} on {_call_label(c)} → {decided}")
            for c in suspects:
                out.append(f"    ⌛ {_hm(c.start)} {_call_label(c)} {span(c.wall)}, suspected wait")
            if turn.interrupted:
                out.append("    ! interrupted")
            out += _subagent_lines(st, launched)
        elif item.kind == "gap":
            g = item.value
            # A stall in the gap has a line of its own; the gap shows the rest.
            if g.unstalled_seconds < gap_limit and not g.away:
                add(g.start, g.end, idle=g.unstalled_seconds)
                continue
            flush()
            besides = " besides the stall" if g.stalled_seconds else ""
            if g.away:
                out.append(f"{_hm(g.start)}-{_hm(g.end)} … away {span(g.unstalled_seconds)}{besides} → {g.ended_by}")
            else:
                busy = f" (subagents active {span(g.background_seconds)})" if g.background_seconds > 30 else ""
                out.append(
                    f"{_hm(g.start)}-{_hm(g.end)} … idle {span(g.unstalled_seconds)}{besides} → {g.ended_by}{busy}"
                )
        elif item.kind == "compaction":
            c = item.value
            if compact and _in_background(c):
                continue  # the day header counts it
            flush()
            took = (
                f"{span(c.duration_ms / 1000)}{'' if c.wait_seconds else ' in the background'}"
                if c.duration_ms is not None
                else "?"
            )
            out.append(
                f"{_hm(c.time)} ≡ compaction {c.trigger or '?'} {took}"
                f" {tokens(c.pre_tokens or 0)}→{tokens(c.post_tokens or 0)}, segment {c.segment}"
            )
        elif item.kind == "stall":
            flush()
            out.append(_stall_line(item.value))
        else:
            flush()
            e = item.value
            out.append(f"{_hm(e.time)} {_api_error(e)}")
    flush()
    return out


def _call_label(call: TimedCall) -> str:
    command = call.input.get("command")
    if call.name == "Bash" and isinstance(command, str):
        return f"Bash `{redact(command, limit=40)}`"
    return call.name


def _subagent_lines(
    st: SessionTime, launched: Sequence[Subagent], indent: str = "    └ ", group_over: int = 4
) -> list[str]:
    """One line per subagent, or with more than ``group_over`` of them, one line per shape, type and role."""
    if len(launched) <= group_over:
        return [_subagent_line(st, s, indent) for s in launched]
    groups: dict[tuple[str, str, str], list[Subagent]] = defaultdict(list)
    for s in launched:
        groups[(_shape(s), (s.agent_type or "?").split(":")[-1], s.role)].append(s)
    lines = []
    for (shape, kind, role), group in sorted(groups.items(), key=lambda kv: -sum(s.active for s in kv[1])):
        if len(group) == 1:
            lines.append(_subagent_line(st, group[0], indent))
            continue
        active = [s.active for s in group]
        nested = sum(1 for s in st.subagents if s.parent_id in {g.agent_id for g in group})
        guessed = "?" if any(s.role_source != "prefix" for s in group) else ""
        lines.append(
            f"{indent}{len(group)}× {shape} {kind} [{role}{guessed}] {span(min(active))}–{span(max(active))}"
            f" (sum {span(sum(active))},"
            f" {tokens(sum(s.lane.tokens_total for s in group))} tok"
            + (f", +{nested} nested" if nested else "")
            + f') e.g. "{redact(group[0].description or "", limit=36)}"'
        )
    return lines


def _in_background(compaction: CompactionEvent) -> bool:
    return compaction.duration_ms is not None and compaction.wait_seconds == 0


def _day_summary(st: SessionTime, day: date) -> list[str]:
    """The header of a day in compact mode: the main agent's busy time, and the subagents launched that day, because
    compact mode prints few of them."""
    turns = [t for t in st.turns if _local(t.start).date() == day]
    kinds = ", ".join(f"{k}×{v}" for k, v in Counter(t.trigger for t in turns).most_common())
    lines = [
        f"## {day:%a %Y-%m-%d} — main busy {span(sum(t.seconds for t in turns))} in {count(len(turns), 'turn')}"
        + (f" ({kinds})" if kinds else "")
    ]
    background = [c for c in st.compactions if c.lane == "main" and _in_background(c) and _local(c.time).date() == day]
    if background:
        segments = ", ".join(str(c.segment) for c in background)
        noun = "segment" if len(background) == 1 else "segments"
        lines[0] += f" · {count(len(background), 'background compaction')} ({noun} {segments})"
    # Top-level subagents only: a parent's active time already covers its wait on the nested ones.
    day_subs = [s for s in st.subagents if s.span is not None and _local(s.span.start).date() == day]
    subs = [s for s in day_subs if s.parent_id is None]
    if not subs:
        return lines
    roles: defaultdict[str, list[Subagent]] = defaultdict(list)
    described: defaultdict[str, list[Subagent]] = defaultdict(list)
    for s in subs:
        roles[s.role].append(s)
        described[redact(normalize_description(redact(s.description or "")), limit=40)].append(s)

    def by_active(groups: dict[str, list[Subagent]]) -> list[tuple[str, list[Subagent]]]:
        return sorted(groups.items(), key=lambda kv: -sum(s.active for s in kv[1]))

    role_parts = [f"{role}×{len(g)} {span(sum(s.active for s in g))}" for role, g in by_active(roles)]
    repeated = [
        f'{len(g)}× "{text}" {span(sum(s.active for s in g))}'
        for text, g in by_active(described)
        if len(g) > 1 and text
    ][:3]
    lines.append(
        f"- {count(len(subs), 'top-level subagent')} launched"
        + (f" (+{len(day_subs) - len(subs)} nested)" if len(day_subs) > len(subs) else "")
        + f", active {span(sum(s.active for s in subs))}: "
        + " · ".join(role_parts)
        + (f"; repeated: {' · '.join(repeated)}" if repeated else "")
    )
    return lines


def render_session(
    st: SessionTime,
    title: str | None,
    grouper: BashGrouper,
    *,
    max_lines: int = 150,
    merge_gap: float = 1200.0,
    window: Interval | None = None,
) -> str:
    def classify(command: str) -> str:
        return grouper.classify(command).group

    header = _header(st, title)
    items = _items(st, window)
    if window is not None:
        full = _render_items(st, items, classify, turn_limit=0, gap_limit=1, compact=False)
        note = (
            f"(from {_local(window.start):%Y-%m-%d %H:%M} to {_local(window.end):%Y-%m-%d %H:%M}, every turn and gap)"
        )
        return "\n".join([*header, note, *full]) + "\n"
    budget = max(10, max_lines - len(header) - 1)

    # Normal mode grows the limits in big steps. When it can't fit, compact mode starts again from the base limits and
    # grows them in small steps. The step that fits is then narrowed down, so the output fills the budget instead of
    # dropping every subagent of a common length at once.
    def attempt(compact: bool, step: float) -> tuple[list[str], float, float]:
        turn_limit = min(BASE_TURN_LIMIT * 1.6**step, MAX_TURN_LIMIT)
        gap_limit = min(merge_gap * 1.5**step, max(merge_gap, MAX_GAP_LIMIT))
        lines = _render_items(st, items, classify, turn_limit=turn_limit, gap_limit=gap_limit, compact=compact)
        return lines, turn_limit, gap_limit

    for compact, steps in ((False, 4), (True, 16)):
        body, turn_limit, gap_limit = attempt(compact, 0)
        step = 0
        while len(body) > budget and step < steps:
            step += 1
            body, turn_limit, gap_limit = attempt(compact, step)
        if len(body) <= budget:
            if step > 0:
                low, high = step - 1.0, float(step)
                for _ in range(5):
                    middle = (low + high) / 2
                    found = attempt(compact, middle)
                    if len(found[0]) <= budget:
                        high, (body, turn_limit, gap_limit) = middle, found
                    else:
                        low = middle
            break
    if compact:
        note = (
            f"(compact mode: turns shorter than {span(turn_limit)} without user waits are not printed; a short turn"
            f" prints only the subagents it launched that were active {span(turn_limit / 2)} or more, as"
            f" `HH:MM trigger→subagent`; day headers carry the totals and count the compactions that ran in the"
            f" background; idle gaps shorter than {span(gap_limit)} are not printed. Use --from/--to to see a stretch"
            " in full.)"
        )
    else:
        note = (
            f"(turns shorter than {span(turn_limit)} without subagents or user waits, and idle gaps shorter than"
            f" {span(gap_limit)}, are merged into `⋯` lines. Use --from/--to to see a stretch in full.)"
        )
    if len(body) > budget:
        note += (
            f"\n(this session does not fit in {max_lines} lines even at the largest thresholds; use --from/--to to see"
            " a stretch at a time.)"
        )
    return "\n".join([*header, note, *body]) + "\n"


def render_subagent(st: SessionTime, sub: Subagent, grouper: BashGrouper, window: Interval | None = None) -> str:
    def classify(command: str) -> str:
        return grouper.classify(command).group

    calls = sorted(sub.lane.calls, key=lambda c: c.streamed_at)
    children = [s for s in st.subagents if s.parent_id == sub.agent_id]
    by_call = {s.tool_use_id: s for s in children if s.tool_use_id}
    lines = [
        f"# Subagent {sub.agent_id} of {st.session_id[:8]}",
        "",
        _subagent_line(st, sub, "- "),
        f"- model {span(sub.lane.model_seconds) if sub.lane.model_seconds is not None else 'n/a'}"
        f" · tools {span(sub.lane.tool_seconds())} · {len(calls)} calls · {sub.rounds} rounds"
        f" · tokens new {tokens(sub.lane.tokens_new)}, first request {tokens(sub.lane.first_context)}",
        "",
    ]
    for number, burst in enumerate(sub.bursts, 1):
        if window is not None and (burst.end < window.start or burst.start > window.end):
            continue
        inside = [c for c in calls if burst.start <= c.streamed_at <= burst.end]
        mix = _tool_mix(inside, classify)
        lines.append(
            f"{_hm(burst.start)}-{_hm(burst.end)} burst {number} {span(burst.seconds)}" + (f": {mix}" if mix else "")
        )
        for c in inside:
            if suspected(c):
                lines.append(f"    ⌛ {_hm(c.start)} {_call_label(c)} {span(c.wall)}, suspected wait")
            child = by_call.get(c.id) if c.id is not None and c.name in AGENT_TOOLS else None
            if child is not None:
                lines.append(_subagent_line(st, child))
    for compaction in (c for c in st.compactions if c.lane == sub.agent_id):
        took = span(compaction.duration_ms / 1000) if compaction.duration_ms is not None else "?"
        lines.append(
            f"{_hm(compaction.time)} ≡ compaction {compaction.trigger or '?'} {took}, segment {compaction.segment}"
        )
    return "\n".join(lines) + "\n"


# JSON


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _call_json(call: TimedCall, turn_of: dict[str, int], classify: Classify) -> dict[str, Any]:
    command = call.input.get("command")
    bash = command if call.name == "Bash" and isinstance(command, str) else None
    description = call.input.get("description")
    return {
        "id": call.id,
        "name": call.name,
        "lane": call.lane,
        "turn": turn_of.get(call.id or ""),
        "streamed_at": _iso(call.streamed_at),
        "start": _iso(call.start),
        "end": _iso(call.end),
        "wall": call.wall,
        "request_ref": call.request_ref,
        "permission_source": call.permission.source if call.permission is not None else None,
        "permission_decision": call.permission.decision if call.permission is not None else None,
        "denial": call.denial,
        "is_error": call.is_error,
        "batched": call.batched,
        "background": call.background,
        "auto_backgrounded": call.auto_backgrounded,
        "moved_after": call.moved_after,
        "capped": call.capped,
        "moved_reason": call.moved_reason,
        "background_task": call.background_task,
        "finished_at": _iso(call.finished_at),
        "finish_status": call.finish_status,
        "run_wall": call.run_wall,
        "user_wait": call.user_wait,
        "suspect_wait": suspected(call),
        "bash_group": classify(bash) if bash is not None else None,
        "command": redact(bash, limit=200) if bash is not None else None,
        "description": redact(description, limit=80) if isinstance(description, str) and description else None,
    }


def _subagent_fields(sub: Subagent) -> dict[str, Any]:
    return {
        "agent_id": sub.agent_id,
        "agent_type": sub.agent_type,
        "description": redact(sub.description or "", limit=80) or None,
        "shape": sub.shape,
        "parent_id": sub.parent_id,
        "depth": sub.depth,
        "tool_use_id": sub.tool_use_id,
        "turn": sub.turn,
        "role": sub.role,
        "role_source": sub.role_source,
        "step": sub.step,
        "start": _iso(sub.span.start if sub.span else None),
        "end": _iso(sub.span.end if sub.span else None),
        "active": sub.active,
        "rounds": sub.rounds,
        "bursts": [{"start": _iso(b.start), "end": _iso(b.end)} for b in sub.bursts],
        "model": sub.lane.model,
        "model_seconds": sub.lane.model_seconds,
        "requests": sub.lane.requests,
        "tokens_total": sub.lane.tokens_total,
        "tokens_new": sub.lane.tokens_new,
        "first_context": sub.lane.first_context,
        "api_errors": len(sub.lane.api_errors),
        "died_context_full": _died_context_full(sub),
    }


def session_json(st: SessionTime, grouper: BashGrouper, window: Interval | None = None) -> dict[str, Any]:
    from .bash_groups import group_label

    def classify(command: str) -> str:
        return group_label(grouper.classify(command))  # with tests split into targeted and full

    def inside(moment: datetime) -> bool:
        return window is None or window.start <= moment <= window.end

    t = st.totals
    suspects, busy_in, active_in = _suspect_busy(st)
    elapsed, lane_time = subagent_active(st)
    turn_of = {c.id: turn.index for turn in st.turns for c in turn.calls if c.id is not None}
    return {
        "session_id": st.session_id,
        "kind": st.kind,
        "kind_source": st.kind_source,
        "start": _iso(st.wall.start),
        "end": _iso(st.wall.end),
        "has_permission_decisions": st.has_permission_decisions,
        "window": {"start": _iso(window.start), "end": _iso(window.end)} if window is not None else None,
        "totals": {
            "wall": t.wall,
            "active": t.active,
            "away": t.away,
            "main_busy": t.main_busy,
            "main_idle": t.main_idle,
            "model": t.model,
            "tools": t.tools,
            "foreground_subagents": t.foreground_subagents,
            "user_waits": t.user_waits,
            "compaction_waits": t.compaction_waits,
            "idle_by_trigger": dict(t.idle_by_trigger),
            "busy_by_trigger": {k: {"turns": v.turns, "seconds": v.seconds} for k, v in t.busy_by_trigger.items()},
            "crons": [
                {"prompt": redact(r.prompt, limit=200), "turns": r.turns, "seconds": r.seconds}
                for r in crons_by_prompt(st.turns, redact)
            ],
            "idle_background": t.idle_background,
            "batched_calls": t.batched_calls,
            "interruptions": t.interruptions,
            "api_errors": t.api_errors,
            "compactions": t.compactions,
            "compactions_manual": t.compactions_manual,
            "compactions_auto": t.compactions_auto,
            "subagent_compactions": t.subagent_compactions,
            "subagents_compacted": t.subagents_compacted,
            "stalled": t.stalled,
            "stalls": t.stalls,
            "context_full_errors": t.context_full_errors,
            "subagents_died_context_full": t.subagents_died_context_full,
            "suspect_waits": suspects,
            "suspect_wait_main_busy": busy_in,
            "suspect_wait_subagent_active": active_in,
            "subagent_active": elapsed,
            "subagent_lane_time": lane_time,
        },
        "main": {
            "model": st.main.model,
            "model_seconds": st.main.model_seconds,
            "requests": st.main.requests,
            "tokens_total": st.main.tokens_total,
            "tokens_new": st.main.tokens_new,
            "first_context": st.main.first_context,
        },
        "turns": [
            {
                "index": turn.index,
                "start": _iso(turn.start),
                "end": _iso(turn.end),
                "seconds": turn.seconds,
                "trigger": turn.trigger,
                "prompt": redact(turn.prompt, limit=PROMPT_LIMIT) or None,
                "segment": turn.segment,
                "ended_by_record": turn.ended_by_record,
                "interrupted": turn.interrupted,
                "subagents": list(turn.subagents),
            }
            for turn in st.turns
            if inside(turn.start)
        ],
        "gaps": [
            {
                "start": _iso(g.start),
                "end": _iso(g.end),
                "seconds": g.seconds,
                "ended_by": g.ended_by,
                "background_seconds": g.background_seconds,
                "stalled_seconds": g.stalled_seconds,
                "away": g.away,
            }
            for g in st.gaps
            if inside(g.start)
        ],
        "calls": [_call_json(c, turn_of, classify) for c in _all_calls(st) if inside(c.streamed_at)],
        "subagents": [_subagent_fields(s) for s in st.subagents if s.span is None or inside(s.span.start)],
        "compactions": [
            {
                "time": _iso(c.time),
                "lane": c.lane,
                "trigger": c.trigger,
                "duration_ms": c.duration_ms,
                "wait_seconds": c.wait_seconds,
                "pre_tokens": c.pre_tokens,
                "post_tokens": c.post_tokens,
                "segment": c.segment,
            }
            for c in st.compactions
            if inside(c.time)
        ],
        "stalls": [
            {
                "lane": stall.lane,
                "start": _iso(stall.start),
                "end": _iso(stall.end),
                "seconds": stall.seconds,
                "refused": stall.refused,
                "ended_by": stall.ended_by,
            }
            for stall in [*st.main.stalls, *(x for s in st.subagents for x in s.lane.stalls)]
            if inside(stall.start)
        ],
        "api_errors": [
            {
                "time": _iso(e.time),
                "lane": lane,
                "error": e.error,
                "status": e.status,
                "message": redact(e.message, limit=200) if e.message else None,
            }
            for lane, lane_errors in (
                ("main", st.main.api_errors),
                *((s.agent_id, s.lane.api_errors) for s in st.subagents),
            )
            for e in lane_errors
            if inside(e.time)
        ],
    }


def subagent_json(
    st: SessionTime, sub: Subagent, grouper: BashGrouper, window: Interval | None = None
) -> dict[str, Any]:
    from .bash_groups import group_label

    def classify(command: str) -> str:
        return group_label(grouper.classify(command))  # with tests split into targeted and full

    def inside(moment: datetime) -> bool:
        return window is None or window.start <= moment <= window.end

    return {
        "session_id": st.session_id,
        "window": {"start": _iso(window.start), "end": _iso(window.end)} if window is not None else None,
        **_subagent_fields(sub),
        "tools": sub.lane.tool_seconds(),
        "calls": [_call_json(c, {}, classify) for c in sub.lane.calls if inside(c.streamed_at)],
        "children": [s.agent_id for s in st.subagents if s.parent_id == sub.agent_id],
    }
