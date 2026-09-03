"""Per-session metadata extracted from a transcript ``.jsonl`` file.

For each session we need three things to render a list row: its last-interaction
time, its real working directory, and a display name. All three come from the
transcript content (not the filesystem), which is the deliberate divergence from
the sibling tools — they sort by file mtime, which is bumped by retroactive
metadata writes (``ai-title``/``summary`` appended hours or weeks later) and so
does not reflect when the human last interacted.

Read strategy: a HEAD slice carries the early records (first ``cwd``, first user
message, an early ``ai-title``/``summary``) and a TAIL slice carries the late ones
(the maximum ``.timestamp``, a late ``ai-title``/``last-prompt``). Records are
append-ordered and thus roughly chronological, so the maximum timestamp always
lives in the tail — the unread middle can never exceed it. Small files are read
whole. Slice boundaries drop the partial line they cut through so a truncated
record never reaches ``json.loads``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .titles import clean_title

# Bytes read from each end of a large transcript. The head must comfortably span
# the opening metadata plus the first human message; the tail must span the final
# timestamped records. Files at or below their sum are read in a single pass.
_HEAD_BYTES = 256 * 1024
_TAIL_BYTES = 128 * 1024

# String-content user messages beginning with any of these tags are harness
# injections (command wiring, bash echo, tool plumbing, task notifications), not
# something the human typed — skipped when hunting for the first real message.
INJECTION_TAG_PREFIXES: tuple[str, ...] = (
    "<local-command-caveat",
    "<local-command-stderr",
    "<bash-input",
    "<bash-stdout",
    "<bash-stderr",
    "<bash-exit-code",
    "<command-message",
    "<command-name",
    "<command-args",
    "<task-notification",
)


@dataclass
class SessionMeta:
    session_id: str
    path: Path
    last_ts: datetime
    cwd: str | None
    name: str
    # True when ``name`` is a title we derived from the first user message (or a
    # bare session-id fallback) rather than a real session name — drives dimmed
    # rendering in the list. ``ai-title`` and ``summary`` are genuine names.
    # ``last-prompt`` counts as genuine too: it is the user's verbatim final
    # prompt, a meaningful human-authored label, not a synthesized title.
    name_is_derived: bool
    # Repo context recorded by Claude Code on the session's records. Both are absent
    # from older transcripts and from sessions started outside a git repo, so both
    # stay optional; only the first record carrying each key is consulted.
    git_branch: str | None = None
    version: str | None = None
    # False when the transcript carried no timestamped record at all and ``last_ts``
    # had to fall back to the file mtime. Such a file is a stub — a bridge-session
    # marker or a bare retroactive title — with no interaction in it, and its mtime
    # can make it look like the newest session in a scope.
    last_ts_from_transcript: bool = True


def _parse_ts(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp (e.g. ``2026-02-13T14:22:13.214Z``) to aware UTC."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _mtime_utc(path: Path) -> datetime:
    try:
        stamp = path.stat().st_mtime
    except OSError:
        stamp = 0.0
    return datetime.fromtimestamp(stamp, tz=UTC)


def _read_slices(path: Path) -> list[dict]:
    """Return decoded JSON records from the head and (for large files) tail slices.

    Blank and malformed lines are skipped — a transcript's final line can be a
    partial write, and a mid-file line is occasionally corrupt. Records keep file
    order (all head records precede all tail records), so callers can take the
    "first" occurrence of anything by scanning front-to-back.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return []

    try:
        with path.open("rb") as fh:
            if size <= _HEAD_BYTES + _TAIL_BYTES:
                lines = fh.read().decode("utf-8", errors="replace").split("\n")
            else:
                head = fh.read(_HEAD_BYTES).decode("utf-8", errors="replace")
                # Drop the head's last line: the slice cut through it mid-record.
                head_lines = head.split("\n")[:-1]
                fh.seek(size - _TAIL_BYTES)
                tail = fh.read().decode("utf-8", errors="replace")
                # Drop the tail's first line: the slice began mid-record.
                tail_lines = tail.split("\n")[1:]
                lines = head_lines + tail_lines
    except OSError:
        return []

    records: list[dict] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            record = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _first_user_message(content: object) -> str | None:
    """Return a plain-string user message that is a real human prompt, else None.

    Array content is tool-result plumbing, and strings opening with an injection
    tag are harness-generated — both are skipped.
    """
    if not isinstance(content, str):
        return None
    if not content.strip():
        return None
    head = content.lstrip()
    if head.startswith(INJECTION_TAG_PREFIXES):
        return None
    return content


def _first_string_field(records: list[dict], key: str) -> str | None:
    """Return the first non-empty string value of ``key`` across ``records``.

    Records keep file order, so this is the earliest occurrence in the transcript.
    Empty strings are treated as absent: a blank ``gitBranch`` (detached HEAD) or a
    blank ``cwd`` carries no more information than a missing key.
    """
    for record in records:
        value = record.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _resolve_name(records: list[dict]) -> tuple[str | None, bool]:
    """Apply the display-name fallback chain over already-parsed records.

    Chain: ``ai-title`` → ``summary`` → ``last-prompt`` (all genuine names) →
    ``clean_title(first real user message)`` (derived). Returns ``(None, True)``
    when nothing yields a name, letting the caller substitute the session id.
    """
    ai_title: str | None = None
    summary: str | None = None
    last_prompt: str | None = None
    first_user: str | None = None

    for record in records:
        rtype = record.get("type")
        if rtype == "ai-title" and ai_title is None:
            value = record.get("aiTitle")
            if isinstance(value, str) and value.strip():
                ai_title = value.strip()
        elif rtype == "summary" and summary is None:
            value = record.get("summary")
            if isinstance(value, str) and value.strip():
                summary = value.strip()
        elif rtype == "last-prompt" and last_prompt is None:
            value = record.get("lastPrompt")
            if isinstance(value, str) and value.strip():
                last_prompt = value.strip()
        elif rtype == "user" and first_user is None:
            message = record.get("message")
            if isinstance(message, dict):
                candidate = _first_user_message(message.get("content"))
                if candidate is not None:
                    first_user = candidate

    if ai_title is not None:
        return ai_title, False
    if summary is not None:
        return summary, False
    if last_prompt is not None:
        return last_prompt, False
    if first_user is not None:
        return clean_title(first_user), True
    return None, True


def read_session_meta(path: Path) -> SessionMeta:
    """Extract :class:`SessionMeta` for a single session transcript file."""
    path = Path(path)
    session_id = path.stem
    records = _read_slices(path)

    timestamps = [ts for record in records if (ts := _parse_ts(record.get("timestamp"))) is not None]
    last_ts_from_transcript = bool(timestamps)
    last_ts = max(timestamps) if last_ts_from_transcript else _mtime_utc(path)

    cwd = _first_string_field(records, "cwd")
    git_branch = _first_string_field(records, "gitBranch")
    version = _first_string_field(records, "version")

    name, name_is_derived = _resolve_name(records)
    if name is None:
        name = session_id
        name_is_derived = True

    return SessionMeta(
        session_id=session_id,
        path=path,
        last_ts=last_ts,
        cwd=cwd,
        name=name,
        name_is_derived=name_is_derived,
        git_branch=git_branch,
        version=version,
        last_ts_from_transcript=last_ts_from_transcript,
    )
