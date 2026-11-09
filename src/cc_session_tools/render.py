"""Condense a session's conversation into markdown.

cc-blackbox's ``conversation()`` extractor supplies the signal: prompts, assistant prose and subagent runs, without
tool-call noise. This module decides how each looks and what is left out.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from cc_blackbox import (
    AssistantText,
    Entry,
    Prompt,
    ReadDiagnostics,
    SessionTranscript,
    ShellCommandPart,
    SlashCommandPart,
    SubagentRun,
    TextPart,
    Thinking,
    conversation,
)

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


@dataclass
class DistilledSession:
    session_id: str
    cwd: str | None
    started_at: str | None
    ended_at: str | None
    items: list[Item]
    diagnostics: ReadDiagnostics

    @property
    def prompt_count(self) -> int:
        return sum(1 for item in self.items if isinstance(item, RenderedPrompt))

    @property
    def subagent_count(self) -> int:
        return sum(1 for item in self.items if isinstance(item, SubagentRun))


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
    items: list[Item] = []
    for item in conversation(materialized, include_thinking=include_thinking, session=session):
        if isinstance(item, Prompt):
            text = render_prompt_text(item)
            if text is not None:
                items.append(RenderedPrompt(item, text))
        else:
            items.append(item)
    return DistilledSession(
        session_id=session_id,
        cwd=cwd,
        started_at=timestamps[0] if timestamps else None,
        ended_at=timestamps[-1] if timestamps else None,
        items=items,
        diagnostics=diagnostics,
    )


def _fmt_ts_range(start: str | None, end: str | None) -> str:
    if start and end:
        return f"{start} → {end}"
    return start or end or "unknown"


def _prompt_heading(origin: str) -> str:
    return _PROMPT_HEADINGS.get(origin, f"📨 Message from {origin}")


def render_markdown(session: DistilledSession) -> str:
    lines: list[str] = []
    lines.append(f"# Session {session.session_id}")

    meta_bits = [f"Project: {session.cwd or 'unknown'}"]
    meta_bits.append(f"{session.prompt_count} prompts")
    meta_bits.append(f"{session.subagent_count} subagents")
    meta_bits.append(_fmt_ts_range(session.started_at, session.ended_at))
    lines.append(f"_{'  ·  '.join(meta_bits)}_")
    lines.append("")

    for item in session.items:
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

    unknown = session.diagnostics.unknown_types
    if unknown:
        listed = ", ".join(f"{name}×{count}" for name, count in sorted(unknown.items()))
        lines.append("---")
        lines.append(f"_Diagnostics: unknown entry types encountered: {listed}_")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
