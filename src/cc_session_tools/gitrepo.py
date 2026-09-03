"""Git repo/worktree resolution for mapping a cwd onto its ``~/.claude/projects/`` dirs.

The main repo root comes from ``git worktree list --porcelain`` (its first block is always
the main root, even when invoked from inside a worktree). From that root we derive the cheap
encoded-name *matchers* used to pre-filter project-dir names — pure string ops, no file reads.
Actual per-session membership (via each session's real ``cwd`` field) is confirmed later; these
matchers only narrow the candidate set and may over-match, which the later ``cwd`` check corrects.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .paths import encode_path


@dataclass(frozen=True)
class Worktree:
    """One entry from ``git worktree list --porcelain``."""

    path: Path
    head: str | None = None
    branch: str | None = None
    detached: bool = False


@dataclass(frozen=True)
class WorktreeDirPatterns:
    """Cheap encoded-name matchers that pre-filter ``~/.claude/projects/`` dir names.

    Fields (all are already-encoded strings, compared against a project *dir name*):

    - ``exact`` — the main repo root's encoded dir; matches the root's own sessions.
    - ``under_root_prefix`` — ``exact + "-"``; matches every dir *under* the root, which
      covers all ``.worktrees/*`` worktrees (they encode as ``exact + "--worktrees-…"``).
    - ``legacy_prefix`` — encodes the legacy ``<parent>/<name>-worktrees/`` location (a
      sibling of the root, outside it) plus ``"-"``; matches worktrees kept there.

    ``under_root_prefix`` is a broad net: as a pure prefix it also catches sibling repos like
    ``<name>-extra`` and even the legacy dir names — those false positives are eliminated later
    by confirming each session's real ``cwd`` field, never here.
    """

    exact: str
    under_root_prefix: str
    legacy_prefix: str

    def matches(self, project_dir_name: str) -> bool:
        """True if a project-dir NAME could belong to this repo (cheap pre-filter only)."""
        return (
            project_dir_name == self.exact
            or project_dir_name.startswith(self.under_root_prefix)
            or project_dir_name.startswith(self.legacy_prefix)
        )


def _parse_worktree_list(porcelain_text: str) -> list[Worktree]:
    """Parse ``git worktree list --porcelain`` output into ``Worktree`` entries.

    Blocks are separated by blank lines; each line is ``key value`` or the bare word
    ``detached``. The first entry is always the main repo root.
    """
    worktrees: list[Worktree] = []
    current: dict[str, str] = {}

    def flush() -> None:
        if "worktree" in current:
            worktrees.append(
                Worktree(
                    path=Path(current["worktree"]),
                    head=current.get("HEAD"),
                    branch=current.get("branch"),
                    detached="detached" in current,
                )
            )
        current.clear()

    for line in porcelain_text.splitlines():
        if not line.strip():
            flush()
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    flush()
    return worktrees


def _run_worktree_list(cwd: str | Path) -> str | None:
    """Shell out to ``git worktree list --porcelain``; ``None`` if not a git repo."""
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=os.fspath(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def main_repo_root(cwd: str | Path) -> Path | None:
    """Return the main repo root for ``cwd``, or ``None`` if not inside a git repo."""
    porcelain = _run_worktree_list(cwd)
    if porcelain is None:
        return None
    worktrees = _parse_worktree_list(porcelain)
    if not worktrees:
        return None
    return worktrees[0].path


def is_git_repo(cwd: str | Path) -> bool:
    """True if ``cwd`` is inside a git repo."""
    return main_repo_root(cwd) is not None


def worktree_dir_patterns(root: Path) -> WorktreeDirPatterns:
    """Build the encoded-name matchers for a main repo ``root`` (see ``WorktreeDirPatterns``)."""
    root = Path(root)
    exact = encode_path(root)
    legacy_root = root.parent / f"{root.name}-worktrees"
    return WorktreeDirPatterns(
        exact=exact,
        under_root_prefix=exact + "-",
        legacy_prefix=encode_path(legacy_root) + "-",
    )
