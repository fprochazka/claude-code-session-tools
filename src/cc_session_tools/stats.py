"""Summarize a rendered transcript so a calling agent can budget its context."""

from __future__ import annotations

from dataclasses import dataclass

from .parse import ParsedSession


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
class TranscriptStats:
    prompt_count: int
    subagent_count: int
    byte_size: int
    char_count: int
    estimated_tokens: int


def compute_stats(session: ParsedSession, markdown: str) -> TranscriptStats:
    """Derive stats from the exact ``markdown`` that is written/emitted, so sizes are truthful."""
    return TranscriptStats(
        prompt_count=session.prompt_count,
        subagent_count=session.subagent_count,
        byte_size=len(markdown.encode("utf-8")),
        char_count=len(markdown),
        estimated_tokens=estimate_tokens(markdown),
    )
