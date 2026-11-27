"""Condense a session's conversation into markdown.

cc-blackbox's ``conversation()`` extractor supplies the signal: prompts, assistant prose and subagent runs, without
tool-call noise. This module decides how each looks and what is left out.

A compacted session is rendered in parts, one per segment of ``segments.compaction_segments``: the main file holds the
context the model works with since the last compaction, and each earlier part sits next to it as
``<session-id>.pre-compact-<k>.md``, linked by a pointer line at the top and the bottom of each file.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path

from cc_blackbox import (
    AssistantEntry,
    AssistantText,
    Compaction,
    Entry,
    Prompt,
    ReadDiagnostics,
    SessionTranscript,
    ShellCommandPart,
    SlashCommandPart,
    SubagentRun,
    TextPart,
    Thinking,
    ToolUseBlock,
    conversation,
)

from .segments import compaction_segments

# Who sent a prompt decides its heading, so a recap never passes off another session's message as the user's.
_PROMPT_HEADINGS = {
    "human": "👤 User",
    "peer": "📨 Peer session",
    "task-notification": "🔔 Task notification",
}


@dataclass(frozen=True)
class RenderedPrompt:
    prompt: Prompt
    text: str


Item = RenderedPrompt | AssistantText | Thinking | SubagentRun


def _prompt_count(items: Sequence[Item]) -> int:
    return sum(1 for item in items if isinstance(item, RenderedPrompt))


def _subagent_count(items: Sequence[Item]) -> int:
    return sum(1 for item in items if isinstance(item, SubagentRun))


@dataclass
class DistilledPart:
    """The conversation between two compactions: what the model worked with after the compaction that opened it."""

    number: int
    """1-based; the last part is the main file."""
    items: list[Item]
    compaction: Compaction | None
    """The compaction that opened this part; ``None`` for the first part."""
    started_at: str | None
    ended_at: str | None

    @property
    def prompt_count(self) -> int:
        return _prompt_count(self.items)

    @property
    def subagent_count(self) -> int:
        return _subagent_count(self.items)


@dataclass
class DistilledSession:
    session_id: str
    cwd: str | None
    started_at: str | None
    ended_at: str | None
    items: list[Item]
    """Every item of the session, across all parts."""
    diagnostics: ReadDiagnostics
    parts: list[DistilledPart]
    """One part for a session without compactions, N+1 for N compactions."""

    @property
    def prompt_count(self) -> int:
        return _prompt_count(self.items)

    @property
    def subagent_count(self) -> int:
        return _subagent_count(self.items)

    @property
    def main_part(self) -> DistilledPart:
        return self.parts[-1]

    def part_file_name(self, part: DistilledPart) -> str:
        """``<session-id>.md`` for the main part, ``<session-id>.pre-compact-<k>.md`` for an earlier one."""
        if part.number == len(self.parts):
            return f"{self.session_id}.md"
        return f"{self.session_id}.pre-compact-{part.number}.md"


def render_prompt_text(prompt: Prompt) -> str | None:
    """Render a prompt's markup compactly; ``None`` when nothing meaningful survives.

    Slash and shell commands become one-line notes so the prompt stays readable; prose is kept as it is.
    """
    rendered: list[str] = []
    for part in prompt.parts:
        if isinstance(part, TextPart):
            rendered.append(part.text)
        elif isinstance(part, SlashCommandPart):
            rendered.append(f"_(ran {part.name} {part.args})_" if part.args else f"_(ran {part.name})_")
        elif isinstance(part, ShellCommandPart) and part.command:
            # An output-only part (empty command) carries no prompt signal here.
            rendered.append(f"_(ran shell: `{part.command}`)_")
    text = "\n\n".join(s for s in rendered if s.strip())
    return text or None


def distill_entries(
    session_id: str,
    entries: Iterable[Entry],
    diagnostics: ReadDiagnostics,
    *,
    include_thinking: bool = False,
    session: SessionTranscript | None = None,
) -> DistilledSession:
    materialized = list(entries)
    timestamps = [t for e in materialized if isinstance(t := e.raw.get("timestamp"), str)]
    cwd = next((c for e in materialized if isinstance(c := e.raw.get("cwd"), str)), None)
    locate = _ItemLocator(materialized)
    items: list[Item] = []
    positions: list[int] = []
    # conversation() runs over the whole transcript so that a subagent spawned before a compaction keeps the result
    # that arrives after it. Each item is then placed by the position of the record it came from.
    for found in conversation(materialized, include_thinking=include_thinking, session=session):
        position = locate(found)
        if isinstance(found, Prompt):
            text = render_prompt_text(found)
            if text is None:
                continue
            items.append(RenderedPrompt(found, text))
        else:
            items.append(found)
        positions.append(position)

    segments = compaction_segments(materialized)
    starts = [segment.start for segment in segments]
    grouped: list[list[Item]] = [[] for _ in segments]
    for item, position in zip(items, positions, strict=True):
        grouped[bisect.bisect_right(starts, position) - 1].append(item)
    parts: list[DistilledPart] = []
    for number, (segment, part_items) in enumerate(zip(segments, grouped, strict=True), start=1):
        stamps = [t for e in materialized[segment.start : segment.end] if isinstance(t := e.raw.get("timestamp"), str)]
        parts.append(
            DistilledPart(
                number=number,
                items=part_items,
                compaction=segment.compaction,
                started_at=stamps[0] if stamps else None,
                ended_at=stamps[-1] if stamps else None,
            )
        )
    return DistilledSession(
        session_id=session_id,
        cwd=cwd,
        started_at=timestamps[0] if timestamps else None,
        ended_at=timestamps[-1] if timestamps else None,
        items=items,
        diagnostics=diagnostics,
        parts=parts,
    )


class _ItemLocator:
    """Find the index of the record a ``conversation()`` item came from.

    A prompt carries its entry. Assistant text and thinking carry the record's uuid, and a subagent run the id of the
    tool call that spawned it. Items come in file order, so an item that cannot be placed takes the position of the
    item before it, and a position never moves backwards.
    """

    def __init__(self, entries: Sequence[Entry]) -> None:
        self._by_entry = {id(entry): index for index, entry in enumerate(entries)}
        self._by_uuid: dict[str, int] = {}
        self._by_tool_use: dict[str, int] = {}
        for index, entry in enumerate(entries):
            if entry.uuid is not None:
                self._by_uuid.setdefault(entry.uuid, index)
            if isinstance(entry, AssistantEntry):
                for block in entry.content:
                    if isinstance(block, ToolUseBlock) and block.id is not None:
                        self._by_tool_use.setdefault(block.id, index)
        self._last = 0

    def __call__(self, item: Prompt | AssistantText | Thinking | SubagentRun) -> int:
        found: int | None
        if isinstance(item, Prompt):
            found = self._by_entry.get(id(item.entry))
        elif isinstance(item, SubagentRun):
            found = self._by_tool_use.get(item.tool_use_id) if item.tool_use_id is not None else None
        else:
            found = self._by_uuid.get(item.uuid) if item.uuid is not None else None
        if found is not None and found > self._last:
            self._last = found
        return self._last


def _fmt_ts_range(start: str | None, end: str | None) -> str:
    if start and end:
        return f"{start} → {end}"
    return start or end or "unknown"


def _prompt_heading(origin: str) -> str:
    return _PROMPT_HEADINGS.get(origin, f"📨 Message from {origin}")


def _compaction_details(compaction: Compaction) -> str:
    bits: list[str] = []
    if compaction.trigger:
        bits.append(compaction.trigger)
    if compaction.timestamp is not None:
        bits.append(compaction.timestamp.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC"))
    if compaction.pre_tokens is not None:
        tokens = compaction.pre_tokens
        bits.append(f"{round(tokens / 1000)}k tokens before it" if tokens >= 1000 else f"{tokens} tokens before it")
    return f" ({', '.join(bits)})" if bits else ""


def render_markdown(
    session: DistilledSession, part: DistilledPart | None = None, *, output_dir: Path | None = None
) -> str:
    """Render one part of the session, the main part by default.

    The pointer lines between parts name the other part files inside ``output_dir``, or by their bare file names when
    it is ``None``. A session without compactions renders as one file with no pointer lines.
    """
    part = part if part is not None else session.main_part
    total = len(session.parts)

    def path_of(number: int) -> str:
        name = session.part_file_name(session.parts[number - 1])
        return str(output_dir / name) if output_dir is not None else name

    lines: list[str] = []
    lines.append(f"# Session {session.session_id}")
    if part.compaction is not None:
        lines.append(
            f"> Compacted session: this part starts at compaction {part.number - 1} of {total - 1}"
            f"{_compaction_details(part.compaction)}. The conversation before it is distilled in"
            f" {path_of(part.number - 1)}."
        )
        lines.append("")
    elif total > 1:
        lines.append(f"> Compacted session: this part is the conversation before compaction 1 of {total - 1}.")
        lines.append("")

    meta_bits = [f"Project: {session.cwd or 'unknown'}"]
    meta_bits.append(f"{part.prompt_count} prompts")
    meta_bits.append(f"{part.subagent_count} subagents")
    meta_bits.append(_fmt_ts_range(part.started_at, part.ended_at))
    lines.append(f"_{'  ·  '.join(meta_bits)}_")
    lines.append("")

    if part.compaction is not None:
        lines.append("## 📦 Compaction summary")
        summary = part.compaction.summary
        lines.append(
            summary.rstrip() if summary and summary.strip() else "_(the summary is missing from the transcript)_"
        )
        lines.append("")

    for item in part.items:
        if isinstance(item, RenderedPrompt):
            lines.append(f"## {_prompt_heading(item.prompt.origin)}")
            lines.append(item.text.rstrip())
            lines.append("")
        elif isinstance(item, AssistantText):
            lines.append("## 🤖 Assistant")
            lines.append(item.text.rstrip())
            lines.append("")
        elif isinstance(item, Thinking):
            lines.append("## 🤖 Assistant")
            lines.append("_thinking:_")
            lines.append("")
            # Quote the thinking so it is visually distinct from real responses.
            for raw in item.text.rstrip().split("\n"):
                lines.append(f"> {raw}" if raw else ">")
            lines.append("")
        elif isinstance(item, SubagentRun):
            header = "### ⤷ Subagent"
            if item.description:
                header += f" · {item.description}"
            if item.subagent_type:
                header += f" ({item.subagent_type})"
            lines.append(header)
            lines.append("")
            lines.append("**Prompt:**")
            lines.append("")
            lines.append((item.prompt or "_(no prompt recorded)_").rstrip())
            lines.append("")
            lines.append("**Result:**")
            lines.append("")
            if item.result:
                lines.append(item.result.rstrip())
            else:
                # The final response is not in the main transcript, for example for a background subagent.
                lines.append("_(no final response in main session file)_")
            lines.append("")

    if part.number < total:
        lines.append(f"> Continued after compaction {part.number} in {path_of(part.number + 1)}.")
        lines.append("")

    unknown = session.diagnostics.unknown_types
    if unknown and part.number == total:
        listed = ", ".join(f"{name}×{count}" for name, count in sorted(unknown.items()))
        lines.append("---")
        lines.append(f"_Diagnostics: unknown entry types encountered: {listed}_")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
