from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from transcripts import Transcript, permission
from typer.testing import CliRunner

from cc_session_tools.cli import app

runner = CliRunner()
HOUR = 3600


@pytest.fixture(autouse=True)
def utc(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Print times in UTC, with the home dir the transcripts name."""
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("HOME", "/home/user")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def invoke(tx: Transcript, tmp_path: Path, *args: str) -> Any:
    path = tx.write(tmp_path / "projects")
    return runner.invoke(app, ["tool-stats", str(path), *args])


def bash(tx: Transcript, t: float, command: str, seconds: float, **result: Any) -> None:
    call = tx.request(t).bash(t + 1, command)
    tx.result(t + 1 + seconds, call, **result)


def session() -> Transcript:
    """Tests, polls and git in the main agent, a prompt the user answered, and a validator with a nested runner."""
    tx = Transcript()
    tx.prompt(0, "Do the step.")
    bash(tx, 1, "cd /home/user/projects/example && uv run pytest", 120, permission=permission("config"))
    bash(tx, 200, "uv run pytest tests/test_a.py", 30, permission=permission("config"))
    for n in range(3):
        bash(tx, 300 + n * 100, f"sleep 60; pgrep -af gradle # run 1234{n}", 60, permission=permission("hook"))
    bash(tx, 700, "git push", 20, permission=permission("user_temporary"))
    bash(tx, 800, "rm -rf build", 5, permission=permission("user_reject", decision="reject"), denial="user-rejected")
    parent = tx.request(900).agent(901, "v1", description="validate: C6", background=True)
    tx.agent_returned(902, parent)
    tx.turn_end(903)
    child = parent.request(910).agent(911, "r1", description="run: tests", agent_type="test-runner")
    child_call = child.request(912).bash(913, "uv run pytest")
    child.result(1213, child_call)
    child.request(1214).text(1215)
    parent.agent_returned(1216, child)
    parent.request(1217).text(1218)
    return tx


def test_the_json_has_every_section(tmp_path: Path, isolated_home: Path) -> None:
    data = json.loads(invoke(session(), tmp_path, "--json").stdout)
    assert set(data) == {
        "session_id", "totals", "tools", "bash_groups", "slowest", "repeated", "permissions", "subagent_roles",
        "nested_by_parent_role",
    }  # fmt: skip
    groups = {r["group"]: r for r in data["bash_groups"]}
    assert (groups["test (full)"]["n"], groups["test (full)"]["total"]) == (2, 420)
    assert (groups["test (targeted)"]["n"], groups["wait"]["n"], groups["wait"]["waits_on_other_builds"]) == (1, 3, 3)
    assert groups["test (full)"]["main"] == 1 and groups["test (full)"]["subagents"] == 1
    assert [r["n"] for r in data["repeated"]] == [2, 3]  # by total wall
    assert data["repeated"][1] == {
        "command": "sleep N; pgrep -af gradle # run N",
        "n": 3, "total": 180.0, "median": 60.0, "max": 60.0, "bash_group": "wait", "lanes_by_type": {"main": 3},
    }  # fmt: skip
    p = data["permissions"]
    assert p["decisions"] == {"hook": 3, "none": 3, "config": 2, "user_temporary": 1, "user_reject": 1}
    assert (p["user_answered"], p["user_wait"], p["denials"]) == (2, 25.0, {"user-rejected": 1})


def test_the_session_paths_are_redacted(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    bash(tx, 1, "cat /home/user/projects/example/README.md", 2)
    tx.turn_end(10)
    data = json.loads(invoke(tx, tmp_path, "--json").stdout)
    assert data["slowest"][0]["command"] == "cat <repo>/README.md"


def test_subagent_cost_keeps_lane_time_apart_from_elapsed_time(tmp_path: Path, isolated_home: Path) -> None:
    data = json.loads(invoke(session(), tmp_path, "--json").stdout)
    roles = {(r["role"], r["type"], r["nested"]): r for r in data["subagent_roles"]}
    parent, child = roles[("validate", "general-purpose", False)], roles[("run", "test-runner", True)]
    assert parent["active"] >= child["active"] == pytest.approx(304)
    assert data["totals"]["subagents_elapsed"] == pytest.approx(parent["active"])  # the runner ran inside its parent
    assert data["nested_by_parent_role"] == [
        {"parent_role": "validate", "n": 1, "active": child["active"], "elapsed": child["active"],
         "tokens_total": child["tokens_total"], "by_type": {"test-runner": 1}},
    ]  # fmt: skip


def test_the_markdown(tmp_path: Path, isolated_home: Path) -> None:
    result = invoke(session(), tmp_path, "--top", "3")
    assert result.exit_code == 0, result.output
    out = result.stdout.splitlines()
    assert out[0].startswith("# Tool stats ")
    assert "| test (full) | 2 | 7m00s | 3m30s | 5m00s | 67 | 1 | 1 | pytest |" in out
    assert "- 5m00s 11-02 09:15 test-runner Bash [test (full)] (+1 similar): `uv run pytest`" in out
    assert len([line for line in out if line.startswith("- ") and " Bash [" in line]) == 3
    assert "- ×3 total 3m00s (max 1m00s) [wait] in main 3: `sleep N; pgrep -af gradle # run N`" in out
    assert "Prompts the user answered: 2, total wait 25s: Bash 20s, Bash 5s" in out
    assert "| validate | general-purpose | 1 |" in "\n".join(out)
    assert any(line.startswith("- validate: 1 nested (test-runner 1), elapsed 5m04s") for line in out)


def test_a_call_over_an_hour_is_flagged(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    bash(tx, 1, "./run-the-long-batch.sh", 3 * HOUR)
    tx.turn_end(3 * HOUR + 10)
    text = invoke(tx, tmp_path).stdout
    assert "⌛ 1 call over 1 h, 3h00m in all" in text and "- 3h00m ⌛ 11-02 09:00 main Bash" in text
    data = json.loads(invoke(tx, tmp_path / "json", "--json").stdout)
    assert data["slowest"][0]["suspect_wait"] is True and data["totals"]["suspect_waits"] == 1


def test_a_bad_target_or_groups_file_fails(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = runner.invoke(app, ["tool-stats", "00000000-0000-0000-0000-000000000000"])
    assert result.exit_code == 1 and "could not resolve" in result.output
    monkeypatch.setenv("HOME", str(isolated_home))
    groups = isolated_home / ".config" / "cc-session-tools" / "bash-groups.toml"
    groups.parent.mkdir(parents=True)
    groups.write_text("[programs]\nmy-db-cli = \n", encoding="utf-8")
    result = invoke(session(), tmp_path)
    assert result.exit_code == 1 and "bash-groups.toml" in result.output and result.stdout == ""


def test_top_below_one_is_a_usage_error(tmp_path: Path, isolated_home: Path) -> None:
    result = invoke(session(), tmp_path, "--top", "0")
    assert result.exit_code == 2 and "--top" in result.output


def test_user_waits_batched_results_and_background_bash_are_kept_apart(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    r = tx.request(1)
    sub = r.agent(2, "a1", description="impl: C6")
    held = r.bash(3, "git diff")
    sub.request(4).text(500)
    tx.agent_returned(600, sub)
    tx.result(601, held)
    ask = tx.request(602).call(603, "AskUserQuestion", question="Which one?")
    tx.result(1200, ask, permission=permission("user_temporary"))
    bash(tx, 1201, "git push", 300, permission=permission("user_temporary"))
    background = tx.request(1600).bash(1601, "./serve.sh", background=True)
    tx.result(1602, background)
    bash(tx, 1610, "make lint", 10, permission=permission("hook", decision="reject", reason_type="hook"))
    tx.turn_end(1700)
    data = json.loads(invoke(tx, tmp_path, "--json").stdout)
    assert [r["tool"] for r in data["slowest"]] == ["Bash", "Bash"]  # make lint and the launch; no git diff, no dialog
    assert {r["command"] for r in data["slowest"]} == {"make lint", "./serve.sh"}
    assert "AskUserQuestion" not in {r["tool"] for r in data["tools"]}
    git = next(r for r in data["bash_groups"] if r["group"] == "git")
    assert (git["n"], git["total"]) == (1, 0)  # the held git diff took 0; the push waited on the user
    t = data["totals"]
    assert (t["batched_calls"], t["background_bash"], t["user_wait_calls"], t["user_wait_wall"]) == (1, 1, 2, 897)
    assert data["permissions"]["decisions"]["hook reject"] == 1
    text = invoke(tx, tmp_path / "md").stdout
    assert "2 calls waited on the user, 14m in all" in text and "Background Bash: 1 call;" in text


def test_an_old_transcript_says_permission_decisions_were_not_recorded(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    bash(tx, 1, "git status", 2)
    tx.turn_end(10)
    text = invoke(tx, tmp_path).stdout
    assert "not recorded: this Claude Code version wrote no permission decisions" in text
