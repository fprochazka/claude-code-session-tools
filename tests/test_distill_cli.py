from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

import cc_session_tools.discovery as discovery
from cc_session_tools.cli import app
from cc_session_tools.paths import encode_path

runner = CliRunner()

CWD = "/home/u/plain-project"
OLD_ID = "aaaaaaaa-0000-0000-0000-000000000001"
NEW_ID = "bbbbbbbb-0000-0000-0000-000000000002"


def _write_session(project_dir: Path, session_id: str, *, cwd: str, ts: str, text: str) -> Path:
    """Write a one-prompt session ``.jsonl`` carrying a real cwd and timestamp."""
    project_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "user",
        "cwd": cwd,
        "timestamp": ts,
        "sessionId": session_id,
        "message": {"role": "user", "content": text},
    }
    path = project_dir / f"{session_id}.jsonl"
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _write_stub_session(project_dir: Path, session_id: str) -> Path:
    """Write a stub transcript: a bridge-session marker, no timestamp, no prompt."""
    project_dir.mkdir(parents=True, exist_ok=True)
    path = project_dir / f"{session_id}.jsonl"
    path.write_text(json.dumps({"type": "bridge-session", "sessionId": session_id}) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def tree(tmp_path, monkeypatch):
    """A fake, non-git projects tree with two sessions whose mtime order is inverted.

    The older *transcript* is touched last, so any implementation that ranks by file
    mtime would pick the wrong session for ``latest``.
    """
    projects = tmp_path / "projects"
    project_dir = projects / encode_path(CWD)

    old = _write_session(project_dir, OLD_ID, cwd=CWD, ts="2026-07-24T10:00:00.000Z", text="the older prompt")
    new = _write_session(project_dir, NEW_ID, cwd=CWD, ts="2026-07-24T12:00:00.000Z", text="the newer prompt")

    # Invert the mtime order: the transcript-older session becomes the file-newest.
    os.utime(new, (1_000_000, 1_000_000))
    os.utime(old, (2_000_000, 2_000_000))

    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(os, "getcwd", lambda: CWD)
    monkeypatch.setattr(discovery, "main_repo_root", lambda _cwd: None)
    return projects


def test_distill_by_path_writes_a_file_and_prints_only_the_path(tree, tmp_path):
    session = tree / encode_path(CWD) / f"{NEW_ID}.jsonl"
    out_dir = tmp_path / "out"

    result = runner.invoke(app, ["distill", str(session), "--output-dir", str(out_dir)])

    assert result.exit_code == 0, result.output
    # Stdout is exactly one line: the path of the written file, nothing else.
    lines = result.stdout.splitlines()
    assert len(lines) == 1, result.stdout
    written = Path(lines[0])
    assert written == (out_dir / f"{NEW_ID}.md").resolve()
    assert written.is_file()
    body = written.read_text(encoding="utf-8")
    assert body.startswith(f"# Session {NEW_ID}")
    assert "## 👤 User" in body
    assert "the newer prompt" in body


def test_stats_table_goes_to_stderr_in_file_mode(tree, tmp_path):
    session = tree / encode_path(CWD) / f"{NEW_ID}.jsonl"
    result = runner.invoke(app, ["distill", str(session), "--output-dir", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    assert "Transcript stats" in result.stderr
    assert "Transcript stats" not in result.stdout


def test_distill_by_uuid_finds_the_session_across_project_dirs(tree, tmp_path):
    result = runner.invoke(app, ["distill", OLD_ID, "--output-dir", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    assert Path(result.stdout.strip()).name == f"{OLD_ID}.md"
    assert "the older prompt" in Path(result.stdout.strip()).read_text(encoding="utf-8")


def test_uuid_lookup_never_reaches_a_subagent_sidecar(tree, tmp_path):
    sidecar_id = "cccccccc-0000-0000-0000-000000000003"
    _write_session(
        tree / encode_path(CWD) / NEW_ID / "subagents",
        sidecar_id,
        cwd=CWD,
        ts="2026-07-24T13:00:00.000Z",
        text="subagent prompt",
    )
    result = runner.invoke(app, ["distill", sidecar_id, "--output-dir", str(tmp_path / "out")])
    assert result.exit_code == 1
    assert f"could not resolve session target: {sidecar_id}" in result.stderr


def test_latest_picks_the_newest_transcript_timestamp_not_the_newest_mtime(tree, tmp_path):
    result = runner.invoke(app, ["distill", "latest", "--output-dir", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    assert Path(result.stdout.strip()).name == f"{NEW_ID}.md"


def test_latest_honors_the_worktree_scope_across_two_encoded_dirs(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    root = Path("/home/u/repo")
    worktree = root / ".worktrees" / "fp-foo"
    outsider = Path("/home/u/unrelated")

    _write_session(
        projects / encode_path(root), OLD_ID, cwd=str(root), ts="2026-07-24T10:00:00.000Z", text="main checkout"
    )
    _write_session(
        projects / encode_path(worktree),
        NEW_ID,
        cwd=str(worktree),
        ts="2026-07-24T12:00:00.000Z",
        text="worktree checkout",
    )
    # Newest of all, but outside the repo: it must never win `latest`.
    _write_session(
        projects / encode_path(outsider),
        "dddddddd-0000-0000-0000-000000000004",
        cwd=str(outsider),
        ts="2026-07-24T23:00:00.000Z",
        text="someone else's session",
    )

    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setattr(os, "getcwd", lambda: str(root))
    monkeypatch.setattr(discovery, "main_repo_root", lambda _cwd: root)

    result = runner.invoke(app, ["distill", "latest", "--output-dir", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    # The worktree's session is the repo's newest; the unrelated newer one is ignored.
    assert Path(result.stdout.strip()).name == f"{NEW_ID}.md"


def test_latest_in_an_empty_scope_fails_instead_of_falling_back(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    # A session exists, but it belongs to another project: `latest` must not reach it.
    _write_session(
        projects / encode_path("/home/u/other"),
        NEW_ID,
        cwd="/home/u/other",
        ts="2026-07-24T12:00:00.000Z",
        text="not yours",
    )
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setattr(os, "getcwd", lambda: "/home/u/empty-project")
    monkeypatch.setattr(discovery, "main_repo_root", lambda _cwd: None)

    result = runner.invoke(app, ["distill", "latest"])
    assert result.exit_code == 1
    assert "could not resolve session target: latest" in result.stderr
    assert result.stdout == ""


def test_unresolvable_target_reports_plainly(tree):
    result = runner.invoke(app, ["distill", "not-a-session"])
    assert result.exit_code == 1
    assert result.stderr.strip() == "Error: could not resolve session target: not-a-session"
    assert result.stdout == ""


def test_stdout_mode_emits_markdown_only(tree):
    session = tree / encode_path(CWD) / f"{NEW_ID}.jsonl"
    result = runner.invoke(app, ["distill", str(session), "--stdout"])
    assert result.exit_code == 0, result.output
    assert result.stdout.startswith(f"# Session {NEW_ID}")
    assert "Transcript stats" not in result.stdout
    assert "Transcript stats" in result.stderr


def test_default_output_dir_is_not_used_when_output_dir_is_given(tree, tmp_path):
    out_dir = tmp_path / "custom" / "nested"
    session = tree / encode_path(CWD) / f"{NEW_ID}.jsonl"
    result = runner.invoke(app, ["distill", str(session), "--output-dir", str(out_dir)])
    assert result.exit_code == 0, result.output
    # The directory is created on demand and the file is named after the session id.
    assert (out_dir / f"{NEW_ID}.md").is_file()
    assert result.stdout.strip() == str((out_dir / f"{NEW_ID}.md").resolve())


def test_latest_skips_a_stub_transcript_that_is_newest_by_mtime(tmp_path, monkeypatch):
    """A bridge-session stub has no timestamp, so its last_ts is the file mtime.

    Given the newest mtime in the scope it would otherwise win `latest` and distill to
    an empty document. The newest session with a real timestamp must win instead.
    """
    projects = tmp_path / "projects"
    project_dir = projects / encode_path(CWD)

    real = _write_session(project_dir, NEW_ID, cwd=CWD, ts="2026-07-24T12:00:00.000Z", text="a real prompt")
    stub = _write_stub_session(project_dir, "eeeeeeee-0000-0000-0000-000000000005")

    # The stub is the file-newest by a wide margin; the real session is older on disk.
    os.utime(real, (1_000_000, 1_000_000))
    os.utime(stub, (2_000_000, 2_000_000))

    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.setattr(os, "getcwd", lambda: CWD)
    monkeypatch.setattr(discovery, "main_repo_root", lambda _cwd: None)

    result = runner.invoke(app, ["distill", "latest", "--output-dir", str(tmp_path / "out")])
    assert result.exit_code == 0, result.output
    assert Path(result.stdout.strip()).name == f"{NEW_ID}.md"
    assert "a real prompt" in Path(result.stdout.strip()).read_text(encoding="utf-8")

    # The stub is still listed by `search` — only `latest` skips it.
    listing = runner.invoke(app, ["search"])
    assert listing.exit_code == 0, listing.output
    assert "eeeeeeee-0000-0000-0000-000000000005" in listing.output


def test_latest_fails_when_the_scope_holds_only_stubs(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    _write_stub_session(projects / encode_path(CWD), "eeeeeeee-0000-0000-0000-000000000005")

    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setattr(os, "getcwd", lambda: CWD)
    monkeypatch.setattr(discovery, "main_repo_root", lambda _cwd: None)

    result = runner.invoke(app, ["distill", "latest"])
    assert result.exit_code == 1
    assert "could not resolve session target: latest" in result.stderr
    assert result.stdout == ""
