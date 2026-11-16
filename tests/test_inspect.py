from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from cc_blackbox import project_slug
from conftest import Machine, cost_state, fake_tmux, response, sid, stamp, write_records
from typer.testing import CliRunner

from cc_session_tools.cli import app
from cc_session_tools.inspection import INSPECT_KEYS, LIVE_ONLY_KEYS

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="liveness and fake processes need Linux")

runner = CliRunner()

LIST_ACTIVE_KEYS = [
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


@pytest.fixture(autouse=True)
def no_tmux(fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])


def invoke(*args: str) -> Any:
    return runner.invoke(app, ["inspect", *args])


def json_of(*args: str) -> Any:
    result = invoke(*args, "--json")
    assert result.exit_code == 0, result.output
    return json.loads(result.stdout)


def ended(
    machine: Machine,
    profile: str,
    key: str,
    *,
    names: list[str],
    cwds: list[str] | None = None,
    pid: int | None = None,
    age_days: float = 1,
    tail: list[dict[str, Any]] | None = None,
) -> Path:
    """Write the transcript of an ended session ``key`` that was named ``names`` in turn, with an auth-switch start log
    when ``pid`` is given, and ``tail`` as its last records. The file's mtime is ``age_days`` ago."""
    cwds = cwds or [str(machine.home / "proj")]
    session_id = sid(key)
    project = machine.config_dir(profile) / "projects" / project_slug(cwds[0])
    project.mkdir(parents=True, exist_ok=True)
    started = time.time() - age_days * 86400
    records: list[dict[str, Any]] = []
    for index, cwd in enumerate(cwds):
        records.append(
            {
                "type": "user",
                "sessionId": session_id,
                "cwd": cwd,
                "gitBranch": f"branch-{index}",
                "version": "2.1.322",
                "entrypoint": "cli",
                "timestamp": stamp(started),
                "message": {"role": "user", "content": f"prompt {index}"},
            }
        )
    for name in names:
        records.append({"type": "custom-title", "customTitle": name, "sessionId": session_id})
        records.append({"type": "agent-name", "agentName": name, "sessionId": session_id})
    path = write_records(project / f"{session_id}.jsonl", records + (tail or []))
    if pid is not None:
        log = project / session_id / "auth-switch-profile-starts.jsonl"
        log.parent.mkdir(parents=True, exist_ok=True)
        start = {
            "v": 1,
            "ts": int(started * 1000),
            "session_id": session_id,
            "pid": pid,
            "source": "startup",
            "profile": profile,
            "config_dir": str(machine.config_dir(profile)),
            "cwd": cwds[0],
        }
        log.write_text(json.dumps(start) + "\n")
    os.utime(path, (started, started))
    return path


def test_the_json_keys_are_list_active_plus_the_ended_session_fields() -> None:
    assert [k for k in INSPECT_KEYS if k in LIST_ACTIVE_KEYS] == LIST_ACTIVE_KEYS
    assert set(INSPECT_KEYS) - set(LIST_ACTIVE_KEYS) == {
        "state",
        "title",
        "title_source",
        "profile_starts",
        "resume_command",
    }


# --- running sessions ---


def test_a_running_session_by_id_prefix_name_address_and_pid(machine: Machine) -> None:
    pid = machine.session("work", "target")
    machine.session("work", "other")
    session_id = sid("target")
    for query in (session_id, session_id[:8], "target", "work/target", f"work/pid:{pid}", str(pid)):
        data = json_of(query)
        assert data["pid"] == pid, query
        assert data["state"] == "running"
    assert list(data) == list(INSPECT_KEYS)
    assert data["alive"] is True
    assert data["resume_command"] == f"claude-work --resume {session_id}"


def test_a_running_session_in_text(machine: Machine) -> None:
    machine.session("lab", "target")
    result = invoke("target")
    assert result.exit_code == 0, result.output
    keys = [line.split()[0] for line in result.stdout.splitlines() if line.strip()]
    assert keys[:3] == ["state", "profile", "profile_config_dir"]
    assert "tmux_live" in keys and "messaging_address" in keys and "raw" not in keys


def test_an_ambiguous_running_name_lists_the_candidates(machine: Machine) -> None:
    first = machine.session("work", "twin", sessionId=sid("twin-1"))
    second = machine.session("lab", "twin", sessionId=sid("twin-2"))
    result = invoke("twin")
    assert result.exit_code == 1
    assert "2 sessions match" in result.stderr
    assert f"pid {first}" in result.stderr and f"pid {second}" in result.stderr
    assert json_of("lab/twin")["pid"] == second


def test_a_running_session_found_by_an_old_name_is_running(machine: Machine) -> None:
    pid = machine.session("work", "new-name", sessionId=sid("renamed"))
    ended(machine, "work", "renamed", names=["old-name", "new-name"])
    data = json_of("old-name")
    assert data["state"] == "running"
    assert data["pid"] == pid


# --- ended sessions ---


def test_an_ended_session_by_id_and_prefix(machine: Machine) -> None:
    home = machine.home
    path = ended(
        machine, "work", "gone", names=["first", "last"], cwds=[str(home / "proj"), str(home / "proj" / "wt")], pid=4242
    )
    session_id = sid("gone")
    for query in (session_id, session_id[:6]):
        data = json_of(query)
        assert data["state"] == "ended"
        assert data["session_id"] == session_id
    assert list(data) == list(INSPECT_KEYS)
    assert data["profile"] == "work"
    assert data["profile_source"] == "log"
    assert data["address"] == "work/last"
    assert data["name"] == "last"
    assert data["title"] == "last"
    assert data["launch_cwd"] == str(home / "proj")
    assert data["cwd"] == str(home / "proj" / "wt")
    assert data["git_branch"] == "branch-1"
    assert data["transcript_path"] == str(path)
    assert data["alive"] is False
    assert data["started_at"] is not None
    assert [(s["pid"], s["source"], s["profile"]) for s in data["profile_starts"]] == [(4242, "startup", "work")]
    assert data["resume_command"] == f"claude-work --resume {session_id}"
    assert all(data[key] is None for key in LIVE_ONLY_KEYS)


def test_an_ended_session_reports_its_last_response_apart_from_its_last_record_and_its_cost(machine: Machine) -> None:
    now = time.time()
    login = {"type": "system", "subtype": "local_command", "timestamp": stamp(now - 600), "content": "/login"}
    ended(machine, "work", "gone", names=["x"], tail=[response(now - 7200), cost_state(1.25), login])

    data = json_of("x")

    assert data["last_activity_at"] == f"{stamp(now - 7200)[:19]}+00:00"
    assert data["last_record_at"] == f"{stamp(now - 600)[:19]}+00:00"
    cost = data["cost"]
    assert cost["total_cost_usd"] == 1.25
    assert cost["duration_seconds"] == 5400.0
    assert cost["api_duration_seconds"] == 600.0
    assert cost["lines_added"] == 12 and cost["lines_removed"] == 3
    assert cost["model_usage"]["claude-opus-5-5"]["output_tokens"] == 1500
    text = invoke("x").stdout
    assert "$1.25, 1h30m run, 10m API, 30s tools, +12 -3 lines; recorded at its last exit" in text
    assert "before this run" not in text


def test_a_running_session_has_a_cost_only_after_an_exit_and_without_the_current_run(machine: Machine) -> None:
    cwd = machine.home / "proj"
    machine.session("work", "resumed", cwd=str(cwd))
    machine.session("work", "fresh", cwd=str(cwd))
    project = machine.config_dir("work") / "projects" / project_slug(cwd)
    first = {"type": "user", "cwd": str(cwd), "timestamp": stamp(time.time() - 60), "message": {"content": "hi"}}
    write_records(project / f"{sid('resumed')}.jsonl", [first, cost_state(2.0), response(time.time() - 30)])
    write_records(project / f"{sid('fresh')}.jsonl", [first, response(time.time() - 30)])

    assert json_of("resumed")["cost"]["total_cost_usd"] == 2.0
    assert "recorded at its last exit before this run, which is not included" in invoke("resumed").stdout
    assert json_of("fresh")["cost"] is None
    assert "it has not exited yet" in invoke("fresh").stdout


def test_an_ended_session_by_its_name_or_an_earlier_one(machine: Machine) -> None:
    ended(machine, "work", "gone", names=["first", "last"])
    for query in ("last", "first", "FIRST", "work/first"):
        assert json_of(query)["session_id"] == sid("gone"), query
    result = invoke("lab/first")
    assert result.exit_code == 1
    assert "no session matches 'lab/first'" in result.stderr


def test_an_ended_session_by_its_dead_pid(machine: Machine) -> None:
    ended(machine, "work", "gone", names=["x"], pid=999999)
    assert json_of("999999")["session_id"] == sid("gone")
    assert json_of("work/pid:999999")["session_id"] == sid("gone")
    assert invoke("lab/pid:999999").exit_code == 1


def test_names_and_pids_are_looked_up_within_the_window(machine: Machine) -> None:
    ended(machine, "work", "old", names=["ancient"], pid=999998, age_days=30)
    assert invoke("ancient").exit_code == 1
    assert invoke("999998").exit_code == 1
    assert json_of("ancient", "--days", "31")["session_id"] == sid("old")
    # A session id is looked up in every transcript.
    assert json_of(sid("old")[:8])["state"] == "ended"


def test_ambiguous_ended_sessions_are_listed(machine: Machine) -> None:
    ended(machine, "work", "one", names=["shared"])
    ended(machine, "lab", "two", names=["shared"])
    result = invoke("shared")
    assert result.exit_code == 1
    assert "2 ended sessions match 'shared' by name" in result.stderr
    assert sid("one") in result.stderr and sid("two") in result.stderr
    assert json_of("lab/shared")["session_id"] == sid("two")


def test_an_ended_session_without_an_auth_switch_profile_resumes_with_plain_claude(machine: Machine) -> None:
    ended(machine, "default", "plain", names=["plain-session"])
    data = json_of("plain-session")
    assert data["profile"] == "default"
    assert data["resume_command"] == f"claude --resume {sid('plain')}"
    assert data["profile_starts"] == []


def test_nothing_matches(machine: Machine) -> None:
    result = invoke("nothing")
    assert result.exit_code == 1
    assert "no session matches 'nothing'" in result.stderr
    assert result.stdout == ""
