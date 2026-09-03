"""Classify a Claude Code ``.jsonl`` session into a condensed event stream.

The on-disk format is reverse-engineered, undocumented, and version-churning, so
this is defensive throughout: unknown ``type``s, malformed lines, and unexpected
shapes are counted in diagnostics rather than raised (the "count-don't-throw"
rule). The output keeps only the signal another agent needs to extract decisions
from a past session: human prompts, assistant prose, and subagent prompt+result
pairs. All tool-call noise is dropped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from .user_markup import (
    ShellCommandPart,
    SlashCommandPart,
    TextPart,
    parse_user_markup,
)

# Entry `type`s we recognize as pure noise for the condensed transcript. They are
# counted (not silently dropped) so a future format change that floods one of
# these is visible in diagnostics.
_DROP_TYPES = frozenset(
    {
        "system",
        "progress",
        "file-history-snapshot",
        "queue-operation",
        "last-prompt",
        "attachment",
        "permission-mode",
        "ai-title",
        "bridge-session",
        # `summary` / compact-boundary entries: dropped for the MVP. A future
        # pass could surface them as section dividers.
        "summary",
    }
)

# Tool names that spawn a subagent. Their prompt+result are kept; every other
# tool_use / tool_result is dropped.
_SUBAGENT_TOOLS = frozenset({"Task", "Agent"})


@dataclass
class HumanPrompt:
    text: str
    ts: str | None = None


@dataclass
class AssistantText:
    text: str
    ts: str | None = None


@dataclass
class Thinking:
    text: str
    ts: str | None = None


@dataclass
class Subagent:
    """A subagent invocation: the spawning prompt plus its final response.

    ``result`` is filled from the linked tool_result (anchored on a user entry
    whose ``toolUseResult.agentId`` is a non-null string). It stays ``None`` when
    no final response is found in the main file -- a future ``--deep`` mode could
    recover it from the ``subagents/`` sidecar.
    """

    tool_use_id: str
    description: str | None
    subagent_type: str | None
    prompt: str
    result: str | None = None
    ts: str | None = None


Event = HumanPrompt | AssistantText | Thinking | Subagent


@dataclass
class Diagnostics:
    malformed_lines: int = 0
    dropped_entries: int = 0
    unknown_types: dict[str, int] = field(default_factory=dict)

    def note_unknown(self, type_name: str) -> None:
        self.unknown_types[type_name] = self.unknown_types.get(type_name, 0) + 1


@dataclass
class ParsedSession:
    session_id: str
    cwd: str | None
    git_branch: str | None
    version: str | None
    started_at: str | None
    ended_at: str | None
    events: list[Event]
    diagnostics: Diagnostics

    @property
    def prompt_count(self) -> int:
        return sum(1 for e in self.events if isinstance(e, HumanPrompt))

    @property
    def subagent_count(self) -> int:
        return sum(1 for e in self.events if isinstance(e, Subagent))


def _stringify_content(content: object) -> str:
    """Normalize a tool_result/message ``content`` (string | list | object) to text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[str] = []
        for part in content:
            if isinstance(part, str):
                out.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                out.append(part["text"])
        return "".join(out)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    return json.dumps(content)


def _user_prompt_text(content: object) -> str:
    """Concatenate the prose of a user message (string content, or text blocks)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out: list[str] = []
    for part in content:
        if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
            out.append(part["text"])
    return "\n".join(out)


def _render_user_parts(raw: str) -> str | None:
    """Interpret a user message's markup, return clean prompt text or ``None``.

    Slash- and shell-commands are rendered compactly so the prompt stays
    readable; pure prose is preserved. Returns ``None`` when nothing meaningful
    survives (entry was only noise).
    """
    parts = parse_user_markup(raw)
    rendered: list[str] = []
    for part in parts:
        if isinstance(part, TextPart):
            rendered.append(part.text)
        elif isinstance(part, SlashCommandPart):
            line = f"_(ran {part.name}"
            line += f" {part.args})_" if part.args else ")_"
            rendered.append(line)
        elif isinstance(part, ShellCommandPart) and part.command:
            # An output-only entry (empty command) carries no prompt signal here.
            rendered.append(f"_(ran shell: `{part.command}`)_")
    text = "\n\n".join(s for s in rendered if s.strip())
    return text or None


def _is_subagent_anchor(entry: dict) -> bool:
    """True iff this user entry is a subagent final-response anchor.

    The robust cross-version anchor is a ``toolUseResult`` object whose
    ``agentId`` is a non-null string. On some entries ``toolUseResult`` is a bare
    string, so the object check guards against that.
    """
    tur = entry.get("toolUseResult")
    return isinstance(tur, dict) and isinstance(tur.get("agentId"), str) and bool(tur.get("agentId"))


def _tool_result_block(content: object) -> dict | None:
    if not isinstance(content, list):
        return None
    for part in content:
        if isinstance(part, dict) and part.get("type") == "tool_result":
            return part
    return None


def parse_session(text: str, session_id: str, include_thinking: bool = False) -> ParsedSession:
    """Parse a whole session file's text into a condensed event stream.

    Two passes: the first records every Task/Agent ``tool_use`` (keyed by id) and
    every subagent final-response (keyed by the tool_use_id it links back to). The
    second walks entries in document order and, at each subagent spawn, emits one
    ``Subagent`` event carrying both the prompt and its already-resolved result,
    so the pair renders adjacently.
    """
    diag = Diagnostics()
    entries: list[dict] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            diag.malformed_lines += 1
            continue
        if not isinstance(entry, dict) or not isinstance(entry.get("type"), str):
            diag.malformed_lines += 1
            continue
        entries.append(entry)

    cwd: str | None = None
    git_branch: str | None = None
    version: str | None = None
    started_at: str | None = None
    ended_at: str | None = None

    # Pass 1: collect subagent spawns and their resolved final responses.
    subagent_inputs: dict[str, dict] = {}
    subagent_results: dict[str, str] = {}
    for entry in entries:
        ts = entry.get("timestamp")
        if isinstance(ts, str):
            if started_at is None:
                started_at = ts
            ended_at = ts
        if cwd is None and isinstance(entry.get("cwd"), str):
            cwd = entry["cwd"]
        if git_branch is None and isinstance(entry.get("gitBranch"), str):
            git_branch = entry["gitBranch"]
        if version is None and isinstance(entry.get("version"), str):
            version = entry["version"]

        if entry.get("type") == "assistant":
            content = (entry.get("message") or {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") in _SUBAGENT_TOOLS
                        and isinstance(block.get("id"), str)
                    ):
                        subagent_inputs[block["id"]] = block.get("input") or {}
        elif entry.get("type") == "user" and _is_subagent_anchor(entry):
            block = _tool_result_block((entry.get("message") or {}).get("content"))
            tool_use_id = block.get("tool_use_id") if block else None
            result_text = _stringify_content(block.get("content")) if block else ""
            if isinstance(tool_use_id, str) and result_text:
                subagent_results[tool_use_id] = result_text

    # Pass 2: emit events in document order.
    events: list[Event] = []
    for entry in entries:
        type_name = entry["type"]
        ts = entry.get("timestamp") if isinstance(entry.get("timestamp"), str) else None

        if type_name in _DROP_TYPES:
            diag.dropped_entries += 1
            continue

        if type_name == "assistant":
            content = (entry.get("message") or {}).get("content")
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and isinstance(block.get("text"), str) and block["text"].strip():
                    events.append(AssistantText(text=block["text"], ts=ts))
                elif btype == "thinking" and include_thinking:
                    think = block.get("thinking")
                    if isinstance(think, str) and think.strip():
                        events.append(Thinking(text=think, ts=ts))
                elif btype == "tool_use" and block.get("name") in _SUBAGENT_TOOLS and isinstance(block.get("id"), str):
                    inp = subagent_inputs.get(block["id"], block.get("input") or {})
                    prompt = inp.get("prompt") if isinstance(inp.get("prompt"), str) else ""
                    events.append(
                        Subagent(
                            tool_use_id=block["id"],
                            description=inp.get("description") if isinstance(inp.get("description"), str) else None,
                            subagent_type=inp.get("subagent_type")
                            if isinstance(inp.get("subagent_type"), str)
                            else None,
                            prompt=prompt,
                            result=subagent_results.get(block["id"]),
                            ts=ts,
                        )
                    )
                # All other assistant blocks (non-subagent tool_use) are dropped.
            continue

        if type_name == "user":
            # A subagent-final anchor is consumed in pass 1; skip it here.
            if _is_subagent_anchor(entry):
                diag.dropped_entries += 1
                continue
            # Synthetic/meta user entries (skill-doc injections, command echoes).
            if entry.get("isMeta") is True:
                diag.dropped_entries += 1
                continue
            content = (entry.get("message") or {}).get("content")
            # A user turn carrying a tool_result block is tool output, not a prompt.
            if _tool_result_block(content) is not None:
                diag.dropped_entries += 1
                continue
            raw = _user_prompt_text(content)
            rendered = _render_user_parts(raw) if raw else None
            if rendered:
                events.append(HumanPrompt(text=rendered, ts=ts))
            else:
                diag.dropped_entries += 1
            continue

        diag.note_unknown(type_name)

    return ParsedSession(
        session_id=session_id,
        cwd=cwd,
        git_branch=git_branch,
        version=version,
        started_at=started_at,
        ended_at=ended_at,
        events=events,
        diagnostics=diag,
    )
