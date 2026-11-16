from __future__ import annotations

import subprocess
from pathlib import Path

from cc_session_tools.gitrepo import GitLocation, _parse_worktree_list, git_location, main_repo_root

_ROOT = "/home/u/devel/libs/github.com/u/metabasecli"

_PORCELAIN = f"""\
worktree {_ROOT}
HEAD abcdef1234567890abcdef1234567890abcdef12
branch refs/heads/main

worktree {_ROOT}/.worktrees/fp-timezones-cleanup
HEAD 1111111111111111111111111111111111111111
branch refs/heads/fp-timezones-cleanup

worktree {_ROOT}/.worktrees/detached-experiment
HEAD 2222222222222222222222222222222222222222
detached
"""


def test_parse_worktree_list_first_entry_is_main_root() -> None:
    entries = _parse_worktree_list(_PORCELAIN)
    assert entries[0].path == Path(_ROOT)
    assert entries[0].branch == "refs/heads/main"
    assert entries[0].detached is False


def test_parse_worktree_list_handles_worktree_block() -> None:
    entries = _parse_worktree_list(_PORCELAIN)
    assert entries[1].path == Path(_ROOT) / ".worktrees" / "fp-timezones-cleanup"
    assert entries[1].branch == "refs/heads/fp-timezones-cleanup"
    assert entries[1].detached is False


def test_parse_worktree_list_handles_detached_block() -> None:
    entries = _parse_worktree_list(_PORCELAIN)
    assert len(entries) == 3
    assert entries[2].path == Path(_ROOT) / ".worktrees" / "detached-experiment"
    assert entries[2].detached is True
    assert entries[2].branch is None


def test_parse_worktree_list_empty() -> None:
    assert _parse_worktree_list("") == []


def test_main_repo_root_real_git_init(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    assert main_repo_root(sub) == repo.resolve()


def test_main_repo_root_none_outside_repo(tmp_path: Path) -> None:
    assert main_repo_root(tmp_path) is None


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True)


def test_git_location_of_a_worktree_names_the_main_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    _git("worktree", "add", "-q", "-b", "fp/x", str(repo / ".worktrees" / "fp-x"), cwd=repo)
    sub = repo / ".worktrees" / "fp-x" / "src"
    sub.mkdir()

    location = git_location(sub)

    assert location == GitLocation(
        worktree_root=(repo / ".worktrees" / "fp-x").resolve(), repo_root=repo.resolve(), branch="fp/x"
    )


def test_git_location_detached_and_unborn_have_no_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    unborn = git_location(repo)
    assert unborn is not None and unborn.branch is None and unborn.repo_root == repo.resolve()
    _git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    _git("checkout", "-q", "--detach", cwd=repo)
    detached = git_location(repo)
    assert detached is not None and detached.branch is None


def test_git_location_outside_a_repo_or_of_a_missing_dir_is_none(tmp_path: Path) -> None:
    assert git_location(tmp_path) is None
    assert git_location(tmp_path / "missing") is None
