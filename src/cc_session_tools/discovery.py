"""Which sessions belong to the current directory, and which session a ``distill`` target names.

cc-blackbox finds and reads the transcripts of every Claude Code profile. This module adds the user-facing scope on
top: inside a git repo a session belongs to the repo when it was launched in the main checkout, in any of its
worktrees, or in the legacy ``<name>-worktrees/`` sibling dir. Membership comes from the session's own launch ``cwd``,
never from whether that directory still exists, so sessions of a deleted worktree still show up. Outside a git repo
only sessions launched in the exact current directory belong to it.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cc_blackbox import (
    SessionTranscript,
    TranscriptNotFound,
    find_session_transcript,
    is_session_id,
    list_session_transcripts,
    read_session_transcript,
)

from .gitrepo import main_repo_root
from .titles import clean_title


def _abspath(path: str | os.PathLike[str]) -> Path:
    """Lexically absolutize a path without following symlinks, the way git and Claude Code record it."""
    return Path(os.path.abspath(os.fspath(path)))


def projects_dirs_override() -> list[Path] | None:
    """The projects dir from ``CLAUDE_PROJECTS_DIR``, or ``None`` to cover every Claude Code profile."""
    override = os.environ.get("CLAUDE_PROJECTS_DIR")
    return [Path(override).expanduser()] if override else None


@dataclass(frozen=True)
class Scope:
    """The directories whose sessions a command covers.

    - ``root``: the main repo root, or ``None`` outside a git repo and under ``--all``.
    - ``legacy_base``: the legacy ``<parent>/<name>-worktrees`` dir next to ``root``.
    - ``cwd``: the current directory, which is the whole scope outside a git repo.
    - ``all_projects``: every session of every project.
    """

    root: Path | None
    legacy_base: Path | None
    cwd: Path
    all_projects: bool = False

    @property
    def in_git_repo(self) -> bool:
        return self.root is not None

    def cwd_within(self) -> list[Path] | None:
        if self.all_projects:
            return None
        if self.root is not None and self.legacy_base is not None:
            return [self.root, self.legacy_base]
        return [self.cwd]

    def contains(self, transcript: SessionTranscript) -> bool:
        """Outside a git repo, keep only sessions launched in the exact cwd; the listing already covers the rest."""
        if self.all_projects or self.root is not None or transcript.cwd is None:
            return True
        return _abspath(transcript.cwd) == self.cwd


def resolve_scope(cwd: str | os.PathLike[str], *, all_projects: bool = False) -> Scope:
    cwd_abs = _abspath(cwd)
    if all_projects:
        return Scope(root=None, legacy_base=None, cwd=cwd_abs, all_projects=True)
    root = main_repo_root(cwd_abs)
    if root is None:
        return Scope(root=None, legacy_base=None, cwd=cwd_abs)
    root = _abspath(root)
    return Scope(root=root, legacy_base=root.parent / f"{root.name}-worktrees", cwd=cwd_abs)


@dataclass(frozen=True)
class SessionRow:
    """One display row: a session in scope and its location label."""

    session_id: str
    path: Path
    last_ts: datetime
    name: str
    # True when ``name`` is derived from the first prompt (or is the bare session id) rather than a title the session
    # actually has; the list renders such names dimmed.
    name_is_derived: bool
    # Inside a git repo: the session's path relative to the main repo root, ``""`` for the main checkout,
    # ``".worktrees/fp-foo"`` for an in-tree worktree and ``"<name>-worktrees/fp-foo"`` for a legacy one. Under
    # ``--all``: the session's own cwd. Outside a git repo: ``""``.
    worktree_rel: str
    # False for a stub with no timestamped record, whose ``last_ts`` is only the file mtime. Listing keeps such rows;
    # ``latest`` skips them.
    last_ts_from_transcript: bool
    profile: str
    """The first profile that uses the session's projects dir, or the dir itself when no profile does."""
    transcript: SessionTranscript


def _worktree_rel(transcript: SessionTranscript, scope: Scope) -> str:
    if scope.all_projects:
        return str(transcript.cwd) if transcript.cwd is not None else ""
    if scope.root is None or transcript.cwd is None:
        return ""
    real = _abspath(transcript.cwd)
    if real == scope.root:
        return ""
    if real.is_relative_to(scope.root):
        return str(real.relative_to(scope.root))
    if scope.legacy_base is not None and real.is_relative_to(scope.legacy_base):
        return str(real.relative_to(scope.root.parent))
    return ""


def _row(transcript: SessionTranscript, scope: Scope) -> SessionRow:
    if transcript.title is not None:
        name, derived = transcript.title, False
    elif transcript.first_prompt is not None:
        name, derived = clean_title(transcript.first_prompt), True
    else:
        name, derived = transcript.session_id, True
    profiles = transcript.projects_dir.profiles
    return SessionRow(
        session_id=transcript.session_id,
        path=transcript.path,
        last_ts=transcript.last_activity_at or transcript.modified_at,
        name=name,
        name_is_derived=derived,
        worktree_rel=_worktree_rel(transcript, scope),
        last_ts_from_transcript=transcript.last_activity_at is not None,
        profile=profiles[0].name if profiles else str(transcript.projects_dir.path),
        transcript=transcript,
    )


def build_session_rows(scope: Scope, *, projects_dirs: Sequence[Path] | None = None) -> list[SessionRow]:
    """Return the sessions in ``scope``, newest interaction first, from every profile or from ``projects_dirs``."""
    transcripts = list_session_transcripts(projects_dirs=projects_dirs, cwd_within=scope.cwd_within())
    rows = [_row(t, scope) for t in transcripts if scope.contains(t)]
    rows.sort(key=lambda row: row.last_ts, reverse=True)
    return rows


def resolve_target(target: str, *, cwd: str, projects_dirs: Sequence[Path] | None = None) -> SessionTranscript | None:
    """Resolve a distill target: a ``.jsonl`` path, a session id, or ``"latest"``.

    ``"latest"`` uses the same scope as ``search`` and skips stubs, which have no interaction to distill but whose
    mtime can put them first. There is deliberately no "newest session overall" fallback: distilling an unrelated
    session is worse than failing. Returns ``None`` when nothing resolves.
    """
    candidate = Path(target)
    if candidate.suffix == ".jsonl" and candidate.is_file():
        try:
            return read_session_transcript(candidate)
        except TranscriptNotFound:
            return None

    if is_session_id(target):
        # Main transcripts only: a subagent transcript under <session id>/subagents/ is not a session.
        try:
            return find_session_transcript(target, projects_dirs=projects_dirs)
        except TranscriptNotFound:
            return None

    if target == "latest":
        rows = build_session_rows(resolve_scope(cwd), projects_dirs=projects_dirs)
        return next((row.transcript for row in rows if row.last_ts_from_transcript), None)

    return None
