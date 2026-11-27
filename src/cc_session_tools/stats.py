"""Summarize a rendered transcript so a calling agent can budget its context."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .render import DistilledPart


def estimate_tokens(text: str) -> int:
    """Rough token-count estimate via the ~4-chars-per-token rule for prose/markdown.

    This is a budget floor, not a true count: exact Claude token counts require the
    server-side count_tokens API, and Claude Opus 4.7+ tokenizes ~30% denser than this
    heuristic assumes — so a real transcript will usually exceed this number.
    """
    return round(len(text) / 4)


def human_bytes(n: int) -> str:
    """Format a byte count with a binary unit (e.g. ``12.4 KB``)."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            # Bytes are whole; larger units read better with one decimal.
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"


@dataclass
class PreCompactStats:
    """The files of a compacted session's earlier parts, together."""

    part_count: int
    byte_size: int
    estimated_tokens: int


@dataclass
class TranscriptStats:
    prompt_count: int
    subagent_count: int
    byte_size: int
    char_count: int
    estimated_tokens: int
    pre_compact: PreCompactStats | None = None
    """``None`` for a session without compactions."""


def compute_stats(part: DistilledPart, markdown: str, pre_compact_markdowns: Sequence[str] = ()) -> TranscriptStats:
    """Derive stats from the exact markdown that is written or printed, so sizes are truthful.

    ``part`` and ``markdown`` are the main file; ``pre_compact_markdowns`` the files of the earlier parts.
    """
    pre_compact = None
    if pre_compact_markdowns:
        pre_compact = PreCompactStats(
            part_count=len(pre_compact_markdowns),
            byte_size=sum(len(text.encode("utf-8")) for text in pre_compact_markdowns),
            estimated_tokens=sum(estimate_tokens(text) for text in pre_compact_markdowns),
        )
    return TranscriptStats(
        prompt_count=part.prompt_count,
        subagent_count=part.subagent_count,
        byte_size=len(markdown.encode("utf-8")),
        char_count=len(markdown),
        estimated_tokens=estimate_tokens(markdown),
        pre_compact=pre_compact,
    )
