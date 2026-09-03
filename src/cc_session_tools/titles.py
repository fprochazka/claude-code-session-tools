"""Derive a clean, human-readable list title from a raw first-user-message.

Reuses the same markup interpreter as the transcript renderer so list and detail
agree on what the markup means, and never returns angle-bracket tag soup.
"""

from __future__ import annotations

import re

from .user_markup import ShellCommandPart, SlashCommandPart, TextPart, parse_user_markup

_TITLE_MAX = 160
_ARGS_MAX = 120
_EMPTY_TITLE = "(no prompt)"


def _collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _truncate(s: str, max_len: int) -> str:
    return s[: max_len - 1].rstrip() + "…" if len(s) > max_len else s


def clean_title(raw: str | None) -> str:
    """Turn a raw first-user-message into a clean, human-readable list title.

    Reuses the same markup interpreter as the transcript so list and detail agree
    on what the markup means. Never returns angle-bracket tag soup.
    """
    if not raw:
        return _EMPTY_TITLE
    parts = parse_user_markup(raw)
    if not parts:
        return _EMPTY_TITLE
    part = parts[0]

    if isinstance(part, SlashCommandPart):
        if not part.args:
            return part.name
        return f"{part.name} — {_truncate(_collapse(part.args), _ARGS_MAX)}"

    if isinstance(part, ShellCommandPart):
        cmd = _collapse(part.command)
        if not cmd:
            return _EMPTY_TITLE
        return f"$ {_truncate(cmd, _TITLE_MAX - 2)}"

    if isinstance(part, TextPart):
        line = next((line.strip() for line in part.text.split("\n") if line.strip()), "")
        cleaned = _collapse(line)
        return _truncate(cleaned, _TITLE_MAX) if cleaned else _EMPTY_TITLE

    return _EMPTY_TITLE
