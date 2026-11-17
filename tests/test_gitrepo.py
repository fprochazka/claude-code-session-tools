from __future__ import annotations

from pathlib import Path

import pytest
from conftest import git

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
    git("init", "-q", cwd=repo)
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    assert main_repo_root(sub) == repo.resolve()


def test_main_repo_root_none_outside_repo(tmp_path: Path) -> None:
    assert main_repo_root(tmp_path) is None


def test_git_location_of_a_worktree_names_the_main_checkout(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    git("worktree", "add", "-q", "-b", "fp/x", str(repo / ".worktrees" / "fp-x"), cwd=repo)
    sub = repo / ".worktrees" / "fp-x" / "src"
    sub.mkdir()

    location = git_location(sub)

    assert location == GitLocation(
        worktree_root=(repo / ".worktrees" / "fp-x").resolve(), repo_root=repo.resolve(), branch="fp/x"
    )


def test_git_location_detached_and_unborn_have_no_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", cwd=repo)
    unborn = git_location(repo)
    assert unborn is not None and unborn.branch is None and unborn.repo_root == repo.resolve()
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    git("checkout", "-q", "--detach", cwd=repo)
    detached = git_location(repo)
    assert detached is not None and detached.branch is None


def test_git_location_outside_a_repo_or_of_a_missing_dir_is_none(tmp_path: Path) -> None:
    assert git_location(tmp_path) is None
    assert git_location(tmp_path / "missing") is None


def _snapshot(git_dir: Path) -> dict[str, bytes]:
    return {str(path.relative_to(git_dir)): path.read_bytes() for path in git_dir.rglob("*") if path.is_file()}


@pytest.fixture
def inherited_git_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, bytes]:
    """A dummy repo that GIT_DIR, GIT_WORK_TREE and GIT_INDEX_FILE name, as in a hook or a ``git rebase --exec``.

    Returns the dummy's ``.git`` as it was before the test, to compare it against afterwards.
    """
    dummy = tmp_path / "dummy"
    dummy.mkdir()
    git("init", "-q", "-b", "dummy", cwd=dummy)
    git("commit", "-q", "--allow-empty", "-m", "dummy", cwd=dummy)
    monkeypatch.setenv("GIT_DIR", str(dummy / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(dummy))
    monkeypatch.setenv("GIT_INDEX_FILE", str(dummy / ".git" / "index"))
    return _snapshot(dummy / ".git")


def test_git_in_a_test_leaves_the_repo_of_an_inherited_git_dir_alone(
    tmp_path: Path, inherited_git_dir: dict[str, bytes]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
    git("worktree", "add", "-q", "-b", "fp/x", str(repo / ".worktrees" / "fp-x"), cwd=repo)

    assert _snapshot(tmp_path / "dummy" / ".git") == inherited_git_dir
    assert (repo / ".git" / "refs" / "heads" / "fp" / "x").is_file()


def test_git_location_answers_for_its_dir_whatever_repo_git_dir_names(
    tmp_path: Path, inherited_git_dir: dict[str, bytes]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    git("init", "-q", "-b", "main", cwd=repo)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)

    assert git_location(repo) == GitLocation(worktree_root=repo.resolve(), repo_root=repo.resolve(), branch="main")
    assert main_repo_root(repo) == repo.resolve()
    assert git_location(tmp_path) is None
    assert _snapshot(tmp_path / "dummy" / ".git") == inherited_git_dir
