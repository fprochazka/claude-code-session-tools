from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from cc_blackbox import project_slug
from conftest import (
    Machine,
    RealTmux,
    compact_boundary,
    fake_tmux,
    git,
    log_start,
    response,
    screen_tmux,
    sid,
    stamp,
    write_records,
)
from typer.testing import CliRunner

from cc_session_tools.active import context_window_text, list_tmux_panes, pane_id_of
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
    git("init", "-q", "-b", "feature/x", cwd=repo)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=repo)
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


def test_a_session_whose_pane_shows_the_plan_dialog_waits_for_plan_approval(machine: Machine, fake_bin: Path) -> None:
    pid = machine.session("work", "planner", status="waiting", waitingFor="permission prompt", tmux="main:@14.%14")
    capture = (Path(__file__).parent / "fixtures" / "screen" / "plan-approval.ansi").read_text(encoding="utf-8")
    screen_tmux(fake_bin, pane_pid=pid, capture=capture, panes=["%14\tmain\t3\t@14\t$1\tw1"])

    result = invoke()

    assert result.exit_code == 0, result.output
    assert "waiting: plan approval" in result.stdout
    [session] = json_of()
    assert (session["waiting_for"], session["raw"]["waitingFor"]) == ("plan approval", "permission prompt")


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
    shortened = _middle_ellipsis("acme/orders/fp/ABC-1234-make-order-level", 20)
    assert len(shortened) == 20
    assert shortened.startswith("acme/orde") and shortened.endswith("der-level")


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
    result = invoke("--profile", "acme*")
    assert result.exit_code == 1
    assert "No Claude Code profile matches 'acme*'" in result.stderr
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


def test_tmux_session_follows_a_window_moved_to_another_tmux_session(
    machine: Machine, real_tmux: RealTmux, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_pane = real_tmux.new_session("alpha")
    other_pane = real_tmux.new_session("beta")
    moved_pane = real_tmux.new_window("alpha")
    machine.session("work", "stays", tmux=real_tmux.recorded(home_pane))
    machine.session("work", "moved", tmux=real_tmux.recorded(moved_pane))
    machine.session("work", "other", tmux=real_tmux.recorded(other_pane))
    machine.session("work", "outside")
    real_tmux.move_window(moved_pane, "beta")

    assert [s["name"] for s in json_of("--tmux-session", "beta")] == ["moved", "other"]
    assert [s["name"] for s in json_of("--tmux-session", "alpha")] == ["stays"]

    monkeypatch.setenv("TMUX_PANE", moved_pane)
    assert [s["name"] for s in json_of("--tmux-session", "current")] == ["moved", "other"]
    result = invoke("--tmux-session", "current")
    assert result.exit_code == 0, result.output
    assert f"{moved_pane} beta:" in result.stdout


@pytest.mark.parametrize(
    ("args", "env_pane", "code", "message"),
    [
        (["--tmux-session", "gamma"], None, 1, "no tmux session is named 'gamma'. tmux sessions: alpha, beta."),
        (["--tmux-session", "current"], None, 1, "this command does not run in tmux ($TMUX_PANE is unset)"),
        (["--tmux-session", "current"], "%99", 1, "the tmux server has no pane %99"),
        (["--tmux-session", "alpha", "--no-tmux"], None, 2, "--tmux-session and --no-tmux exclude each other"),
    ],
)
def test_tmux_session_errors(
    machine: Machine,
    fake_bin: Path,
    monkeypatch: pytest.MonkeyPatch,
    args: list[str],
    env_pane: str | None,
    code: int,
    message: str,
) -> None:
    fake_tmux(fake_bin, ["%1\talpha\t0\t@1\t", "%2\tbeta\t0\t@2\t"])
    if env_pane is not None:
        monkeypatch.setenv("TMUX_PANE", env_pane)
    result = invoke(*args)
    assert result.exit_code == code, result.output
    assert message in result.stderr


def test_tmux_session_needs_a_tmux_server(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, None, exit_code=1)
    result = invoke("--tmux-session", "alpha")
    assert result.exit_code == 1
    assert "cannot read the tmux server" in result.stderr


def with_role(machine: Machine, name: str, *roles: str | None, transcript: bool = True, **fields: Any) -> int:
    """A running ``work`` session and one auth-switch start of its process per role, oldest first, the first one a
    moment after the process started. ``transcript`` writes its transcript; a session waiting for its first prompt has
    none yet."""
    pid = machine.session("work", name, **fields)
    if transcript:
        transcript_of(machine, "work", name, [response(time.time() - 60)])
    started = json.loads(machine.records[pid].read_text())["startedAt"]
    for index, role in enumerate(roles):
        log_start(
            machine.project_dir("work"), sid(name), started + 300 + index * 1000, pid=pid, profile="work", role=role
        )
    return pid


def test_role_comes_from_the_newest_start_of_the_process(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    with_role(machine, "dispatcher", None, "dispatcher")
    with_role(machine, "worker-1", "worker")
    with_role(machine, "lost-role", "worker", None)
    machine.session("work", "plain")

    roles = {s["name"]: s["role"] for s in json_of()}
    assert roles == {"dispatcher": "dispatcher", "worker-1": "worker", "lost-role": None, "plain": None}

    result = invoke()
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split()[:5] == ["Profile", "Name", "Role", "Status", "Active"]
    rows = {line.split()[1]: line.split() for line in lines[1:]}
    assert rows["worker-1"][2] == "worker"
    assert rows["plain"][2] == "idle"


def test_role_is_the_one_of_the_running_process_not_of_another(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    with_role(machine, "shared", "worker")
    # A later start of the same session by another process, with another role, which this listing does not show.
    log_start(machine.project_dir("work"), sid("shared"), int(time.time() * 1000), pid=1, profile="work", role="boss")
    assert [s["role"] for s in json_of()] == ["worker"]


def test_a_session_waiting_for_its_first_prompt_has_its_role(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    with_role(machine, "fresh", "worker", transcript=False)
    [data] = json_of("--role", "worker")
    assert (data["name"], data["role"], data["transcript_path"]) == ("fresh", "worker", None)


def test_the_role_column_is_left_out_when_no_session_has_a_role(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    with_role(machine, "old-style", None)
    result = invoke()
    assert result.stdout.splitlines()[0].split()[:4] == ["Profile", "Name", "Status", "Active"]


def test_role_filter_takes_globs_and_never_matches_a_session_without_a_role(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    with_role(machine, "dispatcher", "dispatcher")
    with_role(machine, "w1", "worker-orders")
    with_role(machine, "w2", "worker-stock")
    machine.session("work", "plain")

    assert [s["name"] for s in json_of("--role", "worker-*")] == ["w1", "w2"]
    assert [s["name"] for s in json_of("--role", "dispatcher", "--role", "worker-s*")] == ["dispatcher", "w2"]
    assert json_of("--role", "WORKER-*") == []
    assert json_of("--role", "*", "--name", "plain") == []


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
    git("init", "-q", cwd=repo)
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
    "role",
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
    "context_window",
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
    assert data["context_window"] is None
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
    fake_tmux(fake_bin, ["%1\ta/b\t0\t@1\t$3\ttitle\twith tab", "garbage", "%7\tc\tx\t@7\t"])
    panes = list_tmux_panes()
    assert panes is not None
    assert set(panes) == {"%1", "%7"}
    assert panes["%1"].pane_title == "title\twith tab"
    assert panes["%1"].session_id == "$3"
    assert panes["%7"].window_index is None


@pytest.mark.parametrize(
    ("recorded", "pane"),
    [
        ("acme/orders/fp/ABC-1234-worker1:@14.%14", "%14"),
        ("s:@1.%1", "%1"),
        (None, None),
        ("", None),
        ("s:@1", None),
        ("s:@1.%x", None),
    ],
)
def test_pane_id_of(recorded: str | None, pane: str | None) -> None:
    assert pane_id_of(recorded) == pane


# --- the context window ---


def test_the_context_window_shows_the_fill_of_the_newest_response(machine: Machine, fake_bin: Path) -> None:
    fake_tmux(fake_bin, [])
    cwd = machine.home / "proj"
    for name in ("filled", "compacted", "fresh"):
        machine.session("work", name, cwd=str(cwd))
    project = machine.config_dir("work") / "projects" / project_slug(cwd)
    first = {"type": "user", "cwd": str(cwd), "timestamp": stamp(time.time() - 60), "message": {"content": "hi"}}
    answered = response(time.time() - 30, used=312_345) | {"requestedModel": "claude-opus-5-5"}
    write_records(project / f"{sid('filled')}.jsonl", [first, answered])
    write_records(project / f"{sid('compacted')}.jsonl", [first, answered, compact_boundary(time.time() - 20)])
    write_records(project / f"{sid('fresh')}.jsonl", [first])

    assert one("filled")["context_window"] == {
        "size": 1_000_000,
        "used": 312_345,
        "percent": 31,
        "compacted": False,
        "model": "claude-opus-5-5",
        "size_source": "model_table",
        "measured_at": f"{answered['timestamp'][:19]}+00:00",
    }
    compacted = one("compacted")["context_window"]
    assert (compacted["used"], compacted["percent"], compacted["compacted"]) == (0, 0, True)
    assert one("fresh")["context_window"] is None

    result = invoke()
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0].split()[:5] == ["Profile", "Name", "Status", "Active", "Ctx"]
    lines = {line.split()[1]: line for line in result.stdout.splitlines()[1:]}
    assert "312k/1M 31%" in lines["filled"]
    assert "0k/1M compacted" in lines["compacted"]


def test_the_context_window_dims_a_size_assumed_for_an_unknown_model() -> None:
    window = {"size": 200_000, "used": 64_999, "percent": 32, "compacted": False, "size_source": "default"}
    text = context_window_text(window)
    assert text.plain == "64k/200k 32%"
    assert [text.plain[span.start : span.end] for span in text.spans if span.style == "dim"] == ["200k"]
    assert not [span for span in context_window_text({**window, "size_source": "model_table"}).spans if span.style]
