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
    """Print times in UTC, so 09:00 in a test is 09:00 in the output, with the home dir the transcripts name."""
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("HOME", "/home/user")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def invoke(tx: Transcript, tmp_path: Path, *args: str) -> Any:
    path = tx.write(tmp_path / "projects")
    return runner.invoke(app, ["timeline", str(path), *args])


def lines_of(tx: Transcript, tmp_path: Path, *args: str) -> list[str]:
    result = invoke(tx, tmp_path, *args)
    assert result.exit_code == 0, result.output
    return list(result.stdout.splitlines())


def session() -> Transcript:
    """A long human turn with a dialog and a subagent, then short turns, a long idle gap and a cron turn."""
    tx = Transcript()
    tx.prompt(0, "Plan the change to /home/user/projects/example/a.py with token=abcdef123456")
    r = tx.request(1)
    sub = r.agent(2, "a1", description="validate: C4")
    sub.request(3).text(300)
    tx.agent_returned(301, sub)
    ask = tx.request(302).call(303, "AskUserQuestion", question="Which one?")
    tx.result(400, ask, permission=permission("user_temporary"))
    tx.request(401).text(410)
    tx.turn_end(411)
    for n in range(3):
        t = 500 + n * 60
        tx.prompt(t, f"Short {n}.")
        call = tx.request(t + 1).bash(t + 2, "git status")
        tx.result(t + 3, call)
        tx.turn_end(t + 5)
    tx.scheduled(3 * HOUR, wakeup=False)
    tx.request(3 * HOUR + 1).text(3 * HOUR + 10)
    tx.turn_end(3 * HOUR + 11)
    return tx


def test_the_markdown_prints_long_turns_merges_short_ones_and_shows_gaps(tmp_path: Path, isolated_home: Path) -> None:
    out = lines_of(session(), tmp_path)
    text = "\n".join(out)
    assert out[0].startswith("# Timeline ")
    assert any(line.startswith("- main agent busy 7m") for line in out)
    assert '09:00-09:06 T human "Plan the change to <repo>/a.py with token=<redact…" 6m51s' in text
    assert "    ⏸ 09:05 user 1m37s on AskUserQuestion → user_temporary" in text
    assert "    └ fg general-purpose claude-opus-5-5" not in text
    assert "    └ fg general-purpose opus-5-5 [validate] 09:00-09:05 4m58s" in text
    assert "⋯ 3 turns (human×3) busy 15s" in text
    assert "09:10-12:00 … away 2h49m → cron" in text


def test_prompts_and_commands_are_redacted(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0, "Use the key sk-abcdefghijklmnop from https://internal.example.com/x " * 2)
    tx.request(1).text(400)
    tx.turn_end(401)
    text = "\n".join(lines_of(tx, tmp_path))
    assert "sk-abcdefghijklmnop" not in text and '"Use the key <redacted> from https://<host>/x Use…"' in text


def test_a_long_session_fits_max_lines_in_compact_mode(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    for n in range(200):
        t = n * 1800  # every turn is followed by a 30-minute gap
        tx.prompt(t, f"Round {n}.", origin="peer", sender="other-session")
        sub = tx.request(t + 1).agent(t + 2, f"b{n}", description=f"run: S{n}", background=True)
        tx.agent_returned(t + 3, sub)
        sub.request(t + 4).text(t + 60)
        tx.turn_end(t + 5)
    out = lines_of(tx, tmp_path, "--max-lines", "60")
    assert len(out) <= 60
    assert any(line.startswith("(compact mode:") for line in out)
    assert any(line.startswith("## ") and "main busy" in line for line in out)


def test_from_and_to_show_a_stretch_in_full(tmp_path: Path, isolated_home: Path) -> None:
    out = lines_of(session(), tmp_path, "--from", "09:08", "--to", "09:12")
    assert not any(" ⋯ " in line for line in out)
    assert [line[:19] for line in out if " T " in line] == [
        "09:08-09:08 T human",
        "09:09-09:09 T human",
        "09:10-09:10 T human",
    ]
    assert not any("T cron" in line for line in out)  # the cron turn at 12:00 is outside the window


def test_a_bad_time_or_gap_or_target_fails(tmp_path: Path, isolated_home: Path) -> None:
    assert invoke(session(), tmp_path, "--from", "noon").exit_code == 1
    assert invoke(session(), tmp_path, "--gap", "soon").exit_code == 1
    assert invoke(session(), tmp_path, "--subagent", "nope").exit_code == 1
    result = runner.invoke(app, ["timeline", "00000000-0000-0000-0000-000000000000"])
    assert result.exit_code == 1 and "could not resolve" in result.output


def test_one_subagent_shows_its_bursts_calls_and_children(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    parent = tx.request(1).agent(2, "p1", description="impl: C5", background=True)
    tx.agent_returned(3, parent)
    tx.turn_end(4)
    r = parent.request(5)
    test = r.bash(6, "uv run pytest tests/test_a.py")
    parent.result(66, test)
    child = parent.request(67).agent(68, "c1", agent_type="test-runner", description="run: lint")
    child.request(69).text(100)
    parent.agent_returned(101, child)
    tx.request(1000).send_message(1001, parent, "Re-check.")
    parent.request(1002).text(1010)
    out = lines_of(tx, tmp_path, "--subagent", "p1")
    text = "\n".join(out)
    assert out[0] == f"# Subagent p1 of {tx.session_id[:8]}"
    assert "09:00-09:01 burst 1 1m39s: Bash:test Agent" in text
    assert "    └ fg test-runner opus-5-5 [run] 09:01-09:01 32s" in text
    assert "09:16-09:16 burst 2 9s" in text


def test_a_call_over_an_hour_is_a_suspected_wait(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    batch = tx.request(1).bash(2, "./run-the-long-batch.sh")
    tx.result(5 * HOUR, batch)
    tx.request(5 * HOUR + 1).text(5 * HOUR + 2)
    tx.turn_end(5 * HOUR + 3)
    text = "\n".join(lines_of(tx, tmp_path))
    assert "- ⌛ 1 calls over 1 h, suspected waits: main busy 4h59m and subagent active 0s are inside them" in text
    assert "    ⌛ 09:00 Bash `./run-the-long-batch.sh` 4h59m, suspected wait" in text
    data = json.loads(invoke(tx, tmp_path / "json", "--json").stdout)
    assert [c["suspect_wait"] for c in data["calls"]] == [True]
    assert data["totals"]["suspect_waits"] == 1 and data["totals"]["suspect_wait_main_busy"] == 5 * HOUR - 2


def test_the_json_has_every_turn_call_subagent_and_compaction_unmerged(tmp_path: Path, isolated_home: Path) -> None:
    tx = session()
    tx.compaction(4 * HOUR, trigger="manual", duration_ms=30_000)
    data = json.loads(invoke(tx, tmp_path, "--json").stdout)
    assert set(data) == {
        "session_id", "kind", "kind_source", "start", "end", "has_permission_decisions", "window",
        "totals", "main", "turns", "gaps", "calls", "subagents", "compactions", "stalls",
        "api_errors",
    }  # fmt: skip
    assert [t["trigger"] for t in data["turns"]] == ["human", "human", "human", "human", "cron"]
    assert data["turns"][0]["prompt"] == "Plan the change to <repo>/a.py with token=<redact…"
    bash = [c for c in data["calls"] if c["name"] == "Bash"]
    assert {(c["bash_group"], c["command"], c["turn"]) for c in bash} == {
        ("git", "git status", 1), ("git", "git status", 2), ("git", "git status", 3),
    }  # fmt: skip
    assert data["subagents"][0]["role"] == "validate" and data["subagents"][0]["role_source"] == "prefix"
    assert data["compactions"] == [
        {
            "time": "2026-11-02T13:00:00+00:00",
            "lane": "main",
            "trigger": "manual",
            "duration_ms": 30000,
            "wait_seconds": 30.0,
            "pre_tokens": 180000,
            "post_tokens": None,
            "segment": 1,
        }
    ]
    assert data["totals"]["user_waits"] == 97
    assert data["gaps"][-1]["away"] is True


def test_an_api_error_shows_its_message(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "a1", description="run: tests")
    sub.request(3).text(10)
    sub.api_error(11, "API Error: Overloaded", error="overloaded", status=529)
    tx.agent_returned(12, sub)
    tx.request(13).text(20)
    tx.api_error(21, "API Error: Rate limited", error="rate_limit", status=429)
    tx.turn_end(22)
    tx.scheduled(600, wakeup=False)
    tx.api_error(601)
    tx.turn_end(602)
    out = lines_of(tx, tmp_path)
    assert "09:00 ! API error rate_limit 429: API Error: Rate limited" in out
    assert "09:10-09:10 ✖ context full: 1 prompt refused, stalled 0s, never recovered" in out
    data = json.loads(invoke(tx, tmp_path / "json", "--json").stdout)
    assert data["api_errors"] == [
        {"time": "2026-11-02T09:00:21+00:00", "lane": "main", "error": "rate_limit", "status": 429,
         "message": "API Error: Rate limited"},
        {"time": "2026-11-02T09:10:01+00:00", "lane": "main", "error": "invalid_request", "status": None,
         "message": "Prompt is too long"},
        {"time": "2026-11-02T09:00:11+00:00", "lane": "a1", "error": "overloaded", "status": 529,
         "message": "API Error: Overloaded"},
    ]  # fmt: skip


def test_a_stall_on_a_full_context_is_one_line_and_not_away(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(5)
    tx.turn_end(10)
    for t in (HOUR, 2 * HOUR, 3 * HOUR):
        tx.scheduled(t)
        tx.api_error(t)
    tx.local_command(4 * HOUR)
    tx.compaction(4 * HOUR, trigger="manual", duration_ms=20)
    tx.prompt(4 * HOUR + 60, "Go on.")
    tx.request(4 * HOUR + 61).text(4 * HOUR + 65)
    tx.turn_end(4 * HOUR + 70)
    out = lines_of(tx, tmp_path)
    assert "10:00-13:00 ✖ context full: 3 prompts refused, stalled 3h00m → compaction" in out
    assert not any("… away" in line or "Prompt is too long" in line for line in out)
    assert any(line.startswith("- ✖ stalls 1 (3h00m, context full: 3 prompts refused)") for line in out)
    data = json.loads(invoke(tx, tmp_path / "json", "--json").stdout)
    assert data["stalls"] == [
        {"lane": "main", "start": "2026-11-02T10:00:00+00:00", "end": "2026-11-02T13:00:00+00:00",
         "seconds": 3 * HOUR, "refused": 3, "ended_by": "compaction"},
    ]  # fmt: skip
    t = data["totals"]
    assert (t["stalled"], t["stalls"], t["context_full_errors"], t["away"]) == (3 * HOUR, 1, 3, 0)
    assert data["gaps"][0]["stalled_seconds"] == 3 * HOUR and data["gaps"][0]["away"] is False


def test_a_bash_call_moved_to_the_background_shows_its_runtime(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    moved = tx.request(1).bash(2, "sleep 900")
    tx.moved_to_background(122, moved, "b1", after_ms=120_000, capped=False)
    tx.turn_end(123)
    tx.task_notification(902, "b1", call_id=moved)
    tx.request(903).text(904)
    tx.turn_end(905)
    data = json.loads(invoke(tx, tmp_path, "--json").stdout)
    c = next(c for c in data["calls"] if c["id"] == moved)
    assert {k: c[k] for k in ("wall", "run_wall", "auto_backgrounded", "moved_after", "capped", "finish_status")} == {
        "wall": 120.0, "run_wall": 900.0, "auto_backgrounded": True, "moved_after": 120.0, "capped": False,
        "finish_status": "completed",
    }  # fmt: skip
    assert c["finished_at"] == "2026-11-02T09:15:02+00:00" and c["background_task"] == "b1"
    assert data["totals"]["main_busy"] < 300 and data["totals"]["tools"] == 120  # blocked until the move only
    text = invoke(tx, tmp_path / "md").stdout
    assert "· ↷ 1 Bash call moved to the background, ran 15m\n" in text


def test_compactions_count_the_main_agents_with_the_subagents_beside_them(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "a1", description="run: tests", background=True)
    tx.agent_returned(3, sub)
    sub.request(4).text(10)
    sub.compaction(20)
    sub.request(21).text(30)
    tx.turn_end(40)
    tx.local_command(100)
    tx.compaction(100, trigger="manual", duration_ms=20)
    tx.prompt(200)
    tx.request(201).text(205)
    tx.compaction(206)
    tx.request(267).text(270)
    tx.turn_end(271)
    out = lines_of(tx, tmp_path)
    assert any(
        line.startswith("- compactions 2 main (1 manual, 1 auto) · 1 in 1 subagent · API errors") for line in out
    )
    data = json.loads(invoke(tx, tmp_path / "json", "--json").stdout)
    t = data["totals"]
    assert {k: t[k] for k in ("compactions", "compactions_manual", "compactions_auto")} == {
        "compactions": 2, "compactions_manual": 1, "compactions_auto": 1,
    }  # fmt: skip
    assert (t["subagent_compactions"], t["subagents_compacted"]) == (1, 1)
    assert len(data["compactions"]) == 3  # the list has every lane


def test_the_json_splits_tests_into_targeted_and_full(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "a1", description="run: tests")
    run_one = sub.request(3).bash(4, "uv run pytest tests/test_x.py::test_y")
    sub.result(10, run_one)
    sub.request(11).text(12)
    tx.agent_returned(13, sub)
    full = tx.request(14).bash(15, "uv run pytest")
    tx.result(20, full)
    lint = tx.request(21).bash(22, "uv run ruff check .")
    tx.result(23, lint)
    tx.turn_end(24)
    data = json.loads(invoke(tx, tmp_path, "--json").stdout)
    groups = {c["id"]: c["bash_group"] for c in data["calls"]}
    assert (groups[run_one], groups[full], groups[lint]) == ("test (targeted)", "test (full)", "lint")
    result = runner.invoke(app, ["timeline", str(tx.path(tmp_path / "projects")), "--subagent", "a1", "--json"])
    assert [c["bash_group"] for c in json.loads(result.stdout)["calls"]] == ["test (targeted)"]


def test_a_session_across_a_dst_change_names_both_zones(
    tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "Europe/Prague")
    time.tzset()
    tx = Transcript()
    tx.prompt(-10 * 24 * HOUR)  # 2026-10-23, still summer time
    tx.request(-10 * 24 * HOUR + 1).text(-10 * 24 * HOUR + 5)
    tx.turn_end(-10 * 24 * HOUR + 6)
    tx.prompt(0)
    tx.request(1).text(5)
    tx.turn_end(6)
    out = lines_of(tx, tmp_path)
    assert out[2] == "2026-10-23 11:00 → 2026-11-02 10:00 (local time, CEST → CET) · kind interactive (origins)"
    one_zone = Transcript()
    one_zone.prompt(0)
    one_zone.request(1).text(5)
    one_zone.turn_end(6)
    assert "(local time, CET) ·" in lines_of(one_zone, tmp_path / "one")[2]


def test_the_json_of_a_window_keeps_only_its_records(tmp_path: Path, isolated_home: Path) -> None:
    data = json.loads(invoke(session(), tmp_path, "--json", "--from", "09:08", "--to", "09:12").stdout)
    assert [t["index"] for t in data["turns"]] == [1, 2, 3]
    assert data["window"] == {"start": "2026-11-02T09:08:00+00:00", "end": "2026-11-02T09:12:00+00:00"}


def test_subagent_active_is_the_union_and_the_lane_time_counts_a_parent_and_its_nested_child(
    tmp_path: Path,
) -> None:
    """A parent waits on its nested child for 30 minutes; the clock ran 30 minutes, the lanes add up to 60."""
    tx = Transcript()
    tx.prompt(0)
    parent = tx.request(1).agent(2, "p1", description="impl: S1")
    child = parent.request(3).agent(4, "c1", description="run: S1 tests")
    child.result(1800, child.request(5).bash(5, "make test"))
    parent.agent_returned(1801, child)
    parent.request(1802).text(1803)
    tx.agent_returned(1804, parent)
    tx.request(1805).text(1806)
    tx.turn_end(1806)

    header = next(line for line in lines_of(tx, tmp_path) if line.startswith("- subagents "))
    assert "subagent active 30m (lane time 59m)" in header
    totals = json.loads(invoke(tx, tmp_path, "--json").stdout)["totals"]
    assert (totals["subagent_active"], totals["subagent_lane_time"]) == (1801, 1801 + 1796)


def test_the_json_of_one_subagent(tmp_path: Path, isolated_home: Path) -> None:
    data = json.loads(invoke(session(), tmp_path, "--json", "--subagent", "a1").stdout)
    assert (data["agent_id"], data["shape"], data["role"], data["children"], data["calls"]) == (
        "a1",
        "foreground",
        "validate",
        [],
        [],
    )


def work(tx: Transcript, start: float, end: float) -> None:
    """Model activity from ``start`` to ``end``, a response every two minutes."""
    for t in range(int(start), int(end), 120):
        tx.request(t).text(t + 1)
    tx.request(end - 1).text(end)


def test_a_session_over_many_days_counts_each_day_header_as_two_lines(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    for n in range(9 * 12):  # a turn every two hours for nine days, each with a subagent
        t = n * 2 * HOUR
        tx.prompt(t, f"Round {n}.", origin="peer", sender="other-session")
        sub = tx.request(t + 1).agent(t + 2, f"d{n}", description=f"run: S{n}", background=True)
        tx.agent_returned(t + 3, sub)
        work(sub, t + 4, t + 60 * (3 + n % 20))
        work(tx, t + 4, t + 60 * (6 + n % 30))
        tx.turn_end(t + 60 * (6 + n % 30) + 1)
    for limit in (60, 100, 150):
        out = lines_of(tx, tmp_path / str(limit), "--max-lines", str(limit))
        assert len(out) <= limit
        assert len(out) >= limit - 10  # compact mode fills the budget instead of jumping far below it


def compact_day(tmp_path: Path) -> list[str]:
    """A day of short turns, each launching subagents, too many to print in full in 40 lines."""
    tx = Transcript()
    for n in range(60):
        t = n * 25 * 60
        tx.prompt(t, f"Round {n}.", origin="peer", sender="other-session")
        r = tx.request(t + 1)
        runners = [r.agent(t + 2 + k, f"r{n}-{k}", description="Run the checks", agent_type="test-runner",
                           background=True) for k in range(4)]  # fmt: skip
        long = r.agent(t + 7, f"v{n}", description=f"validate: S{n}", background=True)
        for k, sub in enumerate([*runners, long]):
            tx.agent_returned(t + 8 + k, sub)
        for k, sub in enumerate(runners):
            sub.request(t + 20 + k).text(t + 26 + k)
        work(long, t + 20, t + 20 + (16 if n % 3 == 0 else 3) * 60)
        tx.turn_end(t + 30)
    return lines_of(tx, tmp_path, "--max-lines", "40")


def test_compact_mode_shows_the_long_subagents_and_the_days_subagent_totals(
    tmp_path: Path, isolated_home: Path
) -> None:
    out = compact_day(tmp_path)
    text = "\n".join(out)
    assert len(out) <= 40 and any(line.startswith("(compact mode:") for line in out)
    day = out.index("## Mon 2026-11-02 — main busy 18m in 36 turns (peer×36)")
    assert out[day - 1] == ""
    assert out[day + 1].startswith("- 180 top-level subagents launched, active 5h29m: validate×36 4h31m · run×144 57m;")
    assert '144× "Run the checks" 57m' in out[day + 1]
    assert out[day + 2] == '09:00 peer→bg general-purpose opus-5-5 [validate] 09:00-09:16 16m 185k tok "validate: S0"'
    assert "test-runner" not in text.replace(out[day + 1], "")  # the 24-second runners are left out


def test_compact_mode_groups_more_than_three_subagents_of_a_short_turn(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    for n in range(200):
        t = n * 1800
        tx.prompt(t, f"Round {n}.", origin="peer", sender="other-session")
        r = tx.request(t + 1)
        subs = [r.agent(t + 2 + k, f"g{n}-{k}", description="Check the build", background=True) for k in range(5)]
        for k, sub in enumerate(subs):
            tx.agent_returned(t + 8 + k, sub)
            work(sub, t + 20, t + 20 + (20 * 60 if n == 0 else 30))
        tx.turn_end(t + 30)
    out = lines_of(tx, tmp_path, "--max-lines", "60")
    grouped = [line for line in out if line.startswith("09:00 peer→")]
    assert grouped == [grouped[0]] and grouped[0].startswith("09:00 peer→5× bg general-purpose [run?] 20m–20m")


def test_the_markdown_details(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0, "Delete the old files.")
    rm = tx.request(1).bash(2, "rm -rf build")
    tx.result(90, rm, permission=permission("user_reject", decision="reject"), denial="user-rejected")
    tx.request(91).text(400)
    tx.turn_end(401)
    tx.compaction(500, trigger="manual", duration_ms=30_000, pre_tokens=0)
    tx.prompt(600, "Next.")
    tx.request(601).text(602)
    tx.turn_end(603)
    out = lines_of(tx, tmp_path)
    text = "\n".join(out)
    assert "    ⏸ 09:00 user 1m28s on Bash `rm -rf build` → user_reject" in out
    assert "09:08 ≡ compaction manual 30s 0→0, segment 1" in out
    assert " in 2 turns:" in text


def test_a_single_turn_is_one_turn(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(2)
    tx.turn_end(3)
    assert any(" in 1 turn:" in line for line in lines_of(tx, tmp_path))


def test_a_grouped_subagent_line_keeps_the_guess_mark(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    tx.prompt(0, "Fan out.")
    r = tx.request(1)
    subs = [r.agent(2 + k, f"f{k}", description="Review the parser", background=True) for k in range(5)]
    for k, sub in enumerate(subs):
        tx.agent_returned(8 + k, sub)
        sub.request(20).text(80)
    tx.turn_end(30)
    assert any(line.startswith("    └ 5× bg general-purpose [validate?] ") for line in lines_of(tx, tmp_path))


def test_from_after_the_session_ends_says_so(tmp_path: Path, isolated_home: Path) -> None:
    result = invoke(session(), tmp_path, "--from", "23:00")
    assert result.exit_code == 1 and "--from 23:00 is after the session ends (2026-11-02 12:00)" in result.output
    result = invoke(session(), tmp_path / "to", "--to", "2026-11-01T10:00:00+00:00")
    assert result.exit_code == 1 and "is before the session starts" in result.output


def test_a_subagent_is_found_by_its_file_name_and_its_json_keeps_the_window(
    tmp_path: Path, isolated_home: Path
) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "w1", description="impl: C5", background=True)
    tx.agent_returned(3, sub)
    tx.turn_end(4)
    for n in range(3):
        call = sub.request(10 + n * 600).bash(11 + n * 600, f"make step{n}")
        sub.result(20 + n * 600, call)
    path = tx.write(tmp_path / "projects")
    result = runner.invoke(app, ["timeline", str(path), "--subagent", "agent-w1"])
    assert result.exit_code == 0 and result.stdout.startswith("# Subagent w1 of ")
    result = runner.invoke(
        app, ["timeline", str(path), "--subagent", "w1", "--json", "--from", "09:05", "--to", "09:15"]
    )
    data = json.loads(result.stdout)
    assert [c["command"] for c in data["calls"]] == ["make step1"] and data["window"] is not None


def test_a_session_id_prefix_is_a_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    projects = tmp_path / "projects"
    for n in (1, 2):
        tx = Transcript(session_id=f"aaaa{n}{n}{n}{n}-0000-4000-8000-000000000000", cwd=f"/home/user/projects/p{n}")
        tx.prompt(0, f"Do step {n}.")
        tx.turn_end(10)
        tx.write(projects)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    result = runner.invoke(app, ["timeline", "aaaa2222"])
    assert result.exit_code == 0, result.output
    assert "aaaa2222" in result.stdout.splitlines()[0]
    result = runner.invoke(app, ["timeline", "aaaa"])
    assert result.exit_code == 1 and result.stdout == ""
    errors = result.stderr.splitlines()
    assert errors[0] == "Error: 2 sessions match 'aaaa' by session id prefix:"
    assert [line.split()[0] for line in errors[1:]] == [
        "aaaa1111-0000-4000-8000-000000000000", "aaaa2222-0000-4000-8000-000000000000",
    ]  # fmt: skip


def test_a_subagent_file_as_target_points_to_the_subagent_option(tmp_path: Path, isolated_home: Path) -> None:
    path = session().write(tmp_path / "projects")
    sub_file = path.with_suffix("") / "subagents" / "agent-a1.jsonl"
    assert sub_file.is_file()
    result = runner.invoke(app, ["timeline", str(sub_file)])
    assert result.exit_code == 1 and "pass its session and --subagent agent-a1" in result.output


def test_a_bad_bash_groups_file_fails(tmp_path: Path, isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(isolated_home))
    groups = isolated_home / ".config" / "cc-session-tools" / "bash-groups.toml"
    groups.parent.mkdir(parents=True)
    groups.write_text("[programs]\nmy-db-cli = \n", encoding="utf-8")
    result = invoke(session(), tmp_path)
    assert result.exit_code == 1 and "bash-groups.toml" in result.output and result.stdout == ""


def test_paths_of_the_session_are_redacted(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript(cwd="/home/user/projects/example/.worktrees/feature")
    tx.prompt(
        0, "Read /tmp/claude-1000/-home-user-projects-example/0b1c2d3e-0000-4000-8000-000000000000/tasks/b1.output"
    )
    call = tx.request(1).bash(2, "cat /home/user/projects/example/README.md ~/.claude/projects/-home-user-other/x")
    tx.result(3, call)
    tx.request(4).text(400)
    tx.turn_end(401)
    path = tx.write(tmp_path / "projects")
    text = runner.invoke(app, ["timeline", str(path)]).stdout
    assert '"Read <claude-tmp>/tasks/b1.output"' in text
    data = json.loads(runner.invoke(app, ["timeline", str(path), "--json"]).stdout)
    assert data["calls"][0]["command"] == "cat <repo>/README.md ~/.claude/projects/~-other/x"


def test_the_day_line_counts_top_level_subagents_and_background_compactions(
    tmp_path: Path, isolated_home: Path
) -> None:
    tx = Transcript()
    for n in range(100):
        t = n * 1800
        tx.prompt(t, f"Round {n}.", origin="peer", sender="other-session")
        parent = tx.request(t + 1).agent(t + 2, f"p{n}", description=f"impl: S{n}", background=True)
        tx.agent_returned(t + 3, parent)
        child = parent.request(t + 10).agent(t + 11, f"c{n}", description="run: tests", agent_type="test-runner")
        work(child, t + 12, t + 200)
        parent.agent_returned(t + 201, child)
        parent.request(t + 202).text(t + 240)
        tx.turn_end(t + 30)
        if n in (5, 6, 60):
            tx.compaction(t + 40, duration_ms=15)
    out = lines_of(tx, tmp_path, "--max-lines", "30")
    day = next(i for i, line in enumerate(out) if line.startswith("## Mon 2026-11-02"))
    assert out[day].endswith(" · 2 background compactions (segments 1, 2)")
    assert next(line for line in out if line.startswith("## Tue 2026-11-03")).endswith(
        " · 1 background compaction (segment 3)"
    )
    assert out[day + 1].startswith("- 30 top-level subagents launched (+30 nested), active 1h59m: implement×30 1h59m")
    assert not any("≡ compaction" in line for line in out)


def test_a_session_that_cannot_fit_says_so(tmp_path: Path, isolated_home: Path) -> None:
    tx = Transcript()
    for n in range(30):  # thirty days, a long turn each
        t = n * 24 * HOUR
        tx.prompt(t, f"Day {n}.")
        work(tx, t + 1, t + 7 * HOUR)
        tx.turn_end(t + 7 * HOUR + 1)
    out = lines_of(tx, tmp_path, "--max-lines", "40")
    assert any(
        line.startswith("(this session does not fit in 40 lines even at the largest thresholds;") for line in out
    )
    assert any(" T human " in line for line in out)  # a 7-hour turn is over the 6-hour cap, so it stays
