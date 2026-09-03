from __future__ import annotations

import subprocess
from pathlib import Path

from cc_session_tools.gitrepo import (
    _parse_worktree_list,
    main_repo_root,
    worktree_dir_patterns,
)
from cc_session_tools.paths import encode_path

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


def test_worktree_dir_patterns_fields() -> None:
    patterns = worktree_dir_patterns(Path(_ROOT))
    exact = "-home-u-devel-libs-github-com-u-metabasecli"
    assert patterns.exact == exact
    assert patterns.under_root_prefix == exact + "-"
    assert patterns.legacy_prefix == exact + "-worktrees-"


def test_worktree_dir_patterns_matches() -> None:
    patterns = worktree_dir_patterns(Path(_ROOT))

    # exact main-root dir
    assert patterns.matches(encode_path(_ROOT))
    # a live-or-deleted .worktrees/* dir (encodes as exact + "--worktrees-…")
    assert patterns.matches(encode_path(_ROOT + "/.worktrees/fp-timezones-cleanup"))
    # a plain subdir of the repo
    assert patterns.matches(encode_path(_ROOT + "/src/server"))
    # the legacy <name>-worktrees location
    assert patterns.matches(encode_path(_ROOT + "-worktrees/fp-foo"))

    # an unrelated sibling repo is NOT matched by any prefix
    assert not patterns.matches(encode_path("/home/u/devel/libs/github.com/u/other"))


def test_worktree_dir_patterns_broad_net_over_matches_decoy() -> None:
    # Documents the intended over-match: a sibling repo sharing the name prefix slips through the
    # cheap name filter and is only rejected later by the real-cwd membership check (C5).
    patterns = worktree_dir_patterns(Path(_ROOT))
    assert patterns.matches(encode_path(_ROOT + "-extra"))


def test_main_repo_root_real_git_init(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    sub = repo / "src" / "pkg"
    sub.mkdir(parents=True)
    assert main_repo_root(sub) == repo.resolve()


def test_main_repo_root_none_outside_repo(tmp_path: Path) -> None:
    assert main_repo_root(tmp_path) is None
