"""The cheap activity scan behind ``search --durations`` and ``--min-active``.

Every test of ``test_session_time`` also checks that the scan agrees with ``analyze`` (see its ``run`` helper); these
tests cover what only the scan does: its prefilter bound, and the ``search`` options that use it.
"""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

import pytest
from cc_blackbox import project_slug, read_session_transcript
from conftest import sid
from transcripts import CWD, START, Transcript
from typer.testing import CliRunner

import cc_session_tools.discovery as discovery
import cc_session_tools.search_cmd as search_cmd
from cc_session_tools.cli import app
from cc_session_tools.session_activity import active_upper_bound, scan, scan_all

runner = CliRunner()
HOUR = 3600
LONG, SHORT, IDLE = sid("long-active"), sid("short-active"), sid("long-idle")


def session(session_id: str, *, active: float, idle: float = 0, start: float = 0) -> Transcript:
    """A session with a turn of ``active`` seconds that runs one command, then ``idle`` seconds later a one-second
    turn."""
    tx = Transcript(session_id=session_id)
    tx.prompt(start)
    tx.result(start + active, tx.request(start + 1).bash(start + 1, "make test"))
    tx.turn_end(start + active)
    if idle:
        tx.prompt(start + active + idle)
        tx.request(start + active + idle + 1).text(start + active + idle + 1)
        tx.turn_end(start + active + idle + 1)
    return tx


@pytest.fixture
def projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Three sessions in one project: active for 3 h, for 30 min, and for 2 s spread over 5 h."""
    projects = tmp_path / "projects"
    session(LONG, active=3 * HOUR, start=600).write(projects)
    session(SHORT, active=30 * 60, start=300).write(projects)
    session(IDLE, active=1, idle=5 * HOUR).write(projects)
    for session_id, last in ((LONG, 600 + 3 * HOUR), (SHORT, 300 + 30 * 60), (IDLE, 5 * HOUR + 2)):
        written(projects / project_slug(CWD) / f"{session_id}.jsonl", last)
    monkeypatch.setenv("CLAUDE_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("COLUMNS", "250")
    monkeypatch.setattr(os, "getcwd", lambda: CWD)
    monkeypatch.setattr(discovery, "main_repo_root", lambda _cwd: None)
    return projects


def written(path: Path, t: float) -> None:
    """Set the file's mtime to ``t`` seconds after :data:`START`, when Claude Code would have written it last."""
    moment = (START + timedelta(seconds=t)).timestamp()
    os.utime(path, (moment, moment))


def search_json(*args: str) -> list[dict[str, object]]:
    result = runner.invoke(app, ["search", "--json", *args])
    assert result.exit_code == 0, result.output
    rows: list[dict[str, object]] = json.loads(result.stdout)
    return rows


def test_the_bound_counts_a_subagent_that_outlives_the_main_transcript(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "b1", background=True)
    tx.agent_returned(3, sub)
    tx.turn_end(4)
    sub.request(5).text(3 * HOUR)
    transcript = tx.load(tmp_path / "projects")
    written(transcript.path, 4)
    written(transcript.sidecar_dir / "subagents" / "agent-b1.jsonl", 3 * HOUR)

    activity = scan(transcript)
    assert active_upper_bound(transcript) == pytest.approx(3 * HOUR) == activity.wall
    assert activity.active <= active_upper_bound(transcript)


@pytest.mark.parametrize(("long_first", "long_last"), [(True, False), (False, True), (True, True)])
def test_the_bound_never_undercounts_a_transcript_whose_first_or_last_line_is_long(
    tmp_path: Path, long_first: bool, long_last: bool
) -> None:
    """cc-blackbox reads 256 KB of a transcript's head and 128 KB of its tail, and drops a line longer than that: a
    prompt with a large paste, or a large tool result."""
    tx = Transcript()
    tx.prompt(0, "pasted log line\n" * 25_000 if long_first else "Run the tests.")
    call = tx.request(1).bash(1, "make test")
    tx.result(2 * HOUR, call, "test output line\n" * 25_000 if long_last else "ok")
    transcript = tx.load(tmp_path / "projects")
    written(transcript.path, 2 * HOUR)
    if long_first:  # the start comes from the tail
        assert transcript.started_at is None or transcript.started_at > START
    if long_last:  # the end comes from the head
        assert transcript.last_record_at is None or transcript.last_record_at < START + timedelta(hours=2)

    activity = scan(transcript)
    assert activity.active == 2 * HOUR
    assert active_upper_bound(transcript) >= activity.active


def test_the_scan_skips_a_record_still_being_written(tmp_path: Path) -> None:
    path = session(LONG, active=HOUR).write(tmp_path / "projects")
    with path.open("a") as handle:
        handle.write('{"type":"assistant","timestamp":"2026-11-02T12:00:00.000Z","message":{"role":"assi')
    assert scan(read_session_transcript(path)).active == HOUR


def test_scan_all_keeps_the_order_it_was_given(tmp_path: Path) -> None:
    paths = [session(s, active=a).write(tmp_path / "projects") for s, a in ((SHORT, 60), (LONG, 120), (IDLE, 30))]
    found = list(scan_all([read_session_transcript(p) for p in paths]))
    assert [(a.session_id, a.active) for a in found] == [(SHORT, 60), (LONG, 120), (IDLE, 30)]


def test_plain_search_reads_no_activity(projects: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(search_cmd, "scan_all", lambda _t: pytest.fail("plain search must not read activity"))
    rows = search_json()
    assert [r["session_id"] for r in rows] == [IDLE, LONG, SHORT] and "active" not in rows[0]
    assert "Active" not in runner.invoke(app, ["search"]).output


def test_durations_add_the_columns(projects: Path) -> None:
    rows = search_json("--durations")
    assert {r["session_id"]: (r["active"], r["wall"], r["subagents"], r["compactions"]) for r in rows} == {
        LONG: (3 * HOUR, 3 * HOUR, 0, 0),
        SHORT: (30 * 60, 30 * 60, 0, 0),
        IDLE: (2, 5 * HOUR + 2, 0, 0),
    }
    assert rows[0]["kind"] == "interactive" and rows[0]["kind_source"] == "origins"
    table = runner.invoke(app, ["search", "--durations"]).output
    header = table.splitlines()[0].split()
    assert header[:7] == ["When", "Session", "Wall", "Active", "Subagents", "Compactions", "Kind"]
    assert "3h00m" in table and "interactive" in table


def test_durations_show_the_subagents_compactions_beside_the_main_agents(
    projects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tx = Transcript(session_id=sid("compacted"))
    tx.prompt(10 * HOUR)
    sub = tx.request(10 * HOUR + 1).agent(10 * HOUR + 2, "a1", description="run: tests", background=True)
    tx.agent_returned(10 * HOUR + 3, sub)
    sub.request(10 * HOUR + 4).text(10 * HOUR + 10)
    sub.compaction(10 * HOUR + 20)
    sub.request(10 * HOUR + 21).text(10 * HOUR + 30)
    tx.compaction(10 * HOUR + 40)
    tx.compaction(10 * HOUR + 140)
    tx.request(10 * HOUR + 201).text(10 * HOUR + 205)
    tx.turn_end(10 * HOUR + 206)
    tx.write(projects)
    row = next(r for r in search_json("--durations") if r["session_id"] == sid("compacted"))
    assert (row["compactions"], row["subagent_compactions"]) == (2, 1)
    assert "2 (+1)" in runner.invoke(app, ["search", "--durations"]).output


def test_min_active_keeps_sessions_active_long_enough_and_reads_only_those_that_can_be(
    projects: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    read: list[str] = []

    def spy(transcripts: list[object]) -> object:
        read.extend(t.session_id for t in transcripts)  # type: ignore[attr-defined]
        return scan_all(transcripts)  # type: ignore[arg-type]

    monkeypatch.setattr(search_cmd, "scan_all", spy)
    rows = search_json("--min-active", "2h")
    assert [r["session_id"] for r in rows] == [LONG]
    assert sorted(read) == sorted([LONG, IDLE])  # SHORT spans 30 min and cannot qualify


def test_sort_active_orders_by_active_time(projects: Path) -> None:
    assert [r["session_id"] for r in search_json("--sort", "active")] == [LONG, SHORT, IDLE]
    assert [r["session_id"] for r in search_json("--sort", "active", "-n", "1")] == [LONG]


def test_nothing_active_enough_says_so(projects: Path) -> None:
    result = runner.invoke(app, ["search", "--min-active", "4h"])
    assert result.exit_code == 0 and "No sessions found for /home/user/projects/example active at least 4h" in (
        result.stderr
    )


@pytest.mark.parametrize(
    ("args", "message"),
    [(["--min-active", "a while"], "--min-active"), (["--sort", "name"], "--sort: use one of last, active")],
)
def test_options_that_cannot_be_read_fail(projects: Path, args: list[str], message: str) -> None:
    result = runner.invoke(app, ["search", *args])
    assert result.exit_code == 1 and message in result.output
