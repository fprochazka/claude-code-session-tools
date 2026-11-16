from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from conftest import Machine, fake_tmux
from typer.testing import CliRunner

from cc_session_tools.active import Filters, resolve_profile_globs
from cc_session_tools.cli import app
from cc_session_tools.watch_events import (
    FilterError,
    WatchEvent,
    format_duration,
    parse_duration,
    resolve_filter,
)
from cc_session_tools.watcher import Ends, Selection, SessionTracker, Thresholds, Watcher

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="liveness and fake processes need Linux")

runner = CliRunner()

NOW = 1794200000.0


class ListSink:
    """An event sink that keeps the events, in the shape of cc-blackbox's sinks."""

    name = "list"

    def __init__(self) -> None:
        self.events: list[WatchEvent] = []
        self.closed = False

    def describe(self) -> str:
        return "list"

    def emit(self, events: Sequence[WatchEvent]) -> None:
        self.events.extend(events)

    def seconds_until_flush(self) -> float | None:
        return None

    def tick(self) -> None:
        pass

    def alive(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]


class Clock:
    """Epoch seconds that move only when the watcher waits. ``on_wait`` runs before each wait returns."""

    def __init__(self, now: float = NOW) -> None:
        self.now = now
        self.waits = 0
        self.on_wait: Callable[[int], None] = lambda n: None

    def __call__(self) -> float:
        return self.now

    def wait(self, seconds: float) -> None:
        self.waits += 1
        self.now += max(seconds, 0.001)
        self.on_wait(self.waits)


def tracker(selection: Selection | None = None, thresholds: Thresholds | None = None) -> SessionTracker:
    t = SessionTracker(selection or Selection(), thresholds or Thresholds(), tmux_interval=0)
    t.start(NOW)
    return t


def kinds(events: list[WatchEvent]) -> list[str]:
    return [e.kind for e in events]


def by_kind(events: list[WatchEvent], kind: str) -> WatchEvent:
    return next(e for e in events if e.kind == kind)


@pytest.fixture(autouse=True)
def no_tmux(fake_bin: Path) -> Path:
    fake_tmux(fake_bin, [])
    return fake_bin


# --- joining and leaving ---


def test_a_new_session_of_a_matching_profile_joins_the_watch(machine: Machine) -> None:
    machine.session("work", "first")
    t = tracker(Selection(Filters(profiles=frozenset(resolve_profile_globs(["w*"])))))
    assert [m.session.name for m in t.matched()] == ["first"]
    assert t.poll(NOW + 1) == []

    second = machine.session("work", "second")
    machine.session("lab", "elsewhere")
    events = t.poll(NOW + 2)

    assert [e.data for e in events] == [{"session": "work/second", "status": "idle"}]
    assert second in machine.processes


def test_a_session_without_a_name_or_with_a_shared_one_is_named_by_pid(machine: Machine) -> None:
    unnamed = machine.session("work", None)
    first = machine.session("work", "twin")
    second = machine.session("work", "Twin")
    machine.session("lab", "twin")
    t = tracker()
    assert sorted(t.summary(m)["session"] for m in t.matched()) == sorted(
        ["lab/twin", f"work/pid:{unnamed}", f"work/pid:{first}", f"work/pid:{second}"]
    )
    # The pid form resolves to exactly that session.
    result = runner.invoke(app, ["inspect", f"work/pid:{first}", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["pid"] == first

    machine.update(second, name="other")
    events = t.poll(NOW + 1)
    assert [e.data for e in events] == [{"session": "work/other", "from": f"work/pid:{second}"}]
    assert t.identify(t.tracked[first]) == "work/twin"


def test_a_rename_into_the_glob_joins_and_a_rename_out_leaves(machine: Machine) -> None:
    pid = machine.session("work", "coordinator")
    t = tracker(Selection(Filters(name_glob="w?-*")))
    assert t.matched() == []

    machine.update(pid, name="w1-order-planner")
    events = t.poll(NOW + 1)
    # A session that starts to match is reported once, with its new state; the rename that caused it is not.
    assert kinds(events) == ["session_matched"]
    assert events[0].data == {"session": "work/w1-order-planner", "status": "idle"}

    machine.update(pid, name="w1-renamed")
    events = t.poll(NOW + 2)
    assert kinds(events) == ["renamed"]
    assert events[0].data == {"session": "work/w1-renamed", "from": "work/w1-order-planner"}

    machine.update(pid, name="done")
    events = t.poll(NOW + 3)
    # A change is reported for a session that matched before it, so the reader learns why the session left.
    assert kinds(events) == ["renamed", "session_unmatched"]
    assert events[1].data == {"session": "work/done"}


def test_a_status_filter_lets_sessions_join_and_leave(machine: Machine) -> None:
    pid = machine.session("work", "worker", status="idle")
    t = tracker(Selection(Filters(statuses=frozenset({"busy"}))))
    machine.update(pid, status="busy")
    assert kinds(t.poll(NOW + 1)) == ["session_matched"]
    machine.update(pid, status="idle")
    assert kinds(t.poll(NOW + 2)) == ["status_changed", "turn_finished", "session_unmatched"]


def test_the_own_session_never_matches(machine: Machine) -> None:
    own = machine.session("work", "me")
    machine.session("work", "other")
    t = tracker(Selection(exclude_pid=own))
    assert [m.session.name for m in t.matched()] == ["other"]
    machine.update(own, status="busy")
    assert t.poll(NOW + 1) == []


def event_pid(machine: Machine, name: str) -> int:
    return next(pid for pid, path in machine.records.items() if json.loads(path.read_text()).get("name") == name)


# --- changes ---


def test_status_transitions_with_derived_events(machine: Machine) -> None:
    pid = machine.session("work", "worker", status="busy")
    t = tracker()

    machine.update(pid, status="idle")
    events = t.poll(NOW + 1)
    assert kinds(events) == ["status_changed", "turn_finished"]
    assert events[0].data == {"session": "work/worker", "from": "busy", "to": "idle"}
    assert events[1].data == {"session": "work/worker", "from": "busy"}

    machine.update(pid, status="waiting", waitingFor="permission prompt")
    events = t.poll(NOW + 2)
    assert kinds(events) == ["status_changed", "waiting"]
    assert events[0].data == {
        "session": "work/worker",
        "from": "idle",
        "to": "waiting",
        "waiting_for": "permission prompt",
    }
    assert events[1].data == {"session": "work/worker", "waiting_for": "permission prompt"}

    # Answering the prompt and finishing the turn without a busy record in between still ends the turn.
    machine.update(pid, status="idle", waitingFor=None)
    assert kinds(t.poll(NOW + 3)) == ["status_changed", "turn_finished"]

    # A shell command is no turn.
    machine.update(pid, status="shell")
    machine_events = t.poll(NOW + 4)
    machine.update(pid, status="idle")
    assert kinds(machine_events + t.poll(NOW + 5)) == ["status_changed", "status_changed"]


def test_an_unchanged_record_reports_nothing(machine: Machine) -> None:
    pid = machine.session("work", "worker")
    t = tracker()
    machine.update(pid, updatedAt=1794200000500)
    assert t.poll(NOW + 1) == []


def test_cwd_changed(machine: Machine) -> None:
    pid = machine.session("work", "worker")
    t = tracker()
    machine.update(pid, cwd=str(machine.home / "repo" / ".worktrees" / "fp-x"))
    [event] = t.poll(NOW + 1)
    assert event.kind == "cwd_changed"
    assert event.data == {"session": "work/worker", "to": str(machine.home / "repo" / ".worktrees" / "fp-x")}


def test_tmux_moved_follows_the_pane(machine: Machine, no_tmux: Path) -> None:
    fake_tmux(no_tmux, ["%14\tbefore\t3\t@14\ttitle"])
    machine.session("work", "worker", tmux="before:@14.%14")
    t = tracker()
    assert t.matched()[0].tmux_live is not None
    assert t.poll(NOW + 1) == []

    fake_tmux(no_tmux, ["%14\tafter\t5\t@14\ttitle"])
    [event] = t.poll(NOW + 2)
    assert event.kind == "tmux_moved"
    assert event.data == {"session": "work/worker", "to": "after:5"}

    # A pane the server no longer has moved nowhere.
    fake_tmux(no_tmux, [])
    [event] = t.poll(NOW + 3)
    assert event.data == {"session": "work/worker", "to": None}

    # An unreachable tmux server says nothing about the pane.
    fake_tmux(no_tmux, ["%14\tafter\t5\t@14\ttitle"])
    t.poll(NOW + 4)
    fake_tmux(no_tmux, None, exit_code=1)
    assert t.poll(NOW + 5) == []


def test_a_new_session_in_a_new_pane_is_placed_at_once(machine: Machine, no_tmux: Path) -> None:
    t = tracker()
    fake_tmux(no_tmux, ["%20\tfresh\t1\t@20\t"])
    machine.session("work", "new", tmux="fresh:@20.%20")
    [event] = t.poll(NOW + 1)
    assert event.kind == "session_started"
    assert t.tracked[event_pid(machine, "new")].tmux_state == "present"


def test_session_ended_by_a_removed_record_or_a_gone_process(machine: Machine) -> None:
    clean = machine.session("work", "clean")
    killed = machine.session("work", "killed")
    t = tracker()

    machine.remove_record(clean)
    machine.kill(killed)
    events = t.poll(NOW + 1)

    assert sorted((e.kind, e.data["session"], e.data["reason"]) for e in events) == [
        ("session_ended", "work/clean", "record_removed"),
        ("session_ended", "work/killed", "process_gone"),
    ]
    assert t.poll(NOW + 2) == []


# --- thresholds ---


def test_busy_long_comes_once_per_busy_stretch(machine: Machine) -> None:
    pid = machine.session("work", "worker", status="busy", statusUpdatedAt=int(NOW * 1000))
    t = tracker(thresholds=Thresholds(busy_after=60))
    assert t.seconds_until_threshold(NOW + 10) == pytest.approx(50)
    assert t.poll(NOW + 30) == []

    [event] = t.poll(NOW + 61)
    assert event.kind == "busy_long"
    assert event.data == {"session": "work/worker", "after": "1m"}
    assert t.poll(NOW + 200) == []

    machine.update(pid, status="idle", statusUpdatedAt=int((NOW + 300) * 1000))
    assert kinds(t.poll(NOW + 300)) == ["status_changed", "turn_finished"]
    machine.update(pid, status="busy", statusUpdatedAt=int((NOW + 310) * 1000))
    assert kinds(t.poll(NOW + 310)) == ["status_changed"]
    assert kinds(t.poll(NOW + 371)) == ["busy_long"]


def test_waiting_long(machine: Machine) -> None:
    machine.session("work", "asking", status="waiting", waitingFor="permission prompt", statusUpdatedAt=int(NOW * 1000))
    t = tracker(thresholds=Thresholds(waiting_after=120))
    assert t.poll(NOW + 100) == []
    [event] = t.poll(NOW + 120)
    assert event.kind == "waiting_long"
    assert event.data == {"session": "work/asking", "after": "2m"}


# --- ends ---


def run_watch(
    t: SessionTracker, ends: Ends, clock: Clock, reported: frozenset[str] | None = None
) -> tuple[ListSink, list[str]]:
    sink = ListSink()
    lines: list[str] = []
    Watcher(
        t,
        sink,
        reported or resolve_filter(None, None, fixed_set=t.selection.pids is not None),
        ends,
        status=lines.append,
        clock=clock,
        wait=clock.wait,
    ).run()
    assert sink.closed
    return sink, lines


def test_until_ends_after_the_first_event_of_the_kind(machine: Machine) -> None:
    pid = machine.session("work", "worker", status="busy")
    clock = Clock()

    def on_wait(n: int) -> None:
        if n == 2:
            machine.update(pid, status="idle")

    clock.on_wait = on_wait
    sink, lines = run_watch(tracker(), Ends(until="turn_finished"), clock)

    assert sink.kinds() == ["watch_started", "status_changed", "turn_finished", "watch_ended"]
    assert [e.seq for e in sink.events] == [1, 2, 3, 4]
    assert sink.events[-1].data == {"reason": "until", "event": "turn_finished", "seq": 3}
    assert sink.events[0].data["sessions"] == [{"session": "work/worker", "status": "busy"}]
    assert lines == ["ended: until"]


def test_events_outside_the_filter_are_not_numbered(machine: Machine) -> None:
    pid = machine.session("work", "worker", status="busy")
    clock = Clock()
    clock.on_wait = lambda n: machine.update(pid, status="idle") if n == 1 else None
    reported = resolve_filter(["turns"], None)
    sink, _ = run_watch(tracker(), Ends(until="turn_finished"), clock, reported)
    assert [(e.kind, e.seq) for e in sink.events] == [("watch_started", 1), ("turn_finished", 2), ("watch_ended", 3)]


def test_timeout(machine: Machine) -> None:
    machine.session("work", "worker")
    clock = Clock()
    sink, _ = run_watch(tracker(), Ends(timeout=5), clock)
    assert sink.kinds() == ["watch_started", "watch_ended"]
    assert sink.events[-1].data == {"reason": "timeout"}
    assert clock.now == pytest.approx(NOW + 5, abs=0.01)


def test_idle_after_counts_from_the_last_reported_event(machine: Machine) -> None:
    pid = machine.session("work", "worker")
    clock = Clock()
    clock.on_wait = lambda n: machine.update(pid, name="renamed") if n == 3 else None
    sink, _ = run_watch(tracker(), Ends(idle_after=10), clock)
    assert sink.kinds() == ["watch_started", "renamed", "watch_ended"]
    assert sink.events[-1].data == {"reason": "idle", "idle_after": "10s"}
    assert clock.now == pytest.approx(NOW + 13, abs=0.01)


def test_a_fixed_set_ends_when_its_sessions_end(machine: Machine) -> None:
    first = machine.session("work", "first")
    second = machine.session("work", "second")
    machine.session("work", "bystander")
    t = tracker()
    t.select(Selection(pids=frozenset({first, second})))
    clock = Clock()

    def on_wait(n: int) -> None:
        if n == 1:
            machine.kill(first)
            machine.session("work", "newcomer")
        if n == 2:
            machine.remove_record(second)

    clock.on_wait = on_wait
    sink, lines = run_watch(t, Ends(), clock)

    assert sink.kinds() == ["watch_started", "session_ended", "session_ended", "watch_ended"]
    assert [s["session"] for s in sink.events[0].data["sessions"]] == ["work/first", "work/second"]
    assert sink.events[-1].data == {"reason": "sessions_ended"}
    assert lines == ["ended: sessions_ended"]


def test_a_signal_ends_the_watch(machine: Machine) -> None:
    machine.session("work", "worker")
    clock = Clock()
    holder: dict[str, Watcher] = {}
    clock.on_wait = lambda n: holder["w"].request_stop("signal SIGTERM")
    sink = ListSink()
    holder["w"] = Watcher(
        tracker(), sink, resolve_filter(None, None), Ends(), status=lambda line: None, clock=clock, wait=clock.wait
    )
    holder["w"].run()
    assert sink.events[-1].data == {"reason": "signal SIGTERM"}


# --- the filter ---


def test_filter_expands_groups_and_always_keeps_the_lifecycle() -> None:
    assert resolve_filter(["turns"], None) == {"turn_finished", "waiting", "watch_started", "watch_ended"}
    everything = resolve_filter(None, None)
    assert "busy_long" not in everything and "session_started" in everything
    assert "status_changed" not in resolve_filter(None, ["status,changes"])
    assert "busy_long" in resolve_filter(["thresholds"], None, enabled_thresholds=["busy_long"])


@pytest.mark.parametrize(
    ("include", "exclude", "kwargs", "message"),
    [
        (["tunrs"], None, {}, "unknown event kind or group 'tunrs'"),
        (["busy_long"], None, {}, "it needs --busy-after"),
        (["thresholds"], None, {}, "the group 'thresholds' has no events"),
        (None, ["lifecycle"], {}, "cannot be excluded"),
        (["session_started"], None, {"fixed_set": True}, "never produces 'session_started'"),
        (["turns"], None, {"enabled_thresholds": ["busy_long"]}, "--busy-after: 'busy_long' is filtered out"),
    ],
)
def test_filter_errors(include: Any, exclude: Any, kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(FilterError, match=message):
        resolve_filter(include, exclude, **kwargs)


def test_durations() -> None:
    assert parse_duration("90s") == 90
    assert parse_duration("1h30m") == 5400
    assert parse_duration("45") == 45
    assert format_duration(5400) == "1h30m"
    with pytest.raises(ValueError):
        parse_duration("soon")


# --- the command ---


def invoke(*args: str) -> Any:
    return runner.invoke(app, ["watch", *args])


def test_command_prints_the_header_on_stderr_and_events_on_stdout(machine: Machine) -> None:
    machine.session("work", "w1-order-planner", status="busy")
    machine.session("lab", "w2-order-planner")
    machine.session("work", "coordinator")

    result = invoke("--profile", "w*", "--name", "w?-order-*", "--timeout=1s", "--events-stream=stdout")

    assert result.exit_code == 0, result.output
    header = result.stderr.splitlines()
    assert all(line.startswith("cc-session-tools watch: ") for line in header)
    assert header[0] == (
        "cc-session-tools watch: selection: profile=w* name=w?-order-* (profiles: work); 1 matching now"
    )
    assert "cc-session-tools watch: stream: stdout (one JSON event per line)" in header
    assert "cc-session-tools watch: ends: timeout 1s" in header
    assert header[-1] == "cc-session-tools watch: ended: timeout"
    changelog = Path(next(line for line in header if "changelog: " in line).split("changelog: ", 1)[1])

    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert [e["kind"] for e in events] == ["watch_started", "watch_ended"]
    assert events[0]["data"]["sessions"] == [{"session": "work/w1-order-planner", "status": "busy"}]
    assert "cc-session-tools watch: details: cc-session-tools inspect <session> --json" in header
    assert events[0]["data"]["selection"] == "profile=w* name=w?-order-*"
    assert set(events[0]) == {"kind", "seq", "observed_at", "data"}
    assert [json.loads(line) for line in changelog.read_text().splitlines()] == events


def test_command_with_sessions_watches_a_fixed_set(machine: Machine) -> None:
    machine.session("work", "target")
    machine.session("work", "other")
    result = invoke("target", "--timeout=1s", "--events-stream=stdout")
    assert result.exit_code == 0, result.output
    assert "selection: sessions=target" in result.stderr
    assert "when every named session has ended" in result.stderr
    started = json.loads(result.stdout.splitlines()[0])
    assert started["data"]["sessions"] == [{"session": "work/target", "status": "idle"}]


def test_command_leaves_out_its_own_session(machine: Machine, monkeypatch: pytest.MonkeyPatch) -> None:
    own = machine.session("work", "me")
    machine.session("work", "other")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(machine.config_dir("work")))
    monkeypatch.setenv("CLAUDE_PID", str(own))

    result = invoke("--timeout=1s", "--events-stream=stdout")
    assert result.exit_code == 0, result.output
    assert f"own session: me (pid {own}) is left out" in result.stderr
    started = json.loads(result.stdout.splitlines()[0])
    assert started["data"]["sessions"] == [{"session": "work/other", "status": "idle"}]
    # Under a session the changelog goes to its scratchpad.
    assert "/scratchpad/cc-session-tools/watch-" in result.stderr

    result = invoke("me", "--timeout=1s", "--events-stream=stdout")
    assert result.exit_code == 1
    assert "--include-self" in result.stderr

    result = invoke("--include-self", "--timeout=1s", "--events-stream=stdout")
    assert result.exit_code == 0, result.output
    assert len(json.loads(result.stdout.splitlines()[0])["data"]["sessions"]) == 2


def test_command_falls_back_to_stdout_when_the_session_takes_no_messages(
    machine: Machine, monkeypatch: pytest.MonkeyPatch
) -> None:
    own = machine.session("work", "me")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(machine.config_dir("work")))
    monkeypatch.setenv("CLAUDE_PID", str(own))

    result = invoke("--timeout=1s")
    assert result.exit_code == 0, result.output
    assert "stream fallback: the session's messaging socket" in result.stderr
    assert json.loads(result.stdout.splitlines()[0])["kind"] == "watch_started"

    result = invoke("--timeout=1s", "--events-stream=claude-message")
    assert result.exit_code == 1
    assert "--events-stream=claude-message is not possible" in result.stderr


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (["--timeout=soon"], 2, "--timeout: cannot read the duration 'soon'"),
        (["--events=nope"], 2, "unknown event kind or group 'nope'"),
        (["--until=watch_started"], 2, "--until: 'watch_started' is not an event kind this watch reports"),
        (["--until=renamed", "--events=turns"], 2, "--until: 'renamed'"),
        (["--profile", "x*"], 1, "No Claude Code profile matches 'x*'"),
        (["missing"], 1, "no active session matches 'missing'"),
    ],
)
def test_command_errors(machine: Machine, args: list[str], code: int, message: str) -> None:
    machine.session("work", "present")
    result = invoke("--timeout=1s", "--events-stream=stdout", *args)
    assert result.exit_code == code, result.output
    assert message in result.stderr
    assert result.stdout == ""
