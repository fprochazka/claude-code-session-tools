from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from transcripts import CWD, SESSION_ID, Transcript, permission
from typer.testing import CliRunner

from cc_session_tools.cli import app
from cc_session_tools.tool_stats_cmd import tool_stats_many

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
        "session_id", "totals", "tools", "bash_groups", "bash_programs", "slowest", "repeated", "permissions",
        "subagent_roles", "subagent_descriptions", "nested_by_parent_role", "filter",
    }  # fmt: skip
    groups = {r["group"]: r for r in data["bash_groups"]}
    assert (groups["test (full)"]["n"], groups["test (full)"]["total"]) == (2, 420)
    assert (groups["test (targeted)"]["n"], groups["wait"]["n"], groups["wait"]["waits_on_other_builds"]) == (1, 3, 3)
    assert groups["test (full)"]["main"] == 1 and groups["test (full)"]["subagents"] == 1
    assert [r["n"] for r in data["repeated"]] == [2, 3]  # by total wall
    assert data["repeated"][1] == {
        "command": "sleep N; pgrep -af gradle # run N",
        "n": 3, "total": 180.0, "median": 60.0, "max": 60.0, "sessions": 1, "bash_group": "wait",
        "lanes_by_type": {"main": 3},
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


def test_only_repeats_of_a_bash_command_are_similar(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    for n, seconds in enumerate([20, 50, 90]):
        edit = tx.request(10 + n * 100).call(11 + n * 100, "Edit", file_path=f"/home/user/projects/example/{n}.py")
        tx.result(11 + n * 100 + seconds, edit)
    bash(tx, 400, "make lint", 5)
    bash(tx, 500, "make lint", 10)
    tx.turn_end(600)
    data = json.loads(invoke(tx, tmp_path, "--json").stdout)
    assert [(r["tool"], r["wall"], r["similar"]) for r in data["slowest"]] == [
        ("Edit", 90.0, 0), ("Edit", 50.0, 0), ("Edit", 20.0, 0), ("Bash", 10.0, 1),
    ]  # fmt: skip
    out = invoke(tx, tmp_path / "md", "--top", "2").stdout.splitlines()
    assert [line for line in out if line.startswith("- ") and " Edit" in line] == [
        "- 1m30s 11-02 09:03 main Edit", "- 50s 11-02 09:01 main Edit",
    ]  # fmt: skip


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
    assert "2 calls waited on the user, 14m in all" in text and "Background Bash: 1 call, ran 1s until" in text


def test_a_stall_on_a_full_context_is_counted(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    bash(tx, 1, "git status", 2)
    tx.turn_end(10)
    for t in (HOUR, 2 * HOUR):
        tx.scheduled(t)
        tx.api_error(t)
    tx.local_command(3 * HOUR)
    tx.compaction(3 * HOUR, trigger="manual", duration_ms=20)
    data = json.loads(invoke(tx, tmp_path, "--json").stdout)
    t = data["totals"]
    assert (t["stalls"], t["stalled"], t["context_full_errors"]) == (1, 2 * HOUR, 2)
    text = invoke(tx, tmp_path / "md").stdout
    assert "✖ 2 prompts refused on a full context (all lanes); the main agent stalled 1 time, 2h00m in all." in text


def test_bash_calls_moved_to_the_background_count_until_their_notice(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "a1", description="run: tests", background=True)
    tx.agent_returned(3, sub)
    tx.turn_end(4)
    moved = sub.request(9).bash(10, "./gradlew test")
    sub.moved_to_background(190, moved, "b1")
    lost = sub.request(191).bash(192, "./gradlew test")
    sub.moved_to_background(312, lost, "b2", after_ms=120_000, capped=False)
    sub.task_notification(610, "b1", call_id=moved, queued=True)
    sub.request(611).text(620)
    launched = tx.request(700).bash(701, "./serve.sh", background=True)
    tx.moved_to_background(702, launched, "b3", after_ms=None)
    tx.turn_end(703)
    tx.task_notification(1001, "b3", call_id=launched)
    tx.request(1002).text(1003)
    tx.turn_end(1004)
    data = json.loads(invoke(tx, tmp_path, "--json").stdout)
    t = data["totals"]
    assert (t["moved_bash"], t["moved_bash_run_wall"], t["moved_bash_unfinished"]) == (2, 720.0, 1)
    assert t["moved_bash_by_reason"] == {"cap": 1, "timeout": 1}
    assert (t["background_bash"], t["background_bash_run_wall"]) == (1, 300.0)
    test = next(r for r in data["bash_groups"] if r["group"] == "test (full)")
    assert (test["n"], test["total"], test["max"]) == (2, 720.0, 600.0)
    slowest = data["slowest"][0]
    assert (slowest["run_wall"], slowest["wall"], slowest["auto_backgrounded"]) == (600.0, 180.0, True)
    assert "blocking_wall" not in slowest
    text = invoke(tx, tmp_path / "md").stdout
    assert (
        "↷ Moved to the background: 2 Bash calls (1 at the subagent cap, 1 at their own timeout); they"
        " ran 12m until their completion notices, 1 without a notice, counted until the move. The tool sections count"
        " that runtime; the lane and busy totals count only the time until the move." in text
    )
    assert "Background Bash: 1 call, ran 5m00s until their completion notices or, without one, their launch." in text
    assert "- 10m ↷ 11-02 09:00 general-purpose Bash [test (full)] (+1 similar): `./gradlew test`" in text


def test_a_move_to_deliver_a_message_is_named(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    moved = tx.request(1).bash(2, "./gradlew test")
    tx.moved_to_background(30, moved, "b1", to_deliver_message=True)
    tx.task_notification(302, "b1", call_id=moved)
    tx.request(303).text(310)
    tx.turn_end(311)
    data = json.loads(invoke(tx, tmp_path, "--json").stdout)
    t = data["totals"]
    assert (t["moved_bash"], t["moved_bash_by_reason"], t["moved_bash_run_wall"]) == (1, {"deliver_message": 1}, 300.0)
    text = invoke(tx, tmp_path / "md").stdout
    assert "↷ Moved to the background: 1 Bash call (1 to deliver a message); they ran 5m00s" in text
    assert "- 5m00s ↷ " in text


def test_every_reason_of_a_move_is_named(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    kinds: list[dict[str, Any]] = [
        {"after_ms": 120_000, "capped": False}, {"to_deliver_message": True}, {"by_user": True}, {"after_ms": None},
    ]  # fmt: skip
    for n, how in enumerate(kinds):
        moved = tx.request(10 + n * 100).bash(11 + n * 100, f"./step-{n}.sh")
        tx.moved_to_background(41 + n * 100, moved, f"b{n}", **how)
    tx.turn_end(500)
    t = json.loads(invoke(tx, tmp_path, "--json").stdout)["totals"]
    assert t["moved_bash_by_reason"] == {"timeout": 1, "deliver_message": 1, "user": 1, "other": 1}
    text = invoke(tx, tmp_path / "md").stdout
    assert (
        "↷ Moved to the background: 4 Bash calls (1 at their own timeout, 1 to deliver a message, 1 by the user,"
        " 1 for another reason)" in text
    )


def test_bash_time_adds_up_per_script_and_program(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    bash(tx, 1, "python3 scripts/digest.py run a", 30)
    bash(tx, 100, "uv run python scripts/digest.py b", 60)
    bash(tx, 200, "./bin/sweep.sh > out 2>&1; echo $?", 20)
    bash(tx, 300, "git status", 1)
    tx.turn_end(400)
    data = json.loads(invoke(tx, tmp_path, "--json").stdout)
    rows = {r["program"]: r for r in data["bash_programs"]}
    assert (rows["digest.py"]["n"], rows["digest.py"]["total"], rows["digest.py"]["max"]) == (2, 90.0, 60.0)
    assert (rows["sweep.sh"]["n"], rows["sweep.sh"]["group"], rows["git"]["group"]) == (1, "run", "git")
    assert [r["program"] for r in data["bash_programs"]] == ["digest.py", "sweep.sh", "git"]
    text = invoke(tx, tmp_path / "md").stdout
    assert "| `digest.py` | 2 | 1m30s | 45s | 1m00s | run | 2 | 0 |" in text


def described_session(session_id: str, cwd: str) -> Transcript:
    """Three indexing passes, one of them with a nested runner, and one profile run."""
    tx = Transcript(session_id=session_id, cwd=cwd)
    tx.prompt(0)
    for n, description in enumerate(("Indexing pass", "Indexing pass 13:17", "Indexing pass 14:47", "Profile 7")):
        t = 10 + n * 1000
        sub = tx.request(t).agent(t + 1, f"a{n}", description=description, background=True)
        tx.agent_returned(t + 2, sub)
        sub.request(t + 3).text(t + 63 + n * 60)
        if n == 0:
            child = sub.request(t + 64).agent(t + 65, "r0", description="run: tests")
            child.request(t + 66).text(t + 500)
            sub.agent_returned(t + 501, child)
    tx.turn_end(5000)
    return tx


def test_top_level_subagents_add_up_by_description(tmp_path: Path, isolated_home: Path) -> None:
    data = json.loads(invoke(described_session(SESSION_ID, CWD), tmp_path, "--json").stdout)
    rows = {r["description"]: r for r in data["subagent_descriptions"]}
    assert set(rows) == {"Indexing pass", "Profile N"}
    passes = rows["Indexing pass"]
    assert (passes["n"], passes["sessions"]) == (3, 1)
    assert passes["median"] == pytest.approx(180, abs=5) and passes["max"] > 400  # the first waited on its runner
    assert passes["elapsed"] == pytest.approx(passes["active"])  # they did not overlap
    assert rows["Profile N"]["n"] == 1 and data["subagent_descriptions"][0]["description"] == "Indexing pass"
    assert passes["roles"] == [{"role": "other", "guessed": True, "n": 3}]
    text = invoke(described_session(SESSION_ID, CWD), tmp_path / "md").stdout
    assert "| `Indexing pass` | 3 |" in text and " | other? 3 |" in text


def test_descriptions_and_cron_prompts_are_redacted_before_their_digits_are_replaced(
    tmp_path: Path, isolated_home: Path
) -> None:
    private = "db12.internal.example.com:5432 for 0123456789abcdef0123456789abcdef"
    tx = Transcript()
    tx.prompt(0)
    for n in range(2):
        sub = tx.request(10 + n * 100).agent(11 + n * 100, f"a{n}", description=f"Check {private}", background=True)
        tx.agent_returned(12 + n * 100, sub)
        sub.request(13 + n * 100).text(20 + n * 100)
    tx.turn_end(300)
    tx.scheduled(1000, f"Ping {private}")
    tx.request(1001).text(1010)
    tx.turn_end(1011)
    data = json.loads(invoke(tx, tmp_path, "--json").stdout)
    (row,) = data["subagent_descriptions"]
    (cron,) = data["totals"]["crons"]
    for text in (row["description"], cron["prompt"]):
        assert "internal" not in text and "abcdef" not in text and "dbN" not in text and "NfN" not in text, text
    assert row["n"] == 2 and data["totals"]["subagent_descriptions"] == 1
    timeline = json.loads(runner.invoke(app, ["timeline", str(tx.path(tmp_path / "projects")), "--json"]).stdout)
    assert timeline["totals"]["crons"][0]["prompt"] == cron["prompt"]


def test_the_descriptions_left_out_by_top_are_counted(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    for n, word in enumerate(("Alpha", "Beta", "Gamma")):
        sub = tx.request(10 + n * 1000).agent(11 + n * 1000, f"a{n}", description=word, background=True)
        tx.agent_returned(12 + n * 1000, sub)
        sub.request(13 + n * 1000).text(13 + n * 1000 + 60 * (3 - n))
    tx.turn_end(5000)
    data = json.loads(invoke(tx, tmp_path, "--json", "--top", "1").stdout)
    assert [r["description"] for r in data["subagent_descriptions"]] == ["Alpha"]
    assert (data["totals"]["subagent_descriptions"], data["totals"]["subagent_descriptions_active"]) == (3, 366.0)
    assert "… 2 more descriptions, 3m04s active" in invoke(tx, tmp_path / "md", "--top", "1").stdout


def test_subagent_descriptions_add_up_over_sessions(tmp_path: Path, isolated_home: Path) -> None:
    targets = [
        str(described_session(f"0000000{n}-0000-4000-8000-000000000000", f"/home/user/projects/d{n}").write(
            tmp_path / "projects"
        ))
        for n in range(2)
    ]  # fmt: skip
    rows = {r["description"]: r for r in tool_stats_many(targets, isolate=False)["subagent_descriptions"]}
    assert (rows["Indexing pass"]["n"], rows["Indexing pass"]["sessions"], rows["Profile N"]["n"]) == (6, 2, 2)


def filtered_session(session_id: str = SESSION_ID, cwd: str = CWD) -> Transcript:
    """The main agent runs a test; a tooling batch with a nested runner runs a build; a profile run reads a file."""
    tx = Transcript(session_id=session_id, cwd=cwd)
    tx.prompt(0)
    bash(tx, 1, "uv run pytest", 100)
    batch = tx.request(200).agent(201, "b1", description="Tooling batch 1", background=True)
    tx.agent_returned(202, batch)
    profile = tx.request(203).agent(204, "p1", description="Profile X", background=True)
    tx.agent_returned(205, profile)
    tx.turn_end(206)
    build = batch.request(210).bash(211, "make build")
    batch.result(271, build)
    runner_sub = batch.request(272).agent(273, "r1", description="run: tests", agent_type="test-runner")
    lint = runner_sub.request(274).bash(275, "uv run ruff check .")
    runner_sub.result(285, lint)
    runner_sub.request(286).text(287)
    batch.agent_returned(288, runner_sub)
    batch.request(289).text(290)
    read = profile.request(210).bash(211, "cat notes.md")
    profile.result(212, read)
    profile.request(213).text(214)
    return tx


def test_subagents_keeps_the_matching_subagents_and_their_nested_ones(tmp_path: Path, isolated_home: Path) -> None:
    data = json.loads(invoke(filtered_session(), tmp_path, "--json", "--subagents", "TOOLING batch").stdout)
    assert data["filter"] == {"subagents": "TOOLING batch", "lanes": 2}
    groups = {r["group"]: r for r in data["bash_groups"]}
    assert set(groups) == {"build", "lint"}  # not the main agent's test, not the profile's read
    assert (groups["build"]["subagents"], groups["build"]["main"]) == (1, 0)
    t = data["totals"]
    assert (t["main_calls"], t["main_busy"], t["subagents"], t["busy_by_trigger"]) == (0, 0, 2, {})
    assert {r["description"] for r in data["subagent_descriptions"]} == {"Tooling batch N"}
    text = invoke(filtered_session(), tmp_path / "md", "--subagents", "tooling batch").stdout
    assert "matches `tooling batch` (case-insensitive) and their nested subagents: 2 lanes." in text
    assert json.loads(invoke(filtered_session(), tmp_path / "all", "--json").stdout)["filter"] is None


def test_subagents_works_over_many_sessions_and_rejects_a_bad_regex(tmp_path: Path, isolated_home: Path) -> None:
    targets = [
        str(filtered_session(f"0000000{n}-0000-4000-8000-000000000000", f"/home/user/projects/f{n}").write(
            tmp_path / "projects"
        ))
        for n in range(2)
    ]  # fmt: skip
    data = tool_stats_many(targets, isolate=False, subagents="profile")
    assert data["filter"] == {"subagents": "profile", "lanes": 2}
    assert [r["group"] for r in data["bash_groups"]] == ["file-read"]
    result = runner.invoke(app, ["tool-stats", targets[0], "--subagents", "("])
    assert result.exit_code == 2 and "--subagents" in result.output


def test_an_old_transcript_says_permission_decisions_were_not_recorded(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    bash(tx, 1, "git status", 2)
    tx.turn_end(10)
    text = invoke(tx, tmp_path).stdout
    assert "not recorded: this Claude Code version wrote no permission decisions" in text


def many(tmp_path: Path) -> tuple[list[str], list[Path]]:
    """Two sessions in different repos that both run the same poll, and a third that is a worker by its start log."""
    paths = []
    for n, cwd in enumerate(("/home/user/projects/one", "/home/user/projects/two", "/home/user/projects/three")):
        tx = Transcript(session_id=f"0000000{n}-0000-4000-8000-000000000000", cwd=cwd)
        tx.prompt(0)
        bash(tx, 1, f"cat {cwd}/README.md", 2)
        for k in range(n + 1):
            bash(tx, 10 + k * 100, "sleep 60; pgrep -af gradle", 60)
        tx.turn_end(500)
        if n == 2:
            tx.log_start(tmp_path / "projects", 0, role="worker")
        paths.append(tx.write(tmp_path / "projects"))
    return [str(p) for p in paths], paths


def test_many_sessions_add_up(tmp_path: Path, isolated_home: Path) -> None:
    targets, _ = many(tmp_path)
    listing = tmp_path / "sessions.txt"
    listing.write_text("# long sessions\n" + "\n".join(targets) + "\n\n", encoding="utf-8")
    result = runner.invoke(app, ["tool-stats", "--sessions-from", str(listing), "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert {"sessions", "by_kind", "skipped", "totals", "repeated", "subagent_roles"} <= set(data)
    assert [r["kind"] for r in data["sessions"]] == ["interactive", "interactive", "worker"]
    row = data["sessions"][2]
    assert (row["top_group"], row["top_group_wall"], row["suspect_wait_wall"]) == ("wait", 180.0, 0.0)
    assert set(row) >= {"session_id", "kind", "active", "main_busy", "top_group", "suspect_wait_wall"}
    assert (row["compactions"], row["subagent_compactions"]) == (0, 0)
    assert data["totals"]["busy_by_trigger"] == {"human": {"turns": 3, "seconds": 1500.0}}
    assert data["totals"]["crons"] == []
    poll = next(r for r in data["repeated"] if r["command"] == "sleep N; pgrep -af gradle")
    assert (poll["n"], poll["sessions"], poll["total"]) == (6, 3, 360.0)
    assert data["by_kind"]["interactive"]["sessions"] == 2 and data["by_kind"]["worker"]["bash_groups"]["wait"] == 180
    assert data["totals"]["calls"] == 9 and isinstance(data["totals"]["calls"], int)
    commands = {r["command"] for r in data["slowest"]}
    assert "cat <repo>/README.md" in commands and not any("/home/" in c for c in commands)  # each in its own repo


def test_every_user_wait_is_kept_and_counted_per_tool(tmp_path: Path, isolated_home: Path) -> None:
    targets = []
    for n, tools in enumerate([["AskUserQuestion"] * 3 + ["ExitPlanMode"], ["AskUserQuestion"] + ["ExitPlanMode"] * 2]):
        tx = Transcript(session_id=f"0000000{n}-0000-4000-8000-000000000000", cwd=f"/home/user/projects/w{n}")
        tx.prompt(0)
        for k, tool in enumerate(tools):
            ask = tx.request(10 + k * 100).call(11 + k * 100, tool, question="Which one?")
            tx.result(11 + k * 100 + 10 * (k + 1), ask, permission=permission("user_temporary"))
        tx.turn_end(1000)
        targets.append(str(tx.write(tmp_path / "projects")))
    p = tool_stats_many(targets, top=1, isolate=False)["permissions"]
    assert len(p["user_waits"]) == p["user_answered"] == 7  # not cut to the top
    assert p["user_waits_by_tool"] == {
        "AskUserQuestion": {"n": 4, "wall": 70.0},
        "ExitPlanMode": {"n": 3, "wall": 90.0},
    }
    assert sum(row["wall"] for row in p["user_waits_by_tool"].values()) == p["user_wait"]
    listing = tmp_path / "sessions.txt"
    listing.write_text("\n".join(targets) + "\n", encoding="utf-8")
    out = runner.invoke(app, ["tool-stats", "--sessions-from", str(listing)]).stdout.splitlines()
    answered = next(line for line in out if line.startswith("Prompts the user answered: 7,"))
    assert answered.endswith(" and 1 shorter")
    assert "By tool: ExitPlanMode 3 1m30s · AskUserQuestion 4 1m10s" in out


def test_a_session_that_fails_is_skipped(tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from cc_session_tools.session_time import analyze

    targets, _ = many(tmp_path)

    def failing(transcript: Any) -> Any:
        if transcript.session_id.startswith("00000001"):
            raise ValueError("the transcript of /home/user/projects/two is broken")
        return analyze(transcript)

    monkeypatch.setattr("cc_session_tools.tool_stats_cmd.analyze", failing)
    listing = [targets[0], "00000000-dead-4000-8000-000000000000", targets[1], targets[2]]
    data = tool_stats_many(listing, isolate=False)  # in this process, so the patch applies
    assert [s["session_id"][:8] for s in data["sessions"]] == ["00000000", "00000002"]
    assert data["skipped"] == [
        {"target": "00000000-dead-4000-8000-000000000000", "error": "LookupError: could not resolve the session"},
        {"target": targets[1].replace("-home-user", "~"),
         "error": "ValueError: the transcript of ~/projects/two is broken"},
    ]  # fmt: skip


def test_a_session_that_fails_in_its_child_process_is_skipped(tmp_path: Path, isolated_home: Path) -> None:
    targets, _ = many(tmp_path)
    listing = "\n".join([targets[0], "00000000-dead-4000-8000-000000000000", targets[2]])
    result = runner.invoke(app, ["tool-stats", "--sessions-from", "-", "--json"], input=listing)
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert [s["session_id"][:8] for s in data["sessions"]] == ["00000000", "00000002"]
    assert "Warning: skipped 00000000-dead-4000-8000-000000000000: LookupError" in result.stderr


def test_many_sessions_in_markdown(tmp_path: Path, isolated_home: Path) -> None:
    targets, _ = many(tmp_path)
    result = runner.invoke(app, ["tool-stats", "--sessions-from", "-"], input="\n".join(targets))
    out = result.stdout.splitlines()
    assert out[0] == "# Tool stats over 3 sessions"
    assert "## Sessions, by active time" in out and "## By session kind" in out
    assert any(line.startswith("| 00000002 | 2026-11-02 | worker |") and "| wait 3m00s |" in line for line in out)
    assert any(line.startswith("- ×6 in 3 sessions total 6m00s (max 1m00s) [wait]") for line in out)
    assert any(line.startswith("- 1m00s 0000000") for line in out)  # the slowest calls name their session
    assert "not recorded: this Claude Code version wrote no permission decisions, so only dialogs count below" in out


def test_an_ambiguous_prefix_in_the_list_is_skipped(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    many(tmp_path)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
    data = tool_stats_many(["00000002", "0000000"], isolate=True)
    assert [s["session_id"][:8] for s in data["sessions"]] == ["00000002"]
    assert data["skipped"] == [
        {"target": "0000000", "error": "LookupError: 3 sessions match '0000000' by session id prefix: "
         + ", ".join(f"0000000{n}-0000-4000-8000-000000000000" for n in range(3))},
    ]  # fmt: skip
    result = runner.invoke(app, ["tool-stats", "0000"])
    assert result.exit_code == 1 and "Error: 3 sessions match '0000' by session id prefix:" in result.stderr


def test_a_target_and_a_list_exclude_each_other(tmp_path: Path, isolated_home: Path) -> None:
    targets, _ = many(tmp_path)
    assert runner.invoke(app, ["tool-stats"]).exit_code == 1
    result = runner.invoke(app, ["tool-stats", targets[0], "--sessions-from", "-"], input="")
    assert result.exit_code == 1 and "either a TARGET or --sessions-from" in result.output
    result = runner.invoke(app, ["tool-stats", "--sessions-from", str(tmp_path / "missing.txt")])
    assert result.exit_code == 1 and "--sessions-from" in result.output
