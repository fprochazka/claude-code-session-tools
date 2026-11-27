from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from cc_blackbox import project_slug
from conftest import sid
from typer.testing import CliRunner

import cc_session_tools.discovery as discovery
from cc_session_tools.cli import app
from cc_session_tools.watch_events import parse_since

ALPHA, BRAVO, CHARLIE = sid("alpha"), sid("bravo"), sid("charlie")

runner = CliRunner()


def _write_session(project_dir: Path, session_id: str, *, cwd: str, ts: str, records: list[dict[str, Any]]) -> None:
    """Write a session ``.jsonl`` whose records already carry ``cwd`` and ``timestamp``."""
    project_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for record in records:
        record = {"cwd": cwd, "timestamp": ts, **record}
        lines.append(json.dumps(record))
    (project_dir / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n")


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake, non-git projects tree wired through env + patched cwd/repo-root.

    Outside a git repo (D5) discovery only considers the exact ``project_slug(cwd)``
    dir, so a single project dir holds every fixture session.
    """
    projects = tmp_path / "projects"
    cwd = "/home/u/plain-project"
    project_dir = projects / project_slug(cwd)

    _write_session(
        project_dir,
        ALPHA,
        cwd=cwd,
        ts="2026-07-24T12:00:00.000Z",
        records=[
            {"type": "user", "message": {"role": "user", "content": "start alpha"}},
            {"type": "ai-title", "aiTitle": "Alpha genuine name"},
        ],
    )
    _write_session(
        project_dir,
        BRAVO,
        cwd=cwd,
        ts="2026-07-24T11:00:00.000Z",
        records=[
            {"type": "user", "message": {"role": "user", "content": "Fix the parser bug UNIQUEMARKER"}},
        ],
    )
    _write_session(
        project_dir,
        CHARLIE,
        cwd=cwd,
        ts="2026-07-24T10:00:00.000Z",
        records=[
            {"type": "user", "message": {"role": "user", "content": "start charlie"}},
            {"type": "ai-title", "aiTitle": "Charlie genuine name"},
        ],
    )

    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(os, "getcwd", lambda: cwd)
    monkeypatch.setattr(discovery, "main_repo_root", lambda _cwd: None)
    return projects


def test_lists_sessions_newest_first(tree: Path) -> None:
    result = runner.invoke(app, ["search"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.index(ALPHA) < out.index(BRAVO) < out.index(CHARLIE)


def test_limit_caps_rows(tree: Path) -> None:
    result = runner.invoke(app, ["search", "-n", "1"])
    assert result.exit_code == 0, result.output
    assert ALPHA in result.output
    assert BRAVO not in result.output
    assert CHARLIE not in result.output


def test_limit_zero_means_unlimited(tree: Path) -> None:
    result = runner.invoke(app, ["search", "--limit", "0"])
    assert result.exit_code == 0, result.output
    for session_id in (ALPHA, BRAVO, CHARLIE):
        assert session_id in result.output


def test_derived_name_is_shown(tree: Path) -> None:
    result = runner.invoke(app, ["search"])
    assert result.exit_code == 0, result.output
    # The derived title comes from bravo's first user message via clean_title.
    assert "Fix the parser bug" in result.output
    # Genuine ai-title names are shown too.
    assert "Alpha genuine name" in result.output


def test_empty_result_prints_friendly_message(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point at an empty projects tree so no sessions are discovered.
    empty = tree.parent / "empty-projects"
    empty.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(empty))
    result = runner.invoke(app, ["search"])
    assert result.exit_code == 0, result.output
    # The notice belongs on stderr so a piped stdout stays either a table or empty.
    assert "No sessions found" in result.stderr
    assert result.stdout == ""


def _widest_line(output: str) -> int:
    return max((len(line) for line in output.splitlines()), default=0)


def test_table_is_never_narrower_than_the_minimum(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A narrow terminal — and the 80-column fallback rich uses when piped — would
    # otherwise wrap the cells into unreadable stacks.
    monkeypatch.setenv("COLUMNS", "80")
    result = runner.invoke(app, ["search"])
    assert result.exit_code == 0, result.output
    assert _widest_line(result.output) >= 210


def test_table_uses_the_full_width_of_a_wide_terminal(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COLUMNS", "300")
    result = runner.invoke(app, ["search"])
    assert result.exit_code == 0, result.output
    assert _widest_line(result.output) > 210


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_rg_narrows_to_matching_sessions(tree: Path) -> None:
    result = runner.invoke(app, ["search", "--rg", "UNIQUEMARKER"])
    assert result.exit_code == 0, result.output
    assert BRAVO in result.output
    assert ALPHA not in result.output
    assert CHARLIE not in result.output


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_repeated_rg_requires_every_keyword(tree: Path) -> None:
    # "start" alone covers alpha and charlie; adding "charlie" narrows to the one session with both.
    both = runner.invoke(app, ["search", "--rg", "start", "--rg", "charlie"])
    assert both.exit_code == 0, both.output
    assert CHARLIE in both.output
    assert ALPHA not in both.output
    assert BRAVO not in both.output


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_rg_any_widens_repeated_keywords_to_a_union(tree: Path) -> None:
    # The same keywords the AND test narrows to charlie alone: under OR, alpha (only "start") joins it.
    result = runner.invoke(app, ["search", "--rg", "start", "--rg", "charlie", "--rg-any"])
    assert result.exit_code == 0, result.output
    assert CHARLIE in result.output
    assert ALPHA in result.output
    assert BRAVO not in result.output


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_rg_any_without_keywords_is_a_no_op(tree: Path) -> None:
    result = runner.invoke(app, ["search", "--rg-any"])
    assert result.exit_code == 0, result.output
    for session_id in (ALPHA, BRAVO, CHARLIE):
        assert session_id in result.output


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_rg_any_no_match_names_the_or_semantics(tree: Path) -> None:
    result = runner.invoke(app, ["search", "--rg", "no-such-text-anywhere-xyzzy", "--rg-any"])
    assert result.exit_code == 0, result.output
    assert "matching any of" in result.output
    assert "no-such-text-anywhere-xyzzy" in result.output


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_repeated_rg_with_an_unmatched_keyword_yields_nothing(tree: Path) -> None:
    result = runner.invoke(app, ["search", "--rg", "UNIQUEMARKER", "--rg", "no-such-text-anywhere-xyzzy"])
    assert result.exit_code == 0, result.output
    assert "No sessions found" in result.output
    assert "UNIQUEMARKER" in result.output
    assert "no-such-text-anywhere-xyzzy" in result.output


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_rg_no_match_prints_friendly_message(tree: Path) -> None:
    result = runner.invoke(app, ["search", "--rg", "no-such-text-anywhere-xyzzy"])
    assert result.exit_code == 0, result.output
    assert "No sessions found" in result.output
    assert "no-such-text-anywhere-xyzzy" in result.output


@pytest.fixture
def two_project_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Two unrelated project dirs, neither of them the (patched) cwd.

    ``--all`` must reach both even though normal discovery, anchored on the cwd,
    would reach neither.
    """
    projects = tmp_path / "projects"
    cwd = "/home/u/somewhere-else"

    _write_session(
        projects / project_slug("/home/u/first-project"),
        "aaaaaaaa-0000-0000-0000-000000000001",
        cwd="/home/u/first-project",
        ts="2026-07-24T12:00:00.000Z",
        records=[{"type": "ai-title", "aiTitle": "First project session"}],
    )
    _write_session(
        projects / project_slug("/home/u/second-project"),
        "bbbbbbbb-0000-0000-0000-000000000002",
        cwd="/home/u/second-project",
        ts="2026-07-24T11:00:00.000Z",
        records=[{"type": "ai-title", "aiTitle": "Second project session"}],
    )
    # A subagent sidecar under a session dir: never a session, never listed.
    sidecar = projects / project_slug("/home/u/first-project") / "aaaaaaaa-0000-0000-0000-000000000001" / "subagents"
    _write_session(
        sidecar,
        "agent-deadbeef",
        cwd="/home/u/first-project",
        ts="2026-07-24T13:00:00.000Z",
        records=[{"type": "ai-title", "aiTitle": "Subagent sidecar"}],
    )

    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.setattr(os, "getcwd", lambda: cwd)
    monkeypatch.setattr(discovery, "main_repo_root", lambda _cwd: None)
    return projects


def test_all_lists_sessions_from_every_project(two_project_tree: Path) -> None:
    result = runner.invoke(app, ["search", "--all"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "First project session" in out
    assert "Second project session" in out
    # The location column is repurposed and carries each session's own cwd.
    assert "Project" in out
    assert "Worktree" not in out
    assert "/home/u/first-project" in out
    assert "/home/u/second-project" in out
    # Sidecar transcripts under <uuid>/subagents/ are not sessions.
    assert "Subagent sidecar" not in out
    assert "agent-deadbeef" not in out


def test_without_all_the_same_tree_yields_nothing(two_project_tree: Path) -> None:
    # Same fixture, no --all: the cwd matches no project dir, so scope is empty.
    result = runner.invoke(app, ["search"])
    assert result.exit_code == 0, result.output
    assert "No sessions found" in result.stderr
    assert "First project session" not in result.output


def test_rg_falls_back_to_python_when_ripgrep_is_missing(tree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    both = runner.invoke(app, ["search", "--rg", "start", "--rg", "charlie"])
    assert both.exit_code == 0, both.output
    assert CHARLIE in both.output
    assert ALPHA not in both.output
    either = runner.invoke(app, ["search", "--rg", "UNIQUEMARKER", "--rg", "charlie", "--rg-any"])
    assert either.exit_code == 0, either.output
    assert BRAVO in either.output and CHARLIE in either.output
    assert ALPHA not in either.output


def test_sessions_of_every_profile_are_listed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = Path(os.environ["HOME"])
    cwd = "/home/u/plain-project"
    shared = home / ".claude" / "projects"
    (home / ".claude" / "sessions").mkdir(parents=True)
    (home / ".claude" / "auth-switch.yaml").write_text("profiles:\n  work: {}\n  lab: {}\n")
    for name in ("work", "lab"):
        (home / ".claude-profiles" / name / "sessions").mkdir(parents=True)
    (home / ".claude-profiles" / "work" / "projects").symlink_to(shared, target_is_directory=True)
    shared.mkdir()
    lab = home / ".claude-profiles" / "lab" / "projects"
    record = {"type": "user", "message": {"role": "user", "content": "hello"}}
    _write_session(shared / project_slug(cwd), ALPHA, cwd=cwd, ts="2026-07-24T12:00:00.000Z", records=[record])
    _write_session(lab / project_slug(cwd), BRAVO, cwd=cwd, ts="2026-07-24T11:00:00.000Z", records=[dict(record)])
    monkeypatch.setenv("COLUMNS", "300")
    monkeypatch.setattr(os, "getcwd", lambda: cwd)
    monkeypatch.setattr(discovery, "main_repo_root", lambda _cwd: None)

    result = runner.invoke(app, ["search"])

    assert result.exit_code == 0, result.output
    assert ALPHA in result.output and BRAVO in result.output
    # Two projects dirs, so the profile column names where each session lives.
    assert "Profile" in result.output
    assert "default" in result.output and "lab" in result.output
    # The shared dir is listed once, under its first profile.
    assert result.output.count(ALPHA) == 1


def test_profile_column_is_hidden_for_a_single_projects_dir(tree: Path) -> None:
    result = runner.invoke(app, ["search"])
    assert result.exit_code == 0, result.output
    assert "Profile" not in result.output


def test_since_keeps_sessions_last_active_after_a_date(tree: Path) -> None:
    result = runner.invoke(app, ["search", "--since", "2026-07-24T10:30:00+00:00"])
    assert result.exit_code == 0, result.output
    assert ALPHA in result.output and BRAVO in result.output and CHARLIE not in result.output


def test_since_takes_a_duration_back_from_now(tree: Path) -> None:
    assert ALPHA in runner.invoke(app, ["search", "--since", "520w"]).output
    result = runner.invoke(app, ["search", "--since", "1d"])
    assert result.exit_code == 0 and ALPHA not in result.stdout
    assert "No sessions found for /home/u/plain-project since " in result.stderr


def test_since_that_cannot_be_read_fails(tree: Path) -> None:
    result = runner.invoke(app, ["search", "--since", "last tuesday"])
    assert result.exit_code == 1 and "--since" in result.output and "2w" in result.output


def test_json_prints_the_rows(tree: Path) -> None:
    result = runner.invoke(app, ["search", "--json", "-n", "2"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    assert [r["session_id"] for r in rows] == [ALPHA, BRAVO]
    assert set(rows[0]) == {
        "session_id", "name", "name_is_derived", "last_interaction", "last_interaction_from_transcript", "profile",
        "location", "cwd", "transcript_path",
    }  # fmt: skip
    assert (rows[0]["name"], rows[0]["name_is_derived"], rows[1]["name_is_derived"]) == (
        "Alpha genuine name",
        False,
        True,
    )
    assert rows[0]["last_interaction"] == "2026-07-24T12:00:00+00:00" and rows[0]["cwd"] == "/home/u/plain-project"


def test_json_of_nothing_is_an_empty_array(tree: Path) -> None:
    result = runner.invoke(app, ["search", "--json", "--rg", "NOTHINGMATCHESTHIS"])
    assert result.exit_code == 0 and json.loads(result.stdout) == [] and "No sessions found" in result.stderr


def test_since_is_a_duration_back_or_a_date() -> None:
    now = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
    assert parse_since("2h", now) == datetime(2026, 11, 27, 10, 0, tzinfo=UTC)
    assert parse_since("1w", now) == datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
    assert parse_since("2026-11-01T09:00:00+00:00", now) == datetime(2026, 11, 1, 9, 0, tzinfo=UTC)
    assert parse_since("2026-11-01", now) == datetime(2026, 11, 1).astimezone()  # local midnight
    with pytest.raises(ValueError, match="ISO date"):
        parse_since("soon", now)
