from __future__ import annotations

from pathlib import Path

import pytest

from cc_session_tools.paths import encode_path, resolve_projects_dir

_BASE = "/home/u/devel/libs/github.com/u/metabasecli"


@pytest.mark.parametrize(
    ("abs_path", "expected"),
    [
        (_BASE, "-home-u-devel-libs-github-com-u-metabasecli"),
        (
            _BASE + "/.worktrees/fp-timezones-cleanup",
            "-home-u-devel-libs-github-com-u-metabasecli--worktrees-fp-timezones-cleanup",
        ),
        ("/home/u/.claude", "-home-u--claude"),
    ],
)
def test_encode_path_concrete_cases(abs_path: str, expected: str) -> None:
    assert encode_path(abs_path) == expected


def test_encode_path_strips_trailing_slash() -> None:
    assert encode_path(_BASE + "/") == encode_path(_BASE)


def test_encode_path_accepts_path_object() -> None:
    assert encode_path(Path(_BASE)) == encode_path(_BASE)


def test_resolve_projects_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLAUDE_PROJECTS_DIR", raising=False)
    assert resolve_projects_dir() == Path("~/.claude/projects").expanduser()


def test_resolve_projects_dir_honors_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path))
    assert resolve_projects_dir() == tmp_path
