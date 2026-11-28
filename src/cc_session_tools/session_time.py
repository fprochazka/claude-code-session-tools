"""Where a session's time went: its turns, tool calls, subagents, idle gaps, user waits and compactions.

:func:`analyze` reads a session's main transcript and its subagents' transcripts into a :class:`SessionTime`. The
renderers of ``timeline`` and ``tool-stats`` only format it.

What the transcripts record, and what that means for the numbers:

- A tool call starts when its ``tool_use`` block streams and ends when its result record is written. The calls of one
  response can overlap: Claude Code runs concurrency-safe tools (Read, Grep, Agent, …) at the same time, so a call's
  wall is the time it took, not time no other call took. Totals over calls are interval unions.
- Bash, Edit, Write and NotebookEdit run one after another, but every ``tool_use`` carries the time its block streamed,
  so a call of one of them starts no earlier than the previous one of the same response ended.
- When a response holds a foreground ``Agent`` call, the results of its other calls can be held back until the
  subagent finishes. Such a call's own runtime is unknown; its wall is counted as 0 and it is flagged ``batched``.
- Records are written late at times, so the analysis sorts by timestamp. Compaction segments are counted in file order.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, TypeGuard

from cc_blackbox import (
    AssistantEntry,
    AttachmentEntry,
    Entry,
    SessionTranscript,
    SubagentTranscript,
    SystemEntry,
    TextBlock,
    ToolCall,
    ToolPermissionDecision,
    ToolResultBlock,
    ToolUseBlock,
    Usage,
    UserEntry,
    tool_calls,
)

from cc_session_tools.segments import compaction_segments

BURST_GAP = timedelta(minutes=5)
"""A pause in a subagent's records longer than this splits it into bursts of activity."""
AWAY = timedelta(hours=2)
"""An idle gap longer than this, ended by a human or cron prompt, counts as away rather than waiting."""
STALE_TURN = timedelta(minutes=2)
"""A turn without a ``turn_duration`` record ends at its last activity once a new prompt or command comes this much
later."""
PAUSE = timedelta(minutes=15)
"""A turn with no call running ends at its last activity once a queued prompt or model activity comes this much later.
Shorter pauses happen inside a turn: a long response writes nothing until it has streamed."""
BATCHED_TOLERANCE = timedelta(seconds=5)
"""A result written this close to an Agent call's result of the same response may have been held back behind it."""
BATCHED_AGENT = timedelta(seconds=30)
"""An Agent call, foreground or a slow background launch, that took longer than this can hold back other results."""
BATCHED_HELD = timedelta(seconds=10)
"""A call shorter than this that ended next to an Agent call's result ran on its own and is not batched."""
COMPACTION_WAIT_MS = 1000
MOVE_REASONS = ("cap", "timeout", "deliver_message", "user", "other")
"""Why a Bash call was moved to the background: the 180 s cap on a subagent's call, the call's own timeout, so that a
message could reach the agent, the user, or a kind of move this version doesn't know."""
NOTICE_SLACK = timedelta(seconds=5)
"""How much earlier than a call's result a completion notice matched by task id only may be written."""
"""A compaction that took at least this long blocked the session; faster ones ran in the background."""

SERIAL_TOOLS = frozenset({"Bash", "Edit", "Write", "MultiEdit", "NotebookEdit", "PowerShell"})
"""Tools that are not concurrency-safe: Claude Code runs the ones of one response one after another."""
AGENT_TOOLS = frozenset({"Agent", "Task"})
DIALOG_TOOLS = frozenset({"AskUserQuestion", "ExitPlanMode"})
HANDBACK_TOOL = "SubagentHandback"
"""The tool a subagent reports with. It is bookkeeping, not work, so it is left out of tool time."""
USER_DECISIONS = frozenset({"user_temporary", "user_permanent", "user_reject"})
"""``permissionDecision.source`` values of a prompt the user answered."""

TRIGGERS = ("human", "peer", "handback", "task-notification", "cron", "sdk", "system", "other")
"""What opens a turn. ``handback`` is a peer message from one of the session's own subagents; ``other`` is model
activity that no recorded prompt opened, such as the continuation after a compaction."""

_COMMAND_PREFIXES = ("<command-name>", "<command-message>", "<local-command", "<bash-input>", "<bash-stdout>")
"""How the record of a slash command or a ``!`` shell command starts."""

_ROLE_PREFIX = re.compile(r"^\s*(impl|validate|run|explore)\s*:\s*(\S+)?", re.IGNORECASE)
_ROLE_NAMES = {"impl": "implement", "validate": "validate", "run": "run", "explore": "explore"}
_ROLE_TYPES = (("runner", "run"), ("watch", "watch"), ("explore", "explore"))
"""Agent type fragments that settle the role before the description is read."""
_ROLE_NOUNS = {
    **dict.fromkeys(("implementer", "actor", "fixer", "writer"), "implement"),
    **dict.fromkeys(("validator", "reviewer", "verifier", "checker", "auditor"), "validate"),
    **dict.fromkeys(("runner",), "run"),
    **dict.fromkeys(("explorer", "researcher", "investigator", "scout"), "explore"),
    **dict.fromkeys(("watcher", "babysitter", "monitor"), "watch"),
}
"""Words that name the agent itself. They win over the verbs, so "MR actor: fixes and reviews" implements. An actor of
an MR babysit triages, fixes and posts, so it implements; its watcher only reports, so it watches."""
_ROLE_VERBS = {
    **dict.fromkeys(("watch", "babysit", "monitor"), "watch"),
    **dict.fromkeys(
        ("validate", "validation", "review", "reviews", "verify", "recheck", "audit", "audits"), "validate"
    ),
    **dict.fromkeys(
        ("explore", "exploration", "research", "investigate", "investigation", "design", "survey", "find"), "explore"
    ),
    **dict.fromkeys(("test", "tests", "run", "lint", "build", "check", "checks"), "run"),
    **dict.fromkeys(
        (
            "implement",
            "implementation",
            "fix",
            "fixes",
            "apply",
            "write",
            "add",
            "refactor",
            "port",
            "restack",
            "rebase",
            "update",
            "make",
            "migrate",
            "rename",
            "extract",
            "create",
            "remove",
            "replace",
            "move",
            "wire",
            "upgrade",
            "bump",
            "support",
            "address",
            "resolve",
        ),  # fmt: skip
        "implement",
    ),
}
"""Whole words of a description and the role they hint at. The first one in the description wins."""
_WEAK_EXPLORE = frozenset(("read", "list", "search", "map", "compare", "mine", "probe", "dive", "assess"))
"""Words that mean exploring only as the description's first word ("Read the ticket", "Deep dive: …"); later they are
mostly objects ("Update the search index")."""
_RE_VERBS = frozenset(("validate", "review", "verify", "test", "run", "check", "write", "build", "base", "stack"))
"""Verbs a leading ``re``/``re-`` repeats: ``re-validate`` validates, ``retest`` runs, while ``report`` is no port."""
_WORD = re.compile(r"[a-z]+")
_VIA_TOOLS = re.compile(r"\bcheck\b.*\bvia\b")
""""Check X via tools" looks something up; a plain "check" runs the checks."""


@dataclass(frozen=True)
class Interval:
    start: datetime
    end: datetime

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


@dataclass
class TimedCall:
    """One tool call with the time it took."""

    id: str | None
    name: str
    lane: str
    """``main`` or the agent id of the subagent that made the call."""
    input: Mapping[str, Any]
    streamed_at: datetime
    """When the ``tool_use`` block streamed."""
    start: datetime
    """When the call could start: :attr:`streamed_at`, or later for a serial call behind another one."""
    end: datetime | None
    """When the result was written; ``None`` for a call without a result."""
    request_ref: str | None
    permission: ToolPermissionDecision | None
    denial: str | None
    is_error: bool
    result_meta: Any
    batched: bool = False
    """The result was held back until an Agent call of the same response returned; :attr:`wall` is 0."""
    background_hint: bool | None = None
    """For an Agent call whose result names no status: whether the subagent's ``.meta.json`` says it ran in the
    background."""
    finished_at: datetime | None = None
    """For a Bash call that ran as a background task: when its completion notice says it stopped."""
    finish_status: str | None = None
    """The ``<status>`` of that notice, such as ``completed`` or ``failed``."""

    @property
    def wall(self) -> float:
        """How long the call blocked its lane: until its result. For a Bash call moved to the background, the move."""
        if self.end is None or self.batched:
            return 0.0
        return max(0.0, (self.end - self.start).total_seconds())

    @property
    def background_task(self) -> str | None:
        """The id of the background task a Bash call ran as, launched so or moved there by Claude Code."""
        if self.name != "Bash" or not isinstance(self.result_meta, Mapping):
            return None
        task = self.result_meta.get("backgroundTaskId")
        return task if isinstance(task, str) and task else None

    @property
    def moved_reason(self) -> str | None:
        """Why a Bash call that was not launched with ``run_in_background`` runs as a background task: one of
        :data:`MOVE_REASONS`, ``other`` for a kind of move this version doesn't know. ``None`` for any other call."""
        if self.background_task is None or self.input.get("run_in_background") is True:
            return None
        meta = self.result_meta
        if meta.get("subagentCapApplied") is True:
            return "cap"
        if self.moved_after is not None:
            return "timeout"
        if meta.get("backgroundedToDeliverMessage") is True:
            return "deliver_message"
        return "user" if meta.get("backgroundedByUser") is True else "other"

    @property
    def auto_backgrounded(self) -> bool:
        """The Bash call was moved to the background, by Claude Code or the user; it was not launched there."""
        return self.moved_reason is not None

    @property
    def moved_after(self) -> float | None:
        """For a Bash call moved to the background when it hit its timeout or the subagent cap: the seconds it had run
        then. ``None`` for any other call."""
        if self.background_task is None or self.input.get("run_in_background") is True:
            return None
        ms = self.result_meta.get("timedOutAfterMs")
        return ms / 1000 if isinstance(ms, int | float) and not isinstance(ms, bool) else None

    @property
    def capped(self) -> bool:
        """The move was the 180 s cap on a subagent's Bash call, not the call's own timeout."""
        return self.moved_reason == "cap"

    @property
    def run_wall(self) -> float:
        """How long the command ran: for a Bash call that ran as a background task, until its completion notice;
        otherwise :attr:`wall`."""
        if self.finished_at is None or self.batched:
            return self.wall
        return max(self.wall, (self.finished_at - self.start).total_seconds())

    @property
    def background(self) -> bool:
        """For an Agent call, whether the subagent ran in the background: from the result's ``status``, else from the
        subagent's ``.meta.json``, else from the ``run_in_background`` input. For a Bash call, from the input."""
        status = _launched_in_background(self.result_meta)
        if status is not None:
            return status
        if self.background_hint is not None:
            return self.background_hint
        return self.input.get("run_in_background") is True

    @property
    def user_wait(self) -> bool:
        """The user answered a permission prompt for the call, or the call is a dialog such as AskUserQuestion."""
        source = self.permission.source if self.permission is not None else None
        return source in USER_DECISIONS or self.name in DIALOG_TOOLS


@dataclass(frozen=True)
class ApiError:
    time: datetime
    error: str | None
    status: Any
    message: str | None = None
    """The error's text as Claude Code showed it, such as ``Prompt is too long``: its first text block."""

    @property
    def context_full(self) -> bool:
        """The API refused the request because the context is full: ``Prompt is too long``."""
        return self.error == "invalid_request" and (self.message or "").startswith("Prompt is too long")


@dataclass(frozen=True)
class TaskNotice:
    """A ``<task-notification>``: a background agent or command stopped."""

    time: datetime
    task_id: str | None
    tool_use_id: str | None
    status: str | None


_NOTICE_FIELD = re.compile(r"<(task-id|tool-use-id|status)>\s*([^<]*?)\s*</\1>")


def task_notice(time: datetime, text: str) -> TaskNotice | None:
    """The notice in ``text``, or ``None`` when it is no task notification."""
    if "<task-notification" not in text:
        return None
    found = {name: value or None for name, value in _NOTICE_FIELD.findall(text)}
    return TaskNotice(time, found.get("task-id"), found.get("tool-use-id"), found.get("status"))


@dataclass(frozen=True)
class Stall:
    """A stretch in which a lane's context was full and the API refused every prompt.

    It starts at the first ``Prompt is too long`` error after the lane's last model activity, so a cron that keeps
    firing into the full context extends one stall. It ends at the lane's next model activity or compaction,
    whichever comes first, or at its last record when neither came."""

    lane: str
    start: datetime
    end: datetime
    refused: int
    """How many requests the API refused in it."""
    ended_by: str
    """``compaction``, ``activity``, or ``never`` for a lane that never recovered, such as a subagent that died."""

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


@dataclass
class Lane:
    """The calls, model time and tokens of the main agent or of one subagent."""

    calls: list[TimedCall] = field(default_factory=list)
    model_seconds: float | None = None
    """Time from each API request to its last response block, summed; ``None`` without ``api-request`` records."""
    requests: int = 0
    tokens_total: int = 0
    """Input, cache creation, cache reads and output of every response, counted once per message."""
    tokens_new: int = 0
    """Input, cache creation and output, without cache reads."""
    first_context: int = 0
    """The context of the first request: input, cache creation and cache reads."""
    model: str | None = None
    api_errors: list[ApiError] = field(default_factory=list)
    stalls: list[Stall] = field(default_factory=list)
    task_notices: list[TaskNotice] = field(default_factory=list)
    """The completion notices of background tasks that reached the lane, whichever lane started the task."""

    def tool_seconds(self) -> float:
        """The union of the lane's tool calls, without Agent calls, user waits, batched calls and SubagentHandback."""
        return union(
            Interval(c.start, c.end)
            for c in self.calls
            if c.end is not None
            and not c.batched
            and not c.user_wait
            and c.name not in AGENT_TOOLS
            and c.name != HANDBACK_TOOL
        )


@dataclass
class Subagent:
    agent_id: str
    agent_type: str | None
    description: str | None
    shape: str | None
    """``foreground`` or ``background``, from the ``.meta.json``; ``None`` for older subagents."""
    parent_id: str | None
    """The agent id of the subagent that launched it; ``None`` when the main agent did."""
    depth: int
    tool_use_id: str | None
    role: str
    """``implement``, ``validate``, ``run``, ``explore``, ``watch`` or ``other``."""
    role_source: str
    """``prefix`` when the description starts with ``impl:``, ``validate:``, ``run:`` or ``explore:``; ``guess`` when
    the role was guessed from keywords."""
    step: str | None
    """The step id after the description prefix."""
    bursts: list[Interval]
    """When it was active: its records split at pauses over :data:`BURST_GAP`, joined with its own tool calls, so a
    subagent that waits on a 10-minute test is active."""
    rounds: int
    """1, plus one for every message from its parent that resumed it."""
    lane: Lane
    compactions: list[CompactionEvent] = field(default_factory=list)
    turn: int | None = None
    """The index of the main agent's turn that launched it, or launched its top-level ancestor."""

    @property
    def span(self) -> Interval | None:
        return Interval(self.bursts[0].start, self.bursts[-1].end) if self.bursts else None

    @property
    def active(self) -> float:
        return sum(b.seconds for b in self.bursts)


@dataclass
class Turn:
    index: int
    start: datetime
    end: datetime
    trigger: str
    """See :data:`TRIGGERS`."""
    prompt: str
    """The opening prompt's text, untrimmed; renderers shorten and redact it."""
    segment: int
    """The compaction segment of the opening record, in file order: 0 before the first compaction."""
    ended_by_record: bool = False
    """The turn ended at a ``turn_duration`` record, not at its last activity."""
    interrupted: bool = False
    calls: list[TimedCall] = field(default_factory=list)
    subagents: list[str] = field(default_factory=list)
    """The agent ids of the subagents the turn launched."""

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())


@dataclass(frozen=True)
class Gap:
    """The idle time between two turns of the main agent."""

    start: datetime
    end: datetime
    ended_by: str
    """The trigger of the turn that ended it."""
    background_seconds: float
    """How much of the gap subagents were active."""
    away: bool
    """Over :data:`AWAY` without its stalls, before a human or cron prompt."""
    stalled_seconds: float = 0.0
    """How much of the gap the main agent was stalled on a full context, see :class:`Stall`."""

    @property
    def seconds(self) -> float:
        return max(0.0, (self.end - self.start).total_seconds())

    @property
    def unstalled_seconds(self) -> float:
        """The gap without its stalls: the time that is idle, or away."""
        return max(0.0, self.seconds - self.stalled_seconds)


@dataclass(frozen=True)
class CompactionEvent:
    time: datetime
    trigger: str | None
    duration_ms: int | None
    pre_tokens: int | None
    post_tokens: int | None
    segment: int
    """The segment the compaction opens."""
    lane: str = "main"

    @property
    def wait_seconds(self) -> float:
        """The time the compaction blocked the session; 0 for one that ran in the background."""
        if self.duration_ms is None or self.duration_ms < COMPACTION_WAIT_MS:
            return 0.0
        return self.duration_ms / 1000


@dataclass(frozen=True)
class TriggerBusy:
    """The main agent's turns that one trigger opened, or one cron prompt, and their busy time."""

    turns: int
    seconds: float


@dataclass(frozen=True)
class CronPrompt:
    """The turns that the crons with one prompt opened."""

    prompt: str
    """The prompt's first line, with runs of digits as ``N``, so the fires of one cron share a row."""
    turns: int
    seconds: float


_DIGIT_RUN = re.compile(r"\d+")
_MARKUP = re.compile(r"<[^>]+>")
_MONTH = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?"
    r"|nov(?:ember)?|dec(?:ember)?)\.?"
)
_TRAILING_TIME = re.compile(
    r"[\s,;@(\[-]*(?<!\w)(?:"
    r"\d{4}-\d{2}-\d{2}(?:[ T]\d{1,2}:\d{2}(?::\d{2})?)?"  # 2026-10-29, 2026-10-29T13:17
    r"|\d{1,2}:\d{2}(?::\d{2})?"  # 13:17, 13:17:05
    rf"|\d{{1,2}}\.?\s*{_MONTH}(?:\s+\d{{4}})?"  # 29 Oct, 29. October 2026
    rf"|{_MONTH}\s*\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?"  # Oct 29, October 29th, 2026
    r"|\d{1,2}\.\d{1,2}\.\d{4}"  # 29.10.2026; not 29.10. without a year, which is also a version: Python 3.12.
    r")[)\]]?\s*$",
    re.IGNORECASE,
)


def normalize_description(text: str) -> str:
    """The form that repeats of a description, or of a cron prompt, share: without a trailing clock time or date
    (``Indexing pass 13:17``, ``Indexing pass 29 Oct 13:17`` and ``Indexing pass`` are one), runs of digits as ``N``,
    whitespace collapsed. Redact the text first: replacing digits breaks the shapes redaction looks for."""
    text = " ".join(text.split())
    for _ in range(2):  # a date and then a time: `29 Oct 13:17`
        text = _TRAILING_TIME.sub("", text)
    return _DIGIT_RUN.sub("N", text).strip()


def cron_key(prompt: str, clean: Callable[[str], str] | None = None) -> str:
    """The row a cron prompt counts in: its first non-empty line, without markup, passed through ``clean`` (such as
    a redaction), then by :func:`normalize_description`."""
    line = next((line.strip() for line in _MARKUP.sub(" ", prompt).splitlines() if line.strip()), "")
    return normalize_description(clean(line) if clean is not None else line)


def crons_by_prompt(turns: Iterable[Turn], clean: Callable[[str], str] | None = None) -> list[CronPrompt]:
    """The main agent's cron turns grouped by :func:`cron_key`, most busy first."""
    rows: dict[str, list[float]] = {}
    for turn in turns:
        if turn.trigger == "cron":
            rows.setdefault(cron_key(turn.prompt, clean), []).append(turn.seconds)
    found = [CronPrompt(key, len(seconds), sum(seconds)) for key, seconds in rows.items()]
    return sorted(found, key=lambda row: (-row.seconds, -row.turns))


@dataclass(frozen=True)
class Totals:
    wall: float
    """First to last user, assistant or subagent record, or turn end. Bookkeeping that lands days later is left out."""
    active: float
    """The union of the main agent's turns and the subagents' bursts."""
    away: float
    main_busy: float
    main_idle: float
    """``wall - main_busy``: the gaps between turns, plus the time before the first and after the last."""
    model: float | None
    tools: float
    """The union of the main agent's tool calls, without Agent calls, user waits and SubagentHandback."""
    foreground_subagents: float
    user_waits: float
    compaction_waits: float
    """The main agent's compactions that blocked it; a subagent's compaction blocks only the subagent."""
    idle_by_trigger: Mapping[str, float]
    """Idle gaps that are not away, by what ended them."""
    idle_background: float
    """How much of those gaps subagents were active."""
    batched_calls: int
    """Calls of the main agent and the subagents whose result was held back behind a foreground subagent."""
    interruptions: int
    api_errors: int
    stalled: float = 0.0
    """The main agent's stalls on a full context, outside its turns. Neither away nor idle by trigger."""
    stalls: int = 0
    """How many stalls the main agent had."""
    context_full_errors: int = 0
    """``Prompt is too long`` errors of the main agent and the subagents."""
    subagents_died_context_full: int = 0
    """Subagents whose last stall never ended: they stopped on a full context."""
    compactions: int = 0
    """The main agent's compactions: they reset the session's own context. A subagent's are counted apart."""
    compactions_manual: int = 0
    compactions_auto: int = 0
    subagent_compactions: int = 0
    """The subagents' compactions, never added to :attr:`compactions`."""
    subagents_compacted: int = 0
    """How many subagents compacted at least once."""
    busy_by_trigger: Mapping[str, TriggerBusy] = field(default_factory=dict)
    """The main agent's turns and busy time by what opened them, most busy first."""


@dataclass
class SessionTime:
    session_id: str
    wall: Interval
    kind: str
    kind_source: str
    """``start-log`` for the role of the auth-switch start log, ``origins`` for a guess from what opened the turns."""
    main: Lane
    turns: list[Turn]
    gaps: list[Gap]
    subagents: list[Subagent]
    compactions: list[CompactionEvent]
    """The main agent's compactions, then each subagent's, with the lane they happened in."""
    totals: Totals
    has_permission_decisions: bool
    """``False`` for transcripts older than ``permissionDecision``; user waits then count dialogs only."""


def analyze(transcript: SessionTranscript) -> SessionTime:
    entries = list(transcript.entries())  # file order, for the compaction segments
    subagents = [_subagent(sub) for sub in transcript.subagents()]
    own_agents = {s.agent_id for s in subagents}
    main = _lane(entries, "main")

    _link_subagents(subagents, [main, *(s.lane for s in subagents)])
    _finish_background_bash([main, *(s.lane for s in subagents)])

    segment_of = _segment_index(entries)
    steps = sort_steps(
        step for index, entry in enumerate(entries) if (step := step_of(index, entry, own_agents, segment_of[index]))
    )
    turns, interruptions = turns_of(steps)
    _attach_calls(turns, main.calls, subagents)

    activity = [b for s in subagents for b in s.bursts]
    wall = wall_of(steps, turns, activity) or Interval(transcript.modified_at, transcript.modified_at)

    gaps = _gaps(turns, subagents, main.stalls)
    compaction_events = _compactions(entries, segment_of, "main")
    for sub in subagents:
        compaction_events += sub.compactions
    roles = [start.role for start in transcript.profile_starts()]
    kind, kind_source = session_kind(roles, Counter(t.trigger for t in turns))
    totals = _totals(wall, main, turns, gaps, subagents, compaction_events, interruptions)
    return SessionTime(
        session_id=transcript.session_id,
        wall=wall,
        kind=kind,
        kind_source=kind_source,
        main=main,
        turns=turns,
        gaps=gaps,
        subagents=subagents,
        compactions=compaction_events,
        totals=totals,
        has_permission_decisions=any(c.permission is not None for c in main.calls),
    )


def only_subagents(st: SessionTime, description: re.Pattern[str]) -> SessionTime:
    """``st`` with only the top-level subagents whose description ``description`` matches, and their nested
    subagents; the main agent's turns, gaps and calls are left out. The totals are computed again over what is left,
    so the main agent's busy time is 0 and the active time is the kept subagents'. The wall stays the session's."""
    by_id = {s.agent_id: s for s in st.subagents}

    def top_of(sub: Subagent) -> Subagent:
        seen = {sub.agent_id}
        while sub.parent_id is not None and sub.parent_id in by_id and sub.parent_id not in seen:
            sub = by_id[sub.parent_id]
            seen.add(sub.agent_id)
        return sub

    kept = [s for s in st.subagents if description.search(top_of(s).description or "")]
    lanes = {s.agent_id for s in kept}
    compactions = [c for c in st.compactions if c.lane in lanes]
    main = Lane()
    wall = Interval(st.wall.start, st.wall.end)
    totals = _totals(wall, main, [], [], kept, compactions, 0)
    return replace(st, main=main, turns=[], gaps=[], subagents=kept, compactions=compactions, totals=totals)


def session_kind(roles: Sequence[str | None], triggers: Mapping[str, int]) -> tuple[str, str]:
    """The kind of a session and where it came from.

    The role of the newest auth-switch start that names one (``worker``, ``dispatcher``, …) comes first. Without one, a
    session whose turns were mostly opened by crons, peers and its subagents, with under 10 % opened by a human, is a
    ``loop``; any other is ``interactive``.
    """
    for role in reversed(roles):
        if role:
            return role, "start-log"
    total = sum(triggers.values())
    machine = sum(triggers.get(t, 0) for t in ("cron", "peer", "handback", "task-notification"))
    if total and triggers.get("human", 0) < 0.1 * total and machine > total / 2:
        return "loop", "origins"
    return "interactive", "origins"


def wall_of(steps: Sequence[Step], turns: Sequence[Turn], bursts: Sequence[Interval]) -> Interval | None:
    """From the first to the last user or assistant record, subagent burst, or turn start and end; ``None`` without
    any. A turn ends at its ``turn_duration`` record, and may start at a queued prompt or a cron fire."""
    moments = [s.time for s in steps if s.record]
    moments += [b.start for b in bursts] + [b.end for b in bursts]
    moments += [t.start for t in turns] + [t.end for t in turns]
    return Interval(min(moments), max(moments)) if moments else None


def active_of(turns: Sequence[Turn], bursts: Sequence[Interval]) -> float:
    """The session's active time: the union of the main agent's turns and the subagents' bursts."""
    return union([Interval(t.start, t.end) for t in turns] + list(bursts))


def union(intervals: Iterable[Interval]) -> float:
    """The seconds covered by ``intervals``, overlaps counted once."""
    total = 0.0
    current: Interval | None = None
    for interval in sorted((i for i in intervals if i.end > i.start), key=lambda i: i.start):
        if current is None or interval.start > current.end:
            if current is not None:
                total += current.seconds
            current = interval
        elif interval.end > current.end:
            current = Interval(current.start, interval.end)
    return total + (current.seconds if current is not None else 0.0)


def merge(intervals: Iterable[Interval]) -> list[Interval]:
    """``intervals`` with the overlapping and touching ones joined, sorted."""
    merged: list[Interval] = []
    for interval in sorted(intervals, key=lambda i: i.start):
        if merged and interval.start <= merged[-1].end:
            if interval.end > merged[-1].end:
                merged[-1] = Interval(merged[-1].start, interval.end)
        else:
            merged.append(interval)
    return merged


def clip(intervals: Iterable[Interval], window: Interval) -> list[Interval]:
    return [
        Interval(max(i.start, window.start), min(i.end, window.end))
        for i in intervals
        if i.end > window.start and i.start < window.end
    ]


def bursts(times: Iterable[datetime], gap: timedelta = BURST_GAP) -> list[Interval]:
    """Split activity at pauses longer than ``gap``."""
    ordered = sorted(times)
    if not ordered:
        return []
    found: list[Interval] = []
    start = previous = ordered[0]
    for moment in ordered[1:]:
        if moment - previous > gap:
            found.append(Interval(start, previous))
            start = moment
        previous = moment
    found.append(Interval(start, previous))
    return found


def role_of(description: str | None, agent_type: str | None) -> tuple[str, str, str | None]:
    """The role, where it came from (``prefix`` or ``guess``) and the step id of a subagent.

    Without a prefix the role is guessed, in this order: from the agent type (``*runner*`` runs, ``Explore`` explores,
    ``*watch*`` watches), then from a word that names the agent (``implementer``, ``actor``, ``validator``, …), then
    from the first hinting verb of the description. Words match whole, so ``checkpoint`` is no check and ``backtest``
    no test.
    """
    text = description or ""
    match = _ROLE_PREFIX.match(text)
    if match is not None:
        return _ROLE_NAMES[match.group(1).lower()], "prefix", match.group(2)
    kind = (agent_type or "").lower()
    for fragment, role in _ROLE_TYPES:
        if fragment in kind:
            return role, "guess", None
    lowered = re.sub(r"\bre-(?=[a-z])", "re", text.lower())
    if _VIA_TOOLS.search(lowered):
        return "explore", "guess", None
    words = [
        w[2:] if w not in _ROLE_VERBS and w.startswith("re") and w[2:] in _RE_VERBS else w
        for w in _WORD.findall(lowered)
    ]
    for word in words:
        if word in _ROLE_NOUNS:
            return _ROLE_NOUNS[word], "guess", None
    if words[:2] == ["deep", "dive"] or (words and words[0] in _WEAK_EXPLORE):
        return "explore", "guess", None
    for word in words:
        if word in _ROLE_VERBS:
            return _ROLE_VERBS[word], "guess", None
    return "other", "guess", None


# Lanes


def _lane(entries: Sequence[Entry], lane_id: str) -> Lane:
    lane = Lane()
    request_starts: dict[str, datetime] = {}
    request_ends: dict[str, datetime] = {}
    usages: dict[str, tuple[datetime | None, Usage]] = {}
    message_of: dict[str, str] = {}  # tool_use id -> message id, for calls without a requestRef
    stall: list[Any] | None = None  # [start, refused] of the open stall
    last_record: datetime | None = None

    def end_stall(end: datetime, ended_by: str) -> None:
        nonlocal stall
        if stall is not None:
            lane.stalls.append(Stall(lane_id, stall[0], max(end, stall[0]), stall[1], ended_by))
            stall = None

    for entry in entries:
        if isinstance(entry, UserEntry | AssistantEntry) and entry.timestamp is not None:
            last_record = max(last_record or entry.timestamp, entry.timestamp)
        text = _notice_text(entry)
        notice = task_notice(entry.timestamp, text) if text is not None and entry.timestamp is not None else None
        if notice is not None:
            lane.task_notices.append(notice)
        if isinstance(entry, SystemEntry) and entry.subtype == "compact_boundary" and entry.timestamp is not None:
            end_stall(entry.timestamp, "compaction")
        if entry.type == "api-request":
            request_id = entry.raw.get("id")
            if isinstance(request_id, str) and entry.timestamp is not None:
                request_starts[request_id] = entry.timestamp
        elif isinstance(entry, AssistantEntry):
            if entry.raw.get("isApiErrorMessage") is True:
                if entry.timestamp is not None:
                    message = next((b.text for b in entry.content if isinstance(b, TextBlock) and b.text), None)
                    error, status = _text(entry.raw.get("error")), entry.raw.get("apiErrorStatus")
                    api_error = ApiError(entry.timestamp, error, status, message)
                    lane.api_errors.append(api_error)
                    if api_error.context_full:
                        if stall is None:
                            stall = [entry.timestamp, 0]
                        stall[1] += 1
                continue
            if entry.message_id is not None:
                for block in entry.content:
                    if isinstance(block, ToolUseBlock) and block.id is not None:
                        message_of[block.id] = entry.message_id
            if entry.model is None or entry.model == "<synthetic>":
                continue
            if entry.timestamp is not None:
                end_stall(entry.timestamp, "activity")
            lane.model = lane.model or entry.model
            ref = entry.raw.get("requestRef")
            if isinstance(ref, str) and entry.timestamp is not None:
                request_ends[ref] = max(request_ends.get(ref, entry.timestamp), entry.timestamp)
            key = entry.message_id or (ref if isinstance(ref, str) else None)
            if key is not None and entry.usage is not None:
                # Every block of a response repeats its usage; a later block has the final output count.
                first_seen = usages[key][0] if key in usages else entry.timestamp
                usages[key] = (first_seen, entry.usage)
    if stall is not None:
        end_stall(last_record or stall[0], "never")
    if request_starts:
        lane.model_seconds = sum(
            max(0.0, (end - request_starts[ref]).total_seconds())
            for ref, end in request_ends.items()
            if ref in request_starts
        )
    lane.requests = len(usages)
    ordered = sorted(usages.values(), key=lambda item: item[0].timestamp() if item[0] is not None else float("inf"))
    for number, (_t, usage) in enumerate(ordered):
        fresh = usage.input_tokens + usage.cache_creation_input_tokens
        lane.tokens_new += fresh + usage.output_tokens
        lane.tokens_total += fresh + usage.cache_read_input_tokens + usage.output_tokens
        if number == 0:
            lane.first_context = fresh + usage.cache_read_input_tokens
    lane.calls = _timed_calls(tool_calls(entries), lane_id, message_of)
    return lane


def _timed_calls(calls: Iterable[ToolCall], lane_id: str, message_of: Mapping[str, str]) -> list[TimedCall]:
    timed = [
        TimedCall(
            id=call.id,
            name=call.name or "?",
            lane=lane_id,
            input=call.input,
            streamed_at=call.timestamp,
            start=call.timestamp,
            end=call.result_timestamp,
            request_ref=call.request_ref,
            permission=call.permission_decision,
            denial=call.denial,
            is_error=call.result.is_error if call.result is not None else False,
            result_meta=call.result_meta,
        )
        for call in calls
        if call.timestamp is not None
    ]
    timed.sort(key=lambda c: c.streamed_at)
    by_request: dict[str, list[TimedCall]] = {}
    for call in timed:
        key = call.request_ref or (message_of.get(call.id) if call.id is not None else None)
        if key is not None:
            by_request.setdefault(key, []).append(call)
    for group in by_request.values():
        _mark_batched(group)
        _chain_serial(group)
    return timed


def _mark_batched(group: Sequence[TimedCall]) -> None:
    """Flag the calls whose result was held back until a long Agent call of the same response returned: a foreground
    one, or a background launch that was slow. Such a call ended next to the Agent call and took over 10 s itself."""
    agents = [c for c in group if c.name in AGENT_TOOLS and c.end is not None and c.end - c.streamed_at > BATCHED_AGENT]
    for call in group:
        if call.name in AGENT_TOOLS or call.end is None or call.end - call.streamed_at <= BATCHED_HELD:
            continue
        if any(agent.end is not None and abs(agent.end - call.end) <= BATCHED_TOLERANCE for agent in agents):
            call.batched = True


def _chain_serial(group: Sequence[TimedCall]) -> None:
    """Start each serial call no earlier than the previous serial call of the response ended, and never after its own
    result."""
    previous_end: datetime | None = None
    for call in group:
        if call.name not in SERIAL_TOOLS or call.batched:
            continue
        if previous_end is not None and previous_end > call.start:
            call.start = min(previous_end, call.end) if call.end is not None else previous_end
        if call.end is not None:
            previous_end = max(previous_end, call.end) if previous_end is not None else call.end


# Subagents


def _subagent(sub: SubagentTranscript) -> Subagent:
    entries = list(sub.entries())
    lane = _lane(entries, sub.agent_id)
    resumes = sum(1 for e in entries if _resumed_by_parent(e))
    if resumes == 0:  # older versions wrote the parent's message as a plain prompt
        resumes = max(0, sum(1 for e in entries if _plain_prompt(e)) - 1)
    role, role_source, step = role_of(sub.description, sub.agent_type)
    shape = sub.meta.get("requestShape")
    return Subagent(
        agent_id=sub.agent_id,
        agent_type=sub.agent_type,
        description=sub.description,
        shape=shape if isinstance(shape, str) else None,
        parent_id=sub.parent_agent_id,
        depth=sub.spawn_depth or 1,
        tool_use_id=sub.tool_use_id,
        role=role,
        role_source=role_source,
        step=step,
        bursts=activity_bursts(entries),
        rounds=1 + resumes,
        lane=lane,
        compactions=_compactions(entries, _segment_index(entries), sub.agent_id),
    )


def activity_bursts(entries: Iterable[Entry]) -> list[Interval]:
    """When a subagent was active: its user and assistant records and its parent's resumes, split at pauses over
    :data:`BURST_GAP`, joined with its finished tool calls other than SubagentHandback.

    A call runs from its ``tool_use`` to its first result, as cc-blackbox's ``tool_calls`` pairs them. Serial calls of
    one response are not chained here: the chained intervals of :func:`_chain_serial` cover the same time. Reads the
    records once, so a caller can stream them.
    """
    times: list[datetime] = []
    uses: list[tuple[str, datetime]] = []
    results: dict[str, datetime | None] = {}
    for entry in entries:
        if isinstance(entry, UserEntry):
            for block in entry.content:
                if isinstance(block, ToolResultBlock) and block.tool_use_id is not None:
                    results.setdefault(block.tool_use_id, entry.timestamp)
        elif isinstance(entry, AssistantEntry) and entry.timestamp is not None:
            uses += [
                (b.id, entry.timestamp)
                for b in entry.content
                if isinstance(b, ToolUseBlock) and b.id is not None and b.name != HANDBACK_TOOL
            ]
        if entry.timestamp is not None and (isinstance(entry, UserEntry | AssistantEntry) or _resumed_by_parent(entry)):
            times.append(entry.timestamp)
    calls = [Interval(start, end) for call_id, start in uses if (end := results.get(call_id)) is not None]
    return merge(bursts(times) + calls)


def _resumed_by_parent(entry: Entry) -> bool:
    """A message from the parent that resumed the subagent: a ``queued_command`` from its coordinator."""
    if not isinstance(entry, AttachmentEntry) or entry.attachment_type != "queued_command":
        return False
    origin = entry.attachment.get("origin")
    return isinstance(origin, Mapping) and origin.get("kind") == "coordinator"


def _plain_prompt(entry: Entry) -> bool:
    """A prompt with no origin: the task, and in older versions every message that resumed the subagent. Hand-backs
    and notices from its own children are not prompts."""
    if not isinstance(entry, UserEntry) or entry.is_compact_summary or entry.is_meta:
        return False
    if entry.origin is not None or entry.turn_origin is not None:
        return False
    if any(isinstance(block, ToolResultBlock) for block in entry.content):
        return False
    text = entry.plain_text.lstrip()
    return bool(text) and not text.startswith(
        (
            "<task-notification",
            "[Subagent hand-back]",
            "[Request interrupted",
            "<cross-session-message",
            "<agent-message",
        )
    )


def _notice_text(entry: Entry) -> str | None:
    """The text of a record that can carry a task notification: a queued command, or a user prompt."""
    if isinstance(entry, AttachmentEntry):
        if entry.attachment_type == "queued_command":
            prompt = entry.attachment.get("prompt")
            return prompt if isinstance(prompt, str) else None
        return None
    if isinstance(entry, UserEntry) and not any(isinstance(b, ToolResultBlock) for b in entry.content):
        text = entry.plain_text
        return text if "<task-notification" in text else None
    return None


def _finish_background_bash(lanes: Sequence[Lane]) -> None:
    """Give each Bash call that ran as a background task the time and status of its completion notice. A notice
    names the call by its tool use id, or only by its task id, and can reach another lane than the call's.

    A notice can be written a few milliseconds before the result that reports the move, so one matched by its tool
    use id is always the call's. One matched by task id only must come after the result, with
    :data:`NOTICE_SLACK` to spare, so a reused task id can't shorten a later call."""
    by_call: dict[str, TaskNotice] = {}
    by_task: dict[str, TaskNotice] = {}
    for lane in lanes:
        for notice in lane.task_notices:
            if notice.tool_use_id is not None:
                by_call.setdefault(notice.tool_use_id, notice)
            if notice.task_id is not None:
                by_task.setdefault(notice.task_id, notice)
    for lane in lanes:
        for call in lane.calls:
            task = call.background_task
            if task is None:
                continue
            found = by_call.get(call.id or "")
            if found is None:
                by_id = by_task.get(task)
                stale = by_id is not None and call.end is not None and by_id.time < call.end - NOTICE_SLACK
                found = None if stale else by_id
            if found is not None:
                call.finished_at, call.finish_status = found.time, found.status


def _launched_in_background(result_meta: Any) -> bool | None:
    """From an Agent result: ``True`` for a background launch, ``False`` for a finished foreground run, or ``None``."""
    if not isinstance(result_meta, Mapping):
        return None
    if result_meta.get("status") == "async_launched" or result_meta.get("isAsync") is True:
        return True
    return False if result_meta.get("status") == "completed" else None


def _link_subagents(subagents: Sequence[Subagent], lanes: Sequence[Lane]) -> None:
    """Join each subagent with the Agent call that launched it, also when it has no ``.meta.json``, and settle its
    shape: the meta's ``requestShape``, else the result's status."""
    calls = [c for lane in lanes for c in lane.calls if c.name in AGENT_TOOLS]
    by_id = {c.id: c for c in calls if c.id is not None}
    by_agent = {}
    for c in calls:
        agent_id = c.result_meta.get("agentId") if isinstance(c.result_meta, Mapping) else None
        if isinstance(agent_id, str):
            by_agent[agent_id] = c
    depth_of = {s.agent_id: s.depth for s in subagents}
    for sub in subagents:
        call = by_id.get(sub.tool_use_id) if sub.tool_use_id is not None else None
        if call is None and (call := by_agent.get(sub.agent_id)) is not None:
            sub.tool_use_id = call.id
            if call.lane != "main" and sub.parent_id is None:
                sub.parent_id = call.lane
                sub.depth = depth_of.get(call.lane, 1) + 1
        if call is None:
            continue
        if sub.shape is None and (status := _launched_in_background(call.result_meta)) is not None:
            sub.shape = "background" if status else "foreground"
        if sub.shape is not None:
            call.background_hint = sub.shape == "background"


# Turns


def _segment_index(entries: Sequence[Entry]) -> list[int]:
    index = [0] * len(entries)
    for number, segment in enumerate(compaction_segments(entries)):
        for position in range(segment.start, segment.end):
            index[position] = number
    return index


def _opener(entry: Entry, own_agents: set[str]) -> str | None:
    """The trigger of a record that opens a turn, or ``None``."""
    if isinstance(entry, SystemEntry):
        return "cron" if entry.subtype == "scheduled_task_fire" else None
    if isinstance(entry, AttachmentEntry):
        if entry.attachment_type != "queued_command" or entry.attachment.get("commandMode") not in (
            "prompt",
            "task-notification",
        ):
            return None
        origin = entry.attachment.get("origin")
        kind = origin.get("kind") if isinstance(origin, Mapping) else None
        sender = origin.get("from") if isinstance(origin, Mapping) else None
        if entry.attachment.get("commandMode") == "task-notification":
            kind = kind or "task-notification"
        return _trigger(kind if isinstance(kind, str) else "human", sender, own_agents, False)
    if not isinstance(entry, UserEntry) or entry.is_compact_summary:
        return None
    if any(isinstance(block, ToolResultBlock) for block in entry.content):
        return None
    text = entry.plain_text.lstrip()
    if not text or text.startswith("[Request interrupted"):
        return None
    origin = entry.raw.get("origin")
    sender = origin.get("from") if isinstance(origin, Mapping) else None
    handback = isinstance(origin, Mapping) and origin.get("handback") is True
    kind = entry.turn_origin or entry.origin
    if kind is None:
        if entry.is_meta:
            return None
        if text.startswith("<task-notification"):
            return "task-notification"
        if text.startswith(("<cross-session-message", "<agent-message")):
            return "peer"
        return "human"
    return _trigger(kind, sender, own_agents, handback)


def _trigger(kind: str, sender: Any, own_agents: set[str], handback: bool) -> str:
    if kind == "peer":
        return "handback" if handback or sender in own_agents else "peer"
    if kind in ("task-notification", "task_notification"):
        return "task-notification"
    if kind == "scheduled":
        return "cron"
    return kind if kind in TRIGGERS else "other"


def _is_activity(entry: Entry) -> bool:
    """Model output or a tool result. Local commands, notices, bookkeeping and the ``<synthetic>`` messages Claude Code
    writes itself (API errors, "No response requested.") are not activity."""
    if isinstance(entry, AssistantEntry):
        return entry.model != "<synthetic>"
    return isinstance(entry, UserEntry) and any(isinstance(b, ToolResultBlock) for b in entry.content)


def _is_command(entry: Entry) -> TypeGuard[UserEntry]:
    """A slash command or ``!`` shell command typed in the prompt, without the ``origin`` of a prompt that starts a
    model turn. A local command such as ``/model`` or ``/compact`` starts none; in older versions, a command that did
    looks the same, which :func:`_turns` tells apart by the model activity that follows."""
    if not isinstance(entry, UserEntry) or entry.origin is not None or entry.turn_origin is not None:
        return False
    if entry.is_compact_summary or any(isinstance(block, ToolResultBlock) for block in entry.content):
        return False
    return entry.plain_text.lstrip().startswith(_COMMAND_PREFIXES)


@dataclass(frozen=True, slots=True)
class Step:
    """What one record of the main transcript means for its turns, as :func:`turns_of` reads it.

    :func:`analyze` and the cheap scan of ``search --min-active`` both build their steps with :func:`step_of`, so the
    turns, and the active time, follow the same rules in both.
    """

    time: datetime
    index: int
    """The record's position in the file, which orders records with the same timestamp."""
    segment: int
    """The compaction segment of the record, in file order."""
    record: bool
    """A user or assistant record, which bounds the session's wall time."""
    interrupt: bool
    command: bool
    """A slash command or ``!`` shell command, see :func:`_is_command`."""
    opener: str | None
    """The trigger when the record opens a turn, see :func:`_opener`."""
    queued: bool
    """An attachment: a prompt queued while the session may be busy."""
    user: bool
    text: str
    """The text of a command or of a prompt that opens a turn; empty otherwise."""
    activity: bool
    """Model output or a tool result, see :func:`_is_activity`."""
    turn_end: bool
    """A ``turn_duration`` record."""
    uses: tuple[str, ...] = ()
    """The ids of the tool calls an assistant record makes."""
    results: tuple[str, ...] = ()
    """The ids of the tool calls a user record returns results for."""


def step_of(index: int, entry: Entry, own_agents: set[str], segment: int = 0) -> Step | None:
    """The step of a record, or ``None`` for one without a timestamp or one the turns ignore."""
    moment = entry.timestamp
    if moment is None:
        return None
    user = isinstance(entry, UserEntry)
    interrupt = isinstance(entry, UserEntry) and entry.plain_text.lstrip().startswith("[Request interrupted")
    command = _is_command(entry)
    opener = None if command else _opener(entry, own_agents)
    activity = not command and _is_activity(entry)
    turn_end = isinstance(entry, SystemEntry) and entry.subtype == "turn_duration"
    record = isinstance(entry, UserEntry | AssistantEntry)
    if not (record or opener is not None or turn_end):
        return None
    text = ""
    if command or (opener is not None and user):
        text = entry.plain_text if isinstance(entry, UserEntry) else ""
    elif opener is not None:
        text = _queued_text(entry)
    uses: tuple[str, ...] = ()
    results: tuple[str, ...] = ()
    if activity and isinstance(entry, AssistantEntry):
        uses = tuple(b.id for b in entry.content if isinstance(b, ToolUseBlock) and b.id is not None)
    elif activity and isinstance(entry, UserEntry):
        results = tuple(
            b.tool_use_id for b in entry.content if isinstance(b, ToolResultBlock) and b.tool_use_id is not None
        )
    return Step(
        time=moment,
        index=index,
        segment=segment,
        record=record,
        interrupt=interrupt,
        command=command,
        opener=opener,
        queued=isinstance(entry, AttachmentEntry),
        user=user,
        text=text,
        activity=activity,
        turn_end=turn_end,
        uses=uses,
        results=results,
    )


def sort_steps(steps: Iterable[Step]) -> list[Step]:
    """``steps`` by time, records with the same timestamp in file order."""
    return sorted(steps, key=lambda s: (s.time, s.index))


def turns_of(steps: Sequence[Step]) -> tuple[list[Turn], int]:
    """The main agent's turns and the number of interruptions, from its steps sorted by :func:`sort_steps`.

    A turn opens at a prompt, a queued prompt or a cron fire, and ends at its ``turn_duration`` record. A turn without
    one ends at its last activity when a prompt or command comes over :data:`STALE_TURN` later, or a queued prompt or
    model activity over :data:`PAUSE` later while none of its tool calls is running. A turn whose call runs for days
    stays one turn: the transcript can't tell it apart from a turn that never ended.

    A command never opens or extends a turn: model activity within :data:`STALE_TURN` of one opens a ``human`` turn at
    the command, later activity an ``other`` turn. Model activity with no open turn also opens an ``other`` turn.
    """
    turns: list[Turn] = []
    current: Turn | None = None
    active_since: datetime | None = None  # the last activity of the current turn; None while it has none
    command: tuple[datetime, int, str] | None = None  # the last command while no turn is open, with its segment
    running: set[str] = set()  # the current turn's tool calls without a result yet
    interruptions = 0

    def close(end: datetime, *, by_record: bool = False) -> None:
        """End the current turn at ``end``; a turn without activity is dropped."""
        nonlocal current, active_since
        if current is not None and active_since is not None:
            current.end = end
            current.ended_by_record = by_record
            current.index = len(turns)
            turns.append(current)
        current, active_since = None, None
        running.clear()

    def quiet(moment: datetime, limit: timedelta, *, idle: bool) -> bool:
        """The current turn has had no activity for over ``limit``; with ``idle``, also no call running."""
        if current is None or (idle and running):
            return False
        return moment - (active_since or current.start) > limit

    for step in steps:
        moment = step.time
        if step.interrupt:
            interruptions += 1
            running.clear()
            if current is not None:
                current.interrupted = True
        if step.command:
            if quiet(moment, STALE_TURN, idle=True):
                close(active_since or moment)
            if current is None:
                command = (moment, step.segment, step.text)
            continue
        trigger = step.opener
        if trigger is not None and current is not None and step.queued:
            if quiet(moment, PAUSE, idle=True):
                close(active_since or moment)
            else:
                trigger = None  # a prompt queued while the session was busy: the open turn takes it in
        if trigger is not None:
            command = None
            if quiet(moment, STALE_TURN, idle=False):
                close(active_since or moment)  # the turn ended without a turn_duration record
        if trigger is not None and current is None:
            current = Turn(len(turns), moment, moment, trigger, step.text, step.segment)
        elif trigger is not None and current is not None and active_since is None and current.trigger == "cron":
            current.prompt = current.prompt or (step.text if step.user else "")
        if step.activity:
            if quiet(moment, PAUSE, idle=True):
                close(active_since or moment)
            if current is None:
                if command is not None and moment - command[0] <= STALE_TURN:
                    current = Turn(len(turns), command[0], command[0], "human", command[2], command[1])
                else:
                    current = Turn(len(turns), moment, moment, "other", "", step.segment)
                command = None
            if moment >= current.start:
                active_since = moment
            running.update(step.uses)
            running.difference_update(step.results)
        if step.turn_end and current is not None:
            if quiet(moment, PAUSE, idle=True):
                close(active_since or moment)  # a late record of a turn that went quiet long before
            else:
                close(moment, by_record=True)
    if current is not None and active_since is not None:
        close(active_since)
    return turns, interruptions


def _queued_text(entry: Entry) -> str:
    if isinstance(entry, AttachmentEntry):
        prompt = entry.attachment.get("prompt")
        return prompt if isinstance(prompt, str) else ""
    if isinstance(entry, SystemEntry):
        prompt = entry.raw.get("prompt")
        return prompt if isinstance(prompt, str) else ""
    return ""


def _attach_calls(turns: Sequence[Turn], calls: Sequence[TimedCall], subagents: Sequence[Subagent]) -> None:
    by_tool_use = {s.tool_use_id: s for s in subagents if s.tool_use_id}
    position = 0
    for call in calls:  # sorted by streamed_at, as the turns are by start
        while position < len(turns) and turns[position].end + timedelta(seconds=1) < call.streamed_at:
            position += 1
        if position == len(turns):
            break
        turn = turns[position]
        if call.streamed_at < turn.start:
            continue
        turn.calls.append(call)
        sub = by_tool_use.get(call.id) if call.id is not None and call.name in AGENT_TOOLS else None
        if sub is not None:
            turn.subagents.append(sub.agent_id)
            sub.turn = turn.index
    by_id = {s.agent_id: s for s in subagents}
    for sub in subagents:
        ancestor: Subagent | None = sub
        seen: set[str] = set()
        while ancestor is not None and ancestor.turn is None and ancestor.agent_id not in seen:
            seen.add(ancestor.agent_id)
            ancestor = by_id.get(ancestor.parent_id) if ancestor.parent_id is not None else None
        sub.turn = ancestor.turn if ancestor is not None else None


def _gaps(turns: Sequence[Turn], subagents: Sequence[Subagent], stalls: Sequence[Stall] = ()) -> list[Gap]:
    """The gaps between the main agent's turns. Its ``stalls`` are cut out of the time that can be away."""
    activity = [b for s in subagents for b in s.bursts]
    stalled = [Interval(s.start, s.end) for s in stalls]
    gaps = []
    for previous, following in zip(turns, turns[1:], strict=False):
        window = Interval(previous.end, following.start)
        if window.seconds <= 0:
            continue
        stalled_seconds = union(clip(stalled, window))
        away = window.seconds - stalled_seconds > AWAY.total_seconds() and following.trigger in ("human", "cron")
        background = union(clip(activity, window))
        gaps.append(Gap(window.start, window.end, following.trigger, background, away, stalled_seconds))
    return gaps


def _compactions(entries: Sequence[Entry], segment_of: Sequence[int], lane: str) -> list[CompactionEvent]:
    found = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, SystemEntry) or entry.subtype != "compact_boundary" or entry.timestamp is None:
            continue
        meta = entry.raw.get("compactMetadata")
        meta = meta if isinstance(meta, Mapping) else {}
        found.append(
            CompactionEvent(
                time=entry.timestamp,
                trigger=_text(meta.get("trigger")),
                duration_ms=_int(meta.get("durationMs")),
                pre_tokens=_int(meta.get("preTokens")),
                post_tokens=_int(meta.get("postTokens")),
                segment=segment_of[index],
                lane=lane,
            )
        )
    return found


def _totals(
    wall: Interval,
    main: Lane,
    turns: Sequence[Turn],
    gaps: Sequence[Gap],
    subagents: Sequence[Subagent],
    compaction_events: Sequence[CompactionEvent],
    interruptions: int,
) -> Totals:
    main_busy = sum(t.seconds for t in turns)
    by_trigger: dict[str, list[float]] = {}
    for turn in turns:
        by_trigger.setdefault(turn.trigger, []).append(turn.seconds)
    busy_by_trigger = {
        trigger: TriggerBusy(len(seconds), sum(seconds))
        for trigger, seconds in sorted(by_trigger.items(), key=lambda kv: (-sum(kv[1]), -len(kv[1])))
    }
    idle_by_trigger: dict[str, float] = {}
    idle_background = 0.0
    away = 0.0
    for gap in gaps:
        if gap.away:
            away += gap.unstalled_seconds
        else:
            idle_by_trigger[gap.ended_by] = idle_by_trigger.get(gap.ended_by, 0.0) + gap.unstalled_seconds
            idle_background += gap.background_seconds
    main_compactions = [c for c in compaction_events if c.lane == "main"]
    busy = [Interval(t.start, t.end) for t in turns]
    stalled = union(busy + [Interval(s.start, s.end) for s in main.stalls]) - union(busy)
    lanes = [main, *(s.lane for s in subagents)]
    finished = [c for c in main.calls if c.end is not None and not c.batched]

    def spans(calls: Iterable[TimedCall]) -> list[Interval]:
        return [Interval(c.start, c.end) for c in calls if c.end is not None]

    foreground = [c for c in finished if c.name in AGENT_TOOLS and not c.background]
    return Totals(
        wall=wall.seconds,
        active=active_of(turns, [b for s in subagents for b in s.bursts]),
        away=away,
        main_busy=main_busy,
        main_idle=wall.seconds - main_busy,
        model=main.model_seconds,
        tools=main.tool_seconds(),
        foreground_subagents=union(spans(foreground)),
        user_waits=union(Interval(c.start, c.end) for c in main.calls if c.user_wait and c.end is not None),
        compaction_waits=sum(c.wait_seconds for c in compaction_events if c.lane == "main"),
        idle_by_trigger=idle_by_trigger,
        idle_background=idle_background,
        batched_calls=sum(1 for lane in lanes for c in lane.calls if c.batched),
        interruptions=interruptions,
        api_errors=len(main.api_errors),
        stalled=stalled,
        stalls=len(main.stalls),
        context_full_errors=sum(1 for lane in lanes for e in lane.api_errors if e.context_full),
        subagents_died_context_full=sum(
            1 for s in subagents if s.lane.stalls and s.lane.stalls[-1].ended_by == "never"
        ),
        compactions=len(main_compactions),
        compactions_manual=sum(1 for c in main_compactions if c.trigger == "manual"),
        compactions_auto=sum(1 for c in main_compactions if c.trigger == "auto"),
        subagent_compactions=len(compaction_events) - len(main_compactions),
        subagents_compacted=sum(1 for s in subagents if s.compactions),
        busy_by_trigger=busy_by_trigger,
    )


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
