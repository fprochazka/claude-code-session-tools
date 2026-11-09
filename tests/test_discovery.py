from __future__ import annotations

import json
import subprocess
from pathlib import Path

from cc_blackbox import project_slug
from conftest import sid

import cc_session_tools.discovery as discovery
from cc_session_tools.discovery import build_session_rows, resolve_scope

# Fixture sessions get readable names; Claude Code names them by UUID, so each name maps to a stable one.
NAMES: dict[str, str] = {}


def _make_session(projects_dir: Path, dir_name: str, session_id: str, *, cwd: str | None, ts: str) -> Path:
    """Create a tiny one-record session ``.jsonl`` under an encoded project dir."""
    project_dir = projects_dir / dir_name
    project_dir.mkdir(parents=True, exist_ok=True)
    name, session_id = session_id, sid(session_id)
    NAMES[session_id] = name
    record: dict = {
        "type": "user",
        "sessionId": session_id,
        "timestamp": ts,
        "message": {"role": "user", "content": f"message for {session_id}"},
    }
    if cwd is not None:
        record["cwd"] = cwd
    path = project_dir / f"{session_id}.jsonl"
    path.write_text(json.dumps(record) + "\n")
    return path


def test_git_repo_matrix_includes_worktrees_excludes_decoy(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    root = Path("/home/u/metabasecli")
    monkeypatch.setattr(discovery, "main_repo_root", lambda cwd: root)

    _make_session(projects, project_slug(root), "main", cwd=str(root), ts="2026-02-13T10:00:00.000Z")
    _make_session(
        projects,
        project_slug(root / ".worktrees" / "fp-foo"),
        "live",
        cwd=str(root / ".worktrees" / "fp-foo"),
        ts="2026-02-13T12:00:00.000Z",
    )
    # Deleted worktree: dir + jsonl exist in the fake tree, but the real filesystem
    # path root/.worktrees/fp-deleted does NOT exist — we must never stat it.
    _make_session(
        projects,
        project_slug(root / ".worktrees" / "fp-deleted"),
        "deleted",
        cwd=str(root / ".worktrees" / "fp-deleted"),
        ts="2026-02-13T11:00:00.000Z",
    )
    # Decoy sibling: encoded name passes the cheap prefix filter, but its real cwd
    # is /home/u/metabasecli-extra (NOT under root) → must be rejected, even though
    # its timestamp is the newest of all.
    extra = Path("/home/u/metabasecli-extra")
    _make_session(projects, project_slug(extra), "decoy", cwd=str(extra), ts="2026-02-13T13:00:00.000Z")

    rows = build_session_rows(resolve_scope(str(root)), projects_dirs=[projects])

    assert [NAMES[row.session_id] for row in rows] == ["live", "deleted", "main"]
    by_id = {NAMES[row.session_id]: row for row in rows}
    assert by_id["main"].worktree_rel == ""
    assert by_id["live"].worktree_rel == ".worktrees/fp-foo"
    assert by_id["deleted"].worktree_rel == ".worktrees/fp-deleted"
    assert not (root / ".worktrees" / "fp-deleted").exists()


def test_legacy_worktree_is_included_and_labeled(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    root = Path("/home/u/metabasecli")
    monkeypatch.setattr(discovery, "main_repo_root", lambda cwd: root)

    legacy_cwd = root.parent / "metabasecli-worktrees" / "fp-legacy"
    _make_session(projects, project_slug(legacy_cwd), "legacy", cwd=str(legacy_cwd), ts="2026-02-13T09:00:00.000Z")

    rows = build_session_rows(resolve_scope(str(root)), projects_dirs=[projects])

    assert [NAMES[row.session_id] for row in rows] == ["legacy"]
    assert rows[0].worktree_rel == "metabasecli-worktrees/fp-legacy"


def test_git_repo_missing_cwd_falls_back_to_prefix_match(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    root = Path("/home/u/metabasecli")
    monkeypatch.setattr(discovery, "main_repo_root", lambda cwd: root)

    # Older session with no cwd field: accepted on the encoded-dir-name pre-filter
    # alone; worktree_rel defaults to "" since it can't be located.
    _make_session(projects, project_slug(root), "nocwd", cwd=None, ts="2026-02-13T08:00:00.000Z")

    rows = build_session_rows(resolve_scope(str(root)), projects_dirs=[projects])

    assert [NAMES[row.session_id] for row in rows] == ["nocwd"]
    assert rows[0].worktree_rel == ""


def test_not_a_git_repo_returns_only_exact_cwd_dir(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    monkeypatch.setattr(discovery, "main_repo_root", lambda cwd: None)

    cwd = "/home/u/plain-dir"
    exact = project_slug(cwd)

    _make_session(projects, exact, "match", cwd=cwd, ts="2026-02-13T10:00:00.000Z")
    # Same exact dir, but a real cwd that differs → excluded by the cwd check.
    _make_session(projects, exact, "wrongcwd", cwd="/somewhere/else", ts="2026-02-13T11:00:00.000Z")
    # Same exact dir, no cwd field → accepted via the exact-dir fallback.
    _make_session(projects, exact, "nocwd", cwd=None, ts="2026-02-13T09:00:00.000Z")
    # A different project dir entirely → never a candidate outside a git repo.
    _make_session(projects, project_slug("/home/u/other"), "other", cwd="/home/u/other", ts="2026-02-13T12:00:00.000Z")

    rows = build_session_rows(resolve_scope(cwd), projects_dirs=[projects])

    assert {NAMES[row.session_id] for row in rows} == {"match", "nocwd"}
    assert all(row.worktree_rel == "" for row in rows)


def test_real_git_init_sanity(tmp_path):
    """One end-to-end check that the real ``main_repo_root`` wiring resolves rows."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    repo_abs = str(repo.resolve())

    projects = tmp_path / "projects"
    _make_session(projects, project_slug(repo_abs), "s1", cwd=repo_abs, ts="2026-02-13T10:00:00.000Z")

    rows = build_session_rows(resolve_scope(repo_abs), projects_dirs=[projects])

    assert [NAMES[row.session_id] for row in rows] == ["s1"]
    assert rows[0].worktree_rel == ""
