"""Git repo and worktree resolution for scoping sessions to the current repo.

The main repo root comes from ``git worktree list --porcelain``: its first block is always the main root, even when
invoked from inside a worktree.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Variables that tell git which repo to use whatever its cwd, as ``git rev-parse --local-env-vars`` lists them, less the
# config ones. Inherited from a git hook or ``git rebase --exec``, they would make every call below answer for that repo
# instead of the dir it asks about.
_REPO_ENV = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_COMMON_DIR",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_GRAFT_FILE",
    "GIT_SHALLOW_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_PREFIX",
)


def _git(args: list[str], cwd: str | Path) -> subprocess.CompletedProcess[str]:
    """Run git about the repo of ``cwd``, whatever repo the environment names."""
    env = {name: value for name, value in os.environ.items() if name not in _REPO_ENV}
    return subprocess.run(["git", *args], cwd=os.fspath(cwd), capture_output=True, text=True, check=False, env=env)


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
        result = _git(["worktree", "list", "--porcelain"], cwd)
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


@dataclass(frozen=True)
class GitLocation:
    """Where a directory sits in git: its checkout, the repo that checkout belongs to, and the branch it has out."""

    worktree_root: Path
    """The top of the checkout holding the directory: the main checkout or a linked worktree."""
    repo_root: Path
    """The main checkout of the repo, the same for every worktree of it."""
    branch: str | None
    """The short branch name, or ``None`` for a detached HEAD or an unborn branch."""


def _rev_parse(cwd: str | Path, *args: str) -> list[str] | None:
    try:
        result = _git(["rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir", *args], cwd)
    except (OSError, ValueError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.splitlines()


def git_location(cwd: str | Path) -> GitLocation | None:
    """Return where ``cwd`` sits in git, or ``None`` when it is not in a work tree, does not exist, or git is missing.

    One ``git rev-parse`` call. An unborn branch makes ``--abbrev-ref HEAD`` fail, so only then a second call runs
    without it.
    """
    lines = _rev_parse(cwd, "--abbrev-ref", "HEAD")
    branch: str | None = None
    if lines is not None and len(lines) >= 3:
        branch = lines[2] if lines[2] != "HEAD" else None
    else:
        lines = _rev_parse(cwd)
        if lines is None or len(lines) < 2:
            return None
    worktree_root, common_dir = Path(lines[0]), Path(lines[1])
    # A non-bare repo keeps its common dir at <main checkout>/.git. Anything else, such as a submodule's
    # .git/modules/<name>, names no main checkout, so the checkout itself stands in for it.
    repo_root = common_dir.parent if common_dir.name == ".git" else worktree_root
    return GitLocation(worktree_root=worktree_root, repo_root=repo_root, branch=branch)
