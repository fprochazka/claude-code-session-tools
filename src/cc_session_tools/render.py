"""Render a classified session into condensed markdown."""

from __future__ import annotations

from .parse import (
    AssistantText,
    HumanPrompt,
    ParsedSession,
    Subagent,
    Thinking,
)


def _fmt_ts_range(start: str | None, end: str | None) -> str:
    if start and end:
        return f"{start} → {end}"
    return start or end or "unknown"


def render_markdown(session: ParsedSession) -> str:
    lines: list[str] = []
    lines.append(f"# Session {session.session_id}")

    meta_bits = [f"Project: {session.cwd or 'unknown'}"]
    meta_bits.append(f"{session.prompt_count} prompts")
    meta_bits.append(f"{session.subagent_count} subagents")
    meta_bits.append(_fmt_ts_range(session.started_at, session.ended_at))
    lines.append(f"_{'  ·  '.join(meta_bits)}_")
    lines.append("")

    for event in session.events:
        if isinstance(event, HumanPrompt):
            lines.append("## 👤 User")
            lines.append(event.text.rstrip())
            lines.append("")
        elif isinstance(event, AssistantText):
            lines.append("## 🤖 Assistant")
            lines.append(event.text.rstrip())
            lines.append("")
        elif isinstance(event, Thinking):
            lines.append("## 🤖 Assistant")
            lines.append("_thinking:_")
            lines.append("")
            # Quote the thinking so it is visually distinct from real responses.
            for raw in event.text.rstrip().split("\n"):
                lines.append(f"> {raw}" if raw else ">")
            lines.append("")
        elif isinstance(event, Subagent):
            header = "### ⤷ Subagent"
            if event.description:
                header += f" · {event.description}"
            if event.subagent_type:
                header += f" ({event.subagent_type})"
            lines.append(header)
            lines.append("")
            lines.append("**Prompt:**")
            lines.append("")
            lines.append((event.prompt or "_(no prompt recorded)_").rstrip())
            lines.append("")
            lines.append("**Result:**")
            lines.append("")
            if event.result:
                lines.append(event.result.rstrip())
            else:
                # Final response was not in the main file; a future --deep mode
                # could recover it from the subagents/ sidecar.
                lines.append("_(no final response in main session file)_")
            lines.append("")

    diag = session.diagnostics
    if diag.unknown_types:
        unknown = ", ".join(f"{name}×{count}" for name, count in sorted(diag.unknown_types.items()))
        lines.append("---")
        lines.append(f"_Diagnostics: unknown entry types encountered: {unknown}_")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"
