"""Ripgrep-backed content search over a set of target project dirs.

Ported from the replay UI's ``buildRgArgs()`` / ``matchPathToSessionId()`` (see
``claude-code-session-replay/src/server/search.ts``). We grep file *contents* with ripgrep,
then map every matching path back to the session id that owns it — subagent transcripts and
spilled tool-result sidecars live in directories *named* for their parent session, so a hit
inside one folds into that parent (a search for text that only appears in a subagent still
finds its session).

The pure argv builder (:func:`build_rg_args`) and the pure path mapper
(:func:`match_path_to_session_id`) are kept separate from the subprocess call
(:func:`search_session_ids`) so they're unit-testable without ripgrep on PATH.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path


class RipgrepNotFoundError(RuntimeError):
    """Raised when ``rg`` isn't on PATH — surfaced to the user with install guidance."""

    def __init__(self) -> None:
        super().__init__(
            "ripgrep (rg) is required for --rg; install it (e.g. `apt install ripgrep` or `brew install ripgrep`)"
        )


class RipgrepError(RuntimeError):
    """Raised when ``rg`` exits with a real error (exit code >= 2)."""


def build_rg_args(patterns: Sequence[str], dirs: list[Path]) -> list[str]:
    """Assemble the ripgrep argv for a literal, case-insensitive content search.

    Every pattern is passed as the value of its own ``-e``; ripgrep reports a file when *any* of
    them matches, so a multi-pattern argv is the OR of the keywords in one pass. The single-pattern
    call is just the degenerate case of that, which is what lets the AND path reuse this builder.

    ``patterns`` are untrusted, and ``-e`` is what makes them safe: the value is consumed as that
    option's argument, so a keyword beginning with ``-`` can never be read as a flag, and
    ``--fixed-strings`` keeps it a literal (no regex injection). The ``--`` separator then closes
    the flags so the directory operands are unambiguous. Combined with an arg-list
    ``subprocess.run`` (no shell) this is injection-safe. Search is scoped to the passed-in
    ``dirs``, not all of the projects dir.
    """
    pattern_args: list[str] = []
    for pattern in patterns:
        pattern_args += ["-e", pattern]

    return [
        "--files-with-matches",  # we only need WHICH files match, not the lines.
        "--fixed-strings",  # literal match — no regex, no injection.
        "--ignore-case",
        "--hidden",  # ~/.claude is a dotdir — rg skips hidden paths without this.
        "--no-ignore",  # and don't honor .gitignore/global ignores under it.
        "--no-messages",  # suppress per-file errors (e.g. unreadable files).
        *pattern_args,
        "--",  # end of flags; everything after is path operands.
        *(str(d) for d in dirs),
    ]


def match_path_to_session_id(match_path: Path | str, projects_dir: Path) -> str | None:
    """Map a ripgrep match path back to the session id that owns it.

    Layout under the projects dir::

        <proj>/<sessionId>.jsonl                   → sessionId
        <proj>/<sessionId>/subagents/agent-*.jsonl → sessionId (parent)
        <proj>/<sessionId>/tool-results/*          → sessionId (parent)

    Walks the path segments (relative to ``projects_dir``) from the deepest: a ``subagents`` or
    ``tool-results`` segment means the session id is the directory just above it. Otherwise a
    top-level ``<sessionId>.jsonl`` yields its stem. Returns ``None`` for anything that doesn't
    fit or that resolves outside ``projects_dir`` (so callers skip it rather than fabricate an id).
    """
    rel = os.path.relpath(os.fspath(match_path), os.fspath(projects_dir))
    segs = [s for s in rel.split(os.sep) if s]

    # Outside the projects dir (relative climbs out with `..`) → ignore.
    if segs and segs[0] == "..":
        return None
    # Need at least <proj>/<file>.
    if len(segs) < 2:
        return None

    # Sidecar dirs name their parent session: a `subagents`/`tool-results` segment means
    # the session id is the directory just above it.
    for i in range(len(segs) - 1, 0, -1):
        if segs[i] in ("subagents", "tool-results"):
            parent = segs[i - 1]
            return parent or None

    # Otherwise it's a top-level session file: <proj>/<sessionId>.jsonl.
    file = segs[-1]
    if file.endswith(".jsonl"):
        return file[: -len(".jsonl")]
    return None


def search_session_ids(
    queries: Sequence[str], dirs: list[Path], projects_dir: Path, *, match_any: bool = False
) -> set[str]:
    """Return the session ids whose content matches the queries — all of them, or any of them.

    The default AND cannot be expressed in a single ``rg`` invocation: ripgrep is line-oriented and
    one JSONL line is one message record, so a combined pattern would only match keywords that share
    a *record*. The AND we want is session-level — keywords may sit in different messages, or in a
    subagent / tool-result sidecar that folds into the same parent session — so each query gets its
    own pass and the resulting id sets are intersected. An empty pass makes the intersection empty,
    so we return straight away rather than paying for the remaining passes.

    ``match_any`` is the OR, and that one *is* a single pass: ripgrep already reports a file when any
    of its ``-e`` patterns hits, so the union falls out of one invocation with all keywords attached.

    Blank/whitespace queries are ignored; no usable queries or empty ``dirs`` short-circuit to an
    empty set without invoking ``rg``. Exit code 1 (no matches) is empty, not an error; exit code
    >= 2 raises :class:`RipgrepError`; ``rg`` missing from PATH raises :class:`RipgrepNotFoundError`.
    """
    usable = [q for q in queries if q.strip()]
    if not usable or not dirs:
        return set()

    if match_any:
        return _search_pass(usable, dirs, projects_dir)

    matched: set[str] | None = None
    for query in usable:
        ids = _search_pass([query], dirs, projects_dir)
        matched = ids if matched is None else matched & ids
        if not matched:
            return set()
    return matched or set()


def _search_pass(patterns: Sequence[str], dirs: list[Path], projects_dir: Path) -> set[str]:
    """One ripgrep pass: the session ids owning any file that contains any of ``patterns``."""
    args = ["rg", *build_rg_args(patterns, dirs)]
    try:
        proc = subprocess.run(args, capture_output=True, text=True, check=False)
    except FileNotFoundError as exc:
        raise RipgrepNotFoundError() from exc

    if proc.returncode == 1:
        # No matches.
        return set()
    if proc.returncode >= 2:
        raise RipgrepError(f"ripgrep failed (exit {proc.returncode}): {proc.stderr.strip()}")

    ids: set[str] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        session_id = match_path_to_session_id(line, projects_dir)
        if session_id:
            ids.add(session_id)
    return ids
