from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from cc_blackbox import project_slug
from conftest import Machine, fake_tmux, response, sid, write_records
from typer.testing import CliRunner

from cc_session_tools.active import list_tmux_panes, pane_id_of
from cc_session_tools.cli import app
from cc_session_tools.list_active_cmd import _middle_ellipsis

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="liveness and fake processes need Linux")

runner = CliRunner()


def invoke(*args: str) -> Any:
    return runner.invoke(app, ["list-active", *args])


def one(name: str) -> Any:
    """The JSON object of the session called ``name``."""
    return next(s for s in json_of() if s["name"] == name)


def json_of(*args: str) -> Any:
    result = invoke(*args, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


# --- the table ---


def test_table_lists_every_profile_sorted_by_profile_then_start(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    machine.session("work", "work-second")
    machine.session("lab", "lab-only")
    machine.session("default", "plain")
    machine.session("work", "work-third")

    result = invoke()

    assert result.exit_code == 0, result.output
    out = result.stdout
    assert out.splitlines()[0].split()[:4] == ["Profile", "Name", "Status", "Active"]
    assert out.index("plain") < out.index("lab-only") < out.index("work-second") < out.index("work-third")


def test_table_shows_the_live_tmux_location_and_marks_a_gone_pane(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, ["%14\tmoved-here\t3\t@14\tw1 title"])
    machine.session("work", "moved", tmux="old-session:@14.%14")
    machine.session("work", "closed", tmux="old-session:@15.%15")
    machine.session("work", "no-tmux", tmux=None)

    result = invoke()

    assert result.exit_code == 0, result.output
    lines = {line.split()[1]: line for line in result.stdout.splitlines()[1:]}
    assert "%14 moved-here:3" in lines["moved"]
    assert "old-session" not in lines["moved"]
    assert "%15 (gone)" in lines["closed"]
    assert "%" not in lines["no-tmux"]


def test_table_shows_the_branch_in_a_repo_and_the_shortened_cwd_outside(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    repo = machine.home / "devel" / "repo"
    repo.mkdir(parents=True)
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "init", "-q", "-b", "feature/x"], cwd=repo, check=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "init"], cwd=repo, check=True)
    machine.session("work", "in-repo", cwd=str(repo))
    machine.session("work", "outside", cwd=str(machine.home / "notes"))

    result = invoke()

    assert result.exit_code == 0, result.output
    lines = {line.split()[1]: line for line in result.stdout.splitlines()[1:]}
    assert "feature/x" in lines["in-repo"]
    assert "~/notes" in lines["outside"]


def test_a_session_without_a_name_shows_its_pid(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    pid = machine.session("work", None)
    result = invoke()
    assert result.exit_code == 0, result.output
    assert f"pid:{pid}" in result.stdout


def test_a_waiting_session_shows_what_it_waits_for(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    machine.session("work", "asking", status="waiting", waitingFor="permission prompt")
    machine.session("work", "stale", status="idle", waitingFor="permission prompt")

    result = invoke()

    assert result.exit_code == 0, result.output
    lines = {line.split()[1]: line for line in result.stdout.splitlines()[1:]}
    assert "waiting: permission prompt" in lines["asking"]
    assert "permission" not in lines["stale"]
    by_name = {s["name"]: s for s in json_of()}
    assert by_name["asking"]["waiting_for"] == "permission prompt"
    assert by_name["stale"]["waiting_for"] is None


def transcript_of(machine: Machine, profile: str, name: str, records: list[dict[str, Any]]) -> Path:
    """Write the transcript of the running session ``name``, which runs in the default ``elsewhere`` dir."""
    project = machine.config_dir(profile) / "projects" / project_slug(machine.home / "elsewhere")
    return write_records(project / f"{sid(name)}.jsonl", records)


def test_active_is_the_last_model_response_or_the_dimmed_start(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    now = time.time()
    machine.session("work", "answered", startedAt=int((now - 7200) * 1000), updatedAt=int(now * 1000))
    transcript_of(machine, "work", "answered", [response(now - 600)])
    machine.session("work", "silent", startedAt=int((now - 1800) * 1000))

    result = invoke()

    assert result.exit_code == 0, result.output
    lines = {line.split()[1]: line for line in result.stdout.splitlines()[1:]}
    assert "10m ago" in lines["answered"]
    assert "started 30m ago" in lines["silent"]


def test_active_within_keeps_a_recent_response_or_a_recent_start(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    now = time.time()
    long_ago = int((now - 86400) * 1000)
    machine.session("work", "recent", startedAt=long_ago)
    transcript_of(machine, "work", "recent", [response(now - 600)])
    # A fresh registry record says nothing about activity: a /login or a rename also rewrites it.
    machine.session("work", "stale", startedAt=long_ago, updatedAt=int(now * 1000))
    transcript_of(machine, "work", "stale", [response(now - 7200)])
    machine.session("work", "just-started", startedAt=int((now - 60) * 1000))
    machine.session("work", "old-and-silent", startedAt=long_ago)

    assert [s["name"] for s in json_of("--active-within", "1h")] == ["recent", "just-started"]
    assert [s["name"] for s in json_of("--active-within", "3h")] == ["recent", "stale", "just-started"]


@pytest.mark.parametrize("value", ["soon", "0"])
def test_active_within_rejects_a_duration_it_cannot_use(machine: Machine, value: str) -> None:
    result = invoke("--active-within", value)
    assert result.exit_code == 2
    assert "--active-within" in result.stderr


def test_no_sessions_prints_a_notice_on_stderr(machine: Machine) -> None:
    result = invoke()
    assert result.exit_code == 0
    assert result.stdout == ""
    assert "No active sessions found" in result.stderr


def test_middle_ellipsis_keeps_both_ends() -> None:
    assert _middle_ellipsis("short", 10) == "short"
    shortened = _middle_ellipsis("rohlik/commercial-orders/fp/SCEX-6433-make-order-level", 20)
    assert len(shortened) == 20
    assert shortened.startswith("rohlik/co") and shortened.endswith("der-level")


# --- filters ---


def test_profile_filter_is_repeatable(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    machine.session("work", "w")
    machine.session("lab", "l")
    machine.session("default", "d")
    assert [s["name"] for s in json_of("--profile", "lab", "--profile", "work")] == ["l", "w"]


def test_unknown_profile_fails(machine: Machine) -> None:
    result = invoke("--profile", "nope")
    assert result.exit_code == 1
    assert "nope" in result.stderr


def test_profile_filter_takes_globs(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    machine.session("work", "w")
    machine.session("lab", "l")
    machine.session("default", "d")
    assert [s["name"] for s in json_of("--profile", "*a*")] == ["d", "l"]
    assert [s["name"] for s in json_of("--profile", "wor?", "--profile", "lab")] == ["l", "w"]


def test_a_profile_glob_that_matches_nothing_names_the_known_profiles(machine: Machine) -> None:
    result = invoke("--profile", "rohlik*")
    assert result.exit_code == 1
    assert "No Claude Code profile matches 'rohlik*'" in result.stderr
    assert "default, work, lab" in result.stderr


def test_status_filter_and_its_validation(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    machine.session("work", "working", status="busy")
    machine.session("work", "resting", status="idle")
    machine.session("work", "typing", status="shell")
    assert [s["name"] for s in json_of("--status", "busy", "--status", "shell")] == ["working", "typing"]
    result = invoke("--status", "sleeping")
    assert result.exit_code == 2
    assert "busy" in result.output


def test_tmux_filter(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    machine.session("work", "in-tmux", tmux="s:@1.%1")
    machine.session("work", "bare", tmux=None)
    assert [s["name"] for s in json_of("--tmux")] == ["in-tmux"]
    assert [s["name"] for s in json_of("--no-tmux")] == ["bare"]


def test_name_glob_is_case_insensitive_and_skips_unnamed(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    machine.session("work", "W1-Order-Planner")
    machine.session("work", "w2-order-planner")
    machine.session("work", "coordinator")
    machine.session("work", None)
    assert [s["name"] for s in json_of("--name", "w?-order-*")] == ["W1-Order-Planner", "w2-order-planner"]


def test_here_covers_the_repo_and_its_worktrees(
    machine: Machine, fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tmux(fake_bin, [])
    repo = machine.home / "repo"
    (repo / ".worktrees" / "fp-x").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    machine.session("work", "main", cwd=str(repo))
    machine.session("lab", "worktree", cwd=str(repo / ".worktrees" / "fp-x"))
    machine.session("work", "legacy", cwd=str(machine.home / "repo-worktrees" / "fp-y"))
    machine.session("work", "other", cwd=str(machine.home / "repo-other"))
    monkeypatch.chdir(repo)
    assert sorted(s["name"] for s in json_of("--here")) == ["legacy", "main", "worktree"]
    # Filters combine with AND.
    assert [s["name"] for s in json_of("--here", "--profile", "lab")] == ["worktree"]


def test_here_outside_a_repo_is_the_exact_cwd(
    machine: Machine, fake_bin: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_tmux(fake_bin, [])
    plain = machine.home / "plain"
    (plain / "sub").mkdir(parents=True)
    machine.session("work", "exact", cwd=str(plain))
    machine.session("work", "below", cwd=str(plain / "sub"))
    monkeypatch.chdir(plain)
    assert [s["name"] for s in json_of("--here")] == ["exact"]


# --- one session ---


def test_a_session_argument_points_to_inspect(machine: Machine) -> None:
    machine.session("work", "target")
    result = invoke("target")
    assert result.exit_code == 2
    assert "cc-session-tools inspect target" in result.stderr
    assert result.stdout == ""


# --- JSON ---

JSON_KEYS = [
    "profile",
    "profile_config_dir",
    "profile_source",
    "address",
    "pid",
    "session_id",
    "name",
    "name_source",
    "status",
    "waiting_for",
    "kind",
    "entrypoint",
    "version",
    "started_at",
    "updated_at",
    "last_activity_at",
    "last_record_at",
    "cwd",
    "launch_cwd",
    "git_branch",
    "git_worktree_root",
    "git_repo_root",
    "tmux_pane",
    "tmux_state",
    "tmux_live",
    "tmux_recorded",
    "messaging_socket",
    "messaging_address",
    "can_receive_messages",
    "peer_protocol",
    "peer_features",
    "bridge_session_id",
    "scratchpad_dir",
    "scratchpad_dir_inferred",
    "transcript_path",
    "cost",
    "record_path",
    "alive",
    "raw",
]


def test_json_shape(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, ["%14\tnow-here\t2\t@20\tmy title"])
    pid = machine.session("work", "target", tmux="was-here:@14.%14")

    [data] = json_of()

    assert list(data) == JSON_KEYS
    assert data["profile"] == "work"
    assert data["profile_config_dir"] == str(machine.config_dir("work").resolve())
    assert data["address"] == "work/target"
    assert data["started_at"] == "2026-11-08T01:06:41+00:00"
    assert data["tmux_pane"] == "%14"
    assert data["tmux_state"] == "present"
    assert data["tmux_live"] == {"session": "now-here", "window_index": 2, "window_id": "@20", "pane_title": "my title"}
    assert data["tmux_recorded"] == "was-here:@14.%14"
    assert data["messaging_address"] == f"uds:/run/user/1000/cc-socks/{pid}.sock"
    assert data["git_branch"] is None and data["git_repo_root"] is None
    assert data["transcript_path"] is None
    assert data["last_activity_at"] is None and data["last_record_at"] is None and data["cost"] is None
    assert data["bridge_session_id"] is None
    assert data["alive"] is True
    assert data["raw"]["pid"] == pid


def test_json_finds_the_transcript(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    cwd = machine.home / "proj"
    cwd.mkdir()
    machine.session("work", "target", cwd=str(cwd))
    transcript = machine.config_dir("work") / "projects" / project_slug(cwd) / f"{sid('target')}.jsonl"
    transcript.parent.mkdir(parents=True)
    record = {"type": "user", "cwd": str(cwd), "timestamp": "2026-11-08T12:00:00.000Z", "message": {"content": "hi"}}
    transcript.write_text(json.dumps(record) + "\n")

    assert one("target")["transcript_path"] == str(transcript)


def test_json_of_no_sessions_is_an_empty_array(machine: Machine) -> None:
    assert json_of() == []


# --- tmux resolution ---


def test_tmux_missing_reports_the_pane_as_unknown(
    machine: Machine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine.session("work", "target", tmux="s:@1.%1")
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    assert list_tmux_panes() is None

    data = one("target")

    assert data["tmux_pane"] == "%1"
    assert data["tmux_state"] == "unknown"
    assert data["tmux_live"] is None
    result = invoke()
    assert result.exit_code == 0, result.output
    assert "%1 (tmux unknown)" in result.stdout


def test_tmux_without_a_server_reports_the_pane_as_unknown(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, None, exit_code=1)
    machine.session("work", "target", tmux="s:@1.%1")
    assert one("target")["tmux_state"] == "unknown"


def test_a_pane_that_no_longer_exists_is_gone(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, ["%2\tother\t0\t@2\t"])
    machine.session("work", "target", tmux="s:@1.%1")
    data = one("target")
    assert data["tmux_state"] == "gone"
    assert data["tmux_live"] is None
    assert data["tmux_pane"] == "%1"


def test_list_tmux_panes_parses_one_call(fake_bin: Path) -> None:
    fake_tmux(fake_bin, ["%1\ta/b\t0\t@1\ttitle\twith tab", "garbage", "%7\tc\tx\t@7\t"])
    panes = list_tmux_panes()
    assert panes is not None
    assert set(panes) == {"%1", "%7"}
    assert panes["%1"].pane_title == "title\twith tab"
    assert panes["%7"].window_index is None


@pytest.mark.parametrize(
    ("recorded", "pane"),
    [
        ("rohlik/commercial-orders/fp/SCEX-6433-worker1:@14.%14", "%14"),
        ("s:@1.%1", "%1"),
        (None, None),
        ("", None),
        ("s:@1", None),
        ("s:@1.%x", None),
    ],
)
def test_pane_id_of(recorded: str | None, pane: str | None) -> None:
    assert pane_id_of(recorded) == pane
