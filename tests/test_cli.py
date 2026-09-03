from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

import cc_session_tools.discovery as discovery
from cc_session_tools.cli import app
from cc_session_tools.paths import encode_path

runner = CliRunner()


def _write_session(project_dir: Path, session_id: str, *, cwd: str, ts: str, records: list[dict]) -> None:
    """Write a session ``.jsonl`` whose records already carry ``cwd`` and ``timestamp``."""
    project_dir.mkdir(parents=True, exist_ok=True)
    lines = []
    for record in records:
        record = {"cwd": cwd, "timestamp": ts, **record}
        lines.append(json.dumps(record))
    (project_dir / f"{session_id}.jsonl").write_text("\n".join(lines) + "\n")


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A fake, non-git projects tree wired through env + patched cwd/repo-root.

    Outside a git repo (D5) discovery only considers the exact ``encode_path(cwd)``
    dir, so a single project dir holds every fixture session.
    """
    projects = tmp_path / "projects"
    cwd = "/home/u/plain-project"
    project_dir = projects / encode_path(cwd)

    _write_session(
        project_dir,
        "alpha",
        cwd=cwd,
        ts="2026-07-24T12:00:00.000Z",
        records=[
            {"type": "user", "message": {"role": "user", "content": "start alpha"}},
            {"type": "ai-title", "aiTitle": "Alpha genuine name"},
        ],
    )
    _write_session(
        project_dir,
        "bravo",
        cwd=cwd,
        ts="2026-07-24T11:00:00.000Z",
        records=[
            {"type": "user", "message": {"role": "user", "content": "Fix the parser bug UNIQUEMARKER"}},
        ],
    )
    _write_session(
        project_dir,
        "charlie",
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


def test_lists_sessions_newest_first(tree):
    result = runner.invoke(app, ["search"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert out.index("alpha") < out.index("bravo") < out.index("charlie")


def test_limit_caps_rows(tree):
    result = runner.invoke(app, ["search", "-n", "1"])
    assert result.exit_code == 0, result.output
    assert "alpha" in result.output
    assert "bravo" not in result.output
    assert "charlie" not in result.output


def test_limit_zero_means_unlimited(tree):
    result = runner.invoke(app, ["search", "--limit", "0"])
    assert result.exit_code == 0, result.output
    for session_id in ("alpha", "bravo", "charlie"):
        assert session_id in result.output


def test_derived_name_is_shown(tree):
    result = runner.invoke(app, ["search"])
    assert result.exit_code == 0, result.output
    # The derived title comes from bravo's first user message via clean_title.
    assert "Fix the parser bug" in result.output
    # Genuine ai-title names are shown too.
    assert "Alpha genuine name" in result.output


def test_empty_result_prints_friendly_message(tree, monkeypatch):
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


def test_table_is_never_narrower_than_the_minimum(tree, monkeypatch):
    # A narrow terminal — and the 80-column fallback rich uses when piped — would
    # otherwise wrap the cells into unreadable stacks.
    monkeypatch.setenv("COLUMNS", "80")
    result = runner.invoke(app, ["search"])
    assert result.exit_code == 0, result.output
    assert _widest_line(result.output) >= 210


def test_table_uses_the_full_width_of_a_wide_terminal(tree, monkeypatch):
    monkeypatch.setenv("COLUMNS", "300")
    result = runner.invoke(app, ["search"])
    assert result.exit_code == 0, result.output
    assert _widest_line(result.output) > 210


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_rg_narrows_to_matching_sessions(tree):
    result = runner.invoke(app, ["search", "--rg", "UNIQUEMARKER"])
    assert result.exit_code == 0, result.output
    assert "bravo" in result.output
    assert "alpha" not in result.output
    assert "charlie" not in result.output


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_repeated_rg_requires_every_keyword(tree):
    # "start" alone covers alpha and charlie; adding "charlie" narrows to the one session with both.
    both = runner.invoke(app, ["search", "--rg", "start", "--rg", "charlie"])
    assert both.exit_code == 0, both.output
    assert "charlie" in both.output
    assert "alpha" not in both.output
    assert "bravo" not in both.output


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_rg_any_widens_repeated_keywords_to_a_union(tree):
    # The same keywords the AND test narrows to charlie alone: under OR, alpha (only "start") joins it.
    result = runner.invoke(app, ["search", "--rg", "start", "--rg", "charlie", "--rg-any"])
    assert result.exit_code == 0, result.output
    assert "charlie" in result.output
    assert "alpha" in result.output
    assert "bravo" not in result.output


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_rg_any_without_keywords_is_a_no_op(tree):
    result = runner.invoke(app, ["search", "--rg-any"])
    assert result.exit_code == 0, result.output
    for session_id in ("alpha", "bravo", "charlie"):
        assert session_id in result.output


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_rg_any_no_match_names_the_or_semantics(tree):
    result = runner.invoke(app, ["search", "--rg", "no-such-text-anywhere-xyzzy", "--rg-any"])
    assert result.exit_code == 0, result.output
    assert "matching any of" in result.output
    assert "no-such-text-anywhere-xyzzy" in result.output


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_repeated_rg_with_an_unmatched_keyword_yields_nothing(tree):
    result = runner.invoke(app, ["search", "--rg", "UNIQUEMARKER", "--rg", "no-such-text-anywhere-xyzzy"])
    assert result.exit_code == 0, result.output
    assert "No sessions found" in result.output
    assert "UNIQUEMARKER" in result.output
    assert "no-such-text-anywhere-xyzzy" in result.output


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_rg_no_match_prints_friendly_message(tree):
    result = runner.invoke(app, ["search", "--rg", "no-such-text-anywhere-xyzzy"])
    assert result.exit_code == 0, result.output
    assert "No sessions found" in result.output
    assert "no-such-text-anywhere-xyzzy" in result.output


@pytest.fixture
def two_project_tree(tmp_path, monkeypatch):
    """Two unrelated project dirs, neither of them the (patched) cwd.

    ``--all`` must reach both even though normal discovery, anchored on the cwd,
    would reach neither.
    """
    projects = tmp_path / "projects"
    cwd = "/home/u/somewhere-else"

    _write_session(
        projects / encode_path("/home/u/first-project"),
        "aaaaaaaa-0000-0000-0000-000000000001",
        cwd="/home/u/first-project",
        ts="2026-07-24T12:00:00.000Z",
        records=[{"type": "ai-title", "aiTitle": "First project session"}],
    )
    _write_session(
        projects / encode_path("/home/u/second-project"),
        "bbbbbbbb-0000-0000-0000-000000000002",
        cwd="/home/u/second-project",
        ts="2026-07-24T11:00:00.000Z",
        records=[{"type": "ai-title", "aiTitle": "Second project session"}],
    )
    # A subagent sidecar under a session dir: never a session, never listed.
    sidecar = projects / encode_path("/home/u/first-project") / "aaaaaaaa-0000-0000-0000-000000000001" / "subagents"
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


def test_all_lists_sessions_from_every_project(two_project_tree):
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


def test_without_all_the_same_tree_yields_nothing(two_project_tree):
    # Same fixture, no --all: the cwd matches no project dir, so scope is empty.
    result = runner.invoke(app, ["search"])
    assert result.exit_code == 0, result.output
    assert "No sessions found" in result.stderr
    assert "First project session" not in result.output
