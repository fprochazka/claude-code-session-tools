"""Content search over session transcripts: ripgrep when it is installed, cc-blackbox's pure-Python scan otherwise.

ripgrep greps file contents and reports matching files; cc-blackbox's ``session_id_for_path`` maps each one back to
the session that owns it. Subagent transcripts and spilled tool results live in a directory named after their parent
session, so a hit inside one folds into that parent: a search for text that only a subagent saw still finds its
session. ripgrep is 10 to 17 times faster on a large scope; the fallback gives the same answer without it.

The pure argv builder (:func:`build_rg_args`) is kept apart from the subprocess call so it is testable without
ripgrep on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from cc_blackbox import SessionTranscript, session_id_for_path, sessions_containing


class RipgrepError(RuntimeError):
    """Raised when ``rg`` exits with a real error (exit code >= 2)."""


def build_rg_args(patterns: Sequence[str], dirs: Sequence[Path]) -> list[str]:
    """Assemble the ripgrep argv for a literal, case-insensitive content search.

    Every pattern is passed as the value of its own ``-e``; ripgrep reports a file when *any* of
    them matches, so a multi-pattern argv is the OR of the keywords in one pass. The single-pattern
    call is just the degenerate case of that, which is what lets the AND path reuse this builder.

    ``patterns`` are untrusted, and ``-e`` is what makes them safe: the value is consumed as that
    option's argument, so a keyword beginning with ``-`` can never be read as a flag, and
    ``--fixed-strings`` keeps it a literal (no regex injection). The ``--`` separator then closes
    the flags so the directory operands are unambiguous. Combined with an arg-list
    ``subprocess.run`` (no shell) this is injection-safe.
    """
    pattern_args: list[str] = []
    for pattern in patterns:
        pattern_args += ["-e", pattern]

    return [
        "--files-with-matches",  # we only need WHICH files match, not the lines.
        "--fixed-strings",  # literal match — no regex, no injection.
        "--ignore-case",
        "--hidden",  # ~/.claude is a dotdir — rg skips hidden paths without this.
        "--no-ignore",  # and ~/.claude is often a git repo whose .gitignore hides projects/.
        "--no-messages",  # suppress per-file errors (e.g. unreadable files).
        *pattern_args,
        "--",  # end of flags; everything after is path operands.
        *(str(d) for d in dirs),
    ]


def _search_dirs(sessions: Sequence[SessionTranscript]) -> list[tuple[Path, Path]]:
    """The project dirs holding ``sessions``, each with its projects dir, which ``session_id_for_path`` needs."""
    found: dict[Path, Path] = {}
    for session in sessions:
        found.setdefault(session.path.parent, session.projects_dir.path)
    return sorted(found.items())


def _is_content(hit: Path, session_id: str) -> bool:
    """Whether ``hit`` is a file whose text the session holds: the main transcript, a subagent's or a tool result.

    Claude Code keeps other files under the session's dir too, such as logs of the auto-mode classifier. The
    pure-Python fallback reads only the content files, and both paths must agree.
    """
    if hit.name == f"{session_id}.jsonl":
        return True
    parts = hit.parts
    index = parts.index(session_id) if session_id in parts else -1
    return 0 <= index < len(parts) - 2 and parts[index + 1] in ("subagents", "tool-results")


def _rg_pass(rg: str, patterns: Sequence[str], dirs: Sequence[tuple[Path, Path]]) -> set[str]:
    """One ripgrep pass: the session ids owning any file that contains any of ``patterns``."""
    proc = subprocess.run(
        [rg, *build_rg_args(patterns, [d for d, _ in dirs])], capture_output=True, text=True, check=False
    )
    if proc.returncode == 1:
        return set()
    if proc.returncode >= 2:
        raise RipgrepError(f"ripgrep failed (exit {proc.returncode}): {proc.stderr.strip()}")

    projects_dirs = dict(dirs)
    ids: set[str] = set()
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        hit = Path(line.strip())
        projects = next((projects_dirs[p] for p in hit.parents if p in projects_dirs), None)
        session_id = session_id_for_path(hit, projects) if projects is not None else None
        if session_id and _is_content(hit, session_id):
            ids.add(session_id)
    return ids


def search_session_ids(
    queries: Sequence[str], sessions: Sequence[SessionTranscript], *, match_any: bool = False
) -> set[str]:
    """Return the ids of the ``sessions`` whose content matches the queries — all of them, or any of them.

    The AND is per session, not per line: ripgrep is line-oriented and one JSONL line is one record, so a combined
    pattern would only match keywords that share a record. Each keyword gets its own pass and the id sets are
    intersected, stopping at the first empty pass. ``match_any`` is a single pass with every keyword attached.

    Without ``rg`` on PATH the same question goes to cc-blackbox's ``sessions_containing``, which reads the files in
    Python. Blank queries are ignored; no usable query or no session returns an empty set.

    Raises:
        RipgrepError: ``rg`` exited with code 2 or more.
    """
    usable = [q for q in queries if q.strip()]
    if not usable or not sessions:
        return set()

    rg = shutil.which("rg")
    if rg is None:
        return {s.session_id for s in sessions_containing(sessions, usable, match_any=match_any)}

    wanted = {s.session_id for s in sessions}
    dirs = _search_dirs(sessions)
    if match_any:
        return _rg_pass(rg, usable, dirs) & wanted

    matched: set[str] | None = None
    for query in usable:
        ids = _rg_pass(rg, [query], dirs)
        matched = ids if matched is None else matched & ids
        if not matched:
            return set()
    return (matched or set()) & wanted
