"""Git repo and worktree resolution for scoping sessions to the current repo.

The main repo root comes from ``git worktree list --porcelain``: its first block is always the main root, even when
invoked from inside a worktree.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Worktree:
    """One entry from ``git worktree list --porcelain``."""

    path: Path
    head: str | None = None
    branch: str | None = None
    detached: bool = False


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
