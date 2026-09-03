"""Session discovery: which ``~/.claude/projects/`` dirs to look at and which of
their sessions actually belong to the current directory (or its git worktrees).

The candidate *dirs* are chosen by cheap encoded-name pre-filtering (see
``gitrepo.WorktreeDirPatterns``), which over-matches on purpose. Real membership is
then confirmed per session against its non-lossy ``cwd`` field — never via the
filesystem existence of the worktree path, so sessions of a **deleted** worktree
still show up. The ``cwd`` check is a proper path-boundary test (``is_relative_to``),
so a sibling repo like ``metabasecli-extra`` whose name passes the cheap prefix is
correctly rejected once its real ``cwd`` proves to be outside the repo root.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .gitrepo import main_repo_root, worktree_dir_patterns
from .paths import encode_path, resolve_projects_dir
from .transcript import read_session_meta


def _abspath(path: str | Path) -> Path:
    """Lexically absolutize a path *without* following symlinks.

    Matches ``paths.encode_path`` normalization (``os.path.abspath``, not
    ``Path.resolve``) so both sides of every membership comparison line up with
    the literal abspath git and Claude Code record in the transcript ``cwd`` field.
    """
    return Path(os.path.abspath(os.fspath(path)))


@dataclass(frozen=True)
class TargetScope:
    """The project dirs to scan plus the git anchors used to confirm membership.

    - ``root`` — main repo root, or ``None`` when the cwd is not in a git repo.
    - ``legacy_base`` — the legacy ``<parent>/<name>-worktrees`` base dir (a sibling
      of ``root``, outside it), or ``None`` outside a git repo.
    - ``candidate_project_dirs`` — ``~/.claude/projects/`` dirs whose *names* passed
      the cheap encoded pre-filter; still subject to per-session ``cwd`` confirmation.
    """

    root: Path | None
    legacy_base: Path | None
    candidate_project_dirs: list[Path]


@dataclass(frozen=True)
class SessionRow:
    """One display row: a confirmed-member session and its worktree label."""

    session_id: str
    path: Path
    last_ts: datetime
    name: str
    name_is_derived: bool
    # Worktree path relative to the main repo root: ``""`` for the main repo (or
    # the non-git case), ``".worktrees/fp-foo"`` for an in-tree worktree, and
    # ``"<name>-worktrees/fp-foo"`` for a legacy sibling worktree. In all-projects
    # mode there is no root to be relative to, so it holds the session's own ``cwd``.
    worktree_rel: str


def resolve_target_dirs(cwd: str | Path, *, projects_dir: Path | None = None) -> TargetScope:
    """Decide which ``~/.claude/projects/`` dirs to consider for ``cwd``.

    In a git repo: pre-filter project *dir names* by the encoded root/worktree/legacy
    patterns (no file reads). Outside a git repo (D5): the single project dir whose
    name equals ``encode_path(cwd)``, if it exists — no worktree expansion.
    """
    projects_dir = projects_dir or resolve_projects_dir()
    root = main_repo_root(cwd)

    if root is not None:
        root = _abspath(root)
        legacy_base = root.parent / f"{root.name}-worktrees"
        patterns = worktree_dir_patterns(root)
        try:
            names = sorted(p.name for p in projects_dir.iterdir() if p.is_dir())
        except OSError:
            names = []
        candidates = [projects_dir / name for name in names if patterns.matches(name)]
        return TargetScope(root=root, legacy_base=legacy_base, candidate_project_dirs=candidates)

    target = projects_dir / encode_path(cwd)
    candidates = [target] if target.is_dir() else []
    return TargetScope(root=None, legacy_base=None, candidate_project_dirs=candidates)


def all_projects_scope(projects_dir: Path | None = None) -> TargetScope:
    """A scope covering every project dir, with no git anchors.

    Used by ``--all``: there is no repo to confirm membership against, so ``root``
    and ``legacy_base`` are ``None`` and every direct subdirectory of ``projects_dir``
    becomes a candidate. Membership is then unconditional (see ``build_session_rows``).
    """
    projects_dir = projects_dir or resolve_projects_dir()
    try:
        names = sorted(p.name for p in projects_dir.iterdir() if p.is_dir())
    except OSError:
        names = []
    return TargetScope(root=None, legacy_base=None, candidate_project_dirs=[projects_dir / name for name in names])


def find_candidate_session_files(scope: TargetScope) -> list[Path]:
    """Enumerate top-level ``*.jsonl`` session files in each candidate project dir.

    Only direct children are returned; the ``<uuid>/`` sidecar dirs (subagent and
    tool-result transcripts) are deliberately not descended into — they are not
    sessions.
    """
    files: list[Path] = []
    for project_dir in scope.candidate_project_dirs:
        try:
            files.extend(sorted(project_dir.glob("*.jsonl")))
        except OSError:
            continue
    return files


def _is_member(meta_cwd: str | None, *, cwd: str, root: Path | None, legacy_base: Path | None) -> bool:
    """Confirm a session belongs to the target, using its real ``cwd`` field.

    Not in a git repo: accept when the session's ``cwd`` equals the target ``cwd``.
    When ``cwd`` is missing (older session with no cwd field), fall back to accepting
    it — being found in the exact ``encode_path(cwd)`` dir is proof enough.

    In a git repo: accept when the real ``cwd`` is the main ``root`` or a proper
    path-descendant of it (covers the main checkout, its subdirs, and every
    ``.worktrees/*`` worktree, live or deleted), or falls under the legacy
    worktrees base. When ``cwd`` is missing, fall back to the encoded-dir-name match
    the cheap pre-filter already established.
    """
    if root is None:
        if meta_cwd is None:
            return True
        return _abspath(meta_cwd) == _abspath(cwd)

    if meta_cwd is None:
        return True

    real = _abspath(meta_cwd)
    if real.is_relative_to(root):
        return True
    return legacy_base is not None and real.is_relative_to(legacy_base)


def _worktree_rel(meta_cwd: str | None, *, root: Path | None, legacy_base: Path | None) -> str:
    """Compute the session's worktree path relative to the main repo root.

    Main repo (``cwd == root``) and the non-git case both yield ``""``. An in-tree
    worktree yields e.g. ``".worktrees/fp-foo"``. A legacy worktree — outside the
    root — is labeled relative to ``root.parent``, e.g. ``"<name>-worktrees/fp-foo"``.
    A member with no ``cwd`` field can't be located, so it defaults to ``""``.
    """
    if root is None or meta_cwd is None:
        return ""

    real = _abspath(meta_cwd)
    if real == root:
        return ""
    if real.is_relative_to(root):
        return str(real.relative_to(root))
    if legacy_base is not None and real.is_relative_to(legacy_base):
        return str(real.relative_to(root.parent))
    return ""


def build_session_rows(
    cwd: str | Path, *, projects_dir: Path | None = None, all_projects: bool = False
) -> list[SessionRow]:
    """Build the confirmed, newest-first session rows for ``cwd``.

    For every candidate session file: read its metadata (C4), confirm membership via
    the real ``cwd`` field, compute its worktree-relative label, and collect a row.
    Rows are returned sorted by ``last_ts`` descending; no limit/grep is applied here.

    With ``all_projects`` every session under ``projects_dir`` is returned regardless
    of ``cwd``: there is nothing to confirm membership against, so the ``cwd`` check is
    skipped and ``worktree_rel`` carries the session's own ``cwd`` instead of a label
    relative to a repo root.
    """
    cwd_abs = os.path.abspath(os.fspath(cwd))
    if all_projects:
        scope = all_projects_scope(projects_dir)
    else:
        scope = resolve_target_dirs(cwd_abs, projects_dir=projects_dir)

    rows: list[SessionRow] = []
    for path in find_candidate_session_files(scope):
        meta = read_session_meta(path)
        if all_projects:
            worktree_rel = meta.cwd or ""
        else:
            if not _is_member(meta.cwd, cwd=cwd_abs, root=scope.root, legacy_base=scope.legacy_base):
                continue
            worktree_rel = _worktree_rel(meta.cwd, root=scope.root, legacy_base=scope.legacy_base)
        rows.append(
            SessionRow(
                session_id=meta.session_id,
                path=meta.path,
                last_ts=meta.last_ts,
                name=meta.name,
                name_is_derived=meta.name_is_derived,
                worktree_rel=worktree_rel,
            )
        )

    rows.sort(key=lambda row: row.last_ts, reverse=True)
    return rows
