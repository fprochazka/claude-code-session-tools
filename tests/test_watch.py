from __future__ import annotations

import json
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from cc_blackbox import ActiveSession, project_slug
from conftest import (
    FAKE_TMUX_SOCKET,
    Machine,
    RealTmux,
    compact_boundary,
    fake_tmux,
    log_start,
    response,
    sid,
    write_records,
)
from typer.testing import CliRunner

from cc_session_tools.active import (
    Filters,
    list_tmux_panes,
    read_context_window,
    real_socket,
    resolve_profile_globs,
    resolve_tmux_session,
)
from cc_session_tools.cli import app
from cc_session_tools.watch_events import (
    ContextMark,
    FilterError,
    WatchEvent,
    format_duration,
    parse_context_mark,
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


def tmux_session_filters(name: str) -> Filters:
    """The filters of ``--tmux-session <name>``, resolved on the caller's tmux server."""
    selected = resolve_tmux_session(name, list_tmux_panes())
    return Filters(tmux_session_id=selected.id, tmux_socket=selected.socket)


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


def test_a_permission_prompt_whose_pane_shows_the_plan_dialog_is_a_plan_approval(
    machine: Machine, fake_bin: Path
) -> None:
    fake_tmux(fake_bin, ["%14\tmain\t3\t@14\t", "%15\tmain\t4\t@15\t"])
    planner = machine.session("work", "planner", status="busy", tmux="main:@14.%14")
    asker = machine.session("work", "asker", status="busy", tmux="main:@15.%15")
    outside = machine.session("work", "outside", status="busy")
    screens = {planner: ["unknown", "plan_approval"], asker: ["permission_prompt"]}
    captured: list[int] = []
    slept: list[float] = []

    def read_screen(session: ActiveSession) -> str | None:
        captured.append(session.pid)
        return screens[session.pid].pop(0)

    plan = machine.home / "elsewhere" / ".claude" / "plans" / "a-quiet-otter.md"
    t = SessionTracker(
        Selection(),
        Thresholds(),
        tmux_interval=0,
        read_screen=read_screen,
        find_plan=lambda session: plan,
        sleep=slept.append,
    )
    t.start(NOW)

    for pid in (planner, asker, outside):
        machine.update(pid, status="waiting", waitingFor="permission prompt")
    events = t.poll(NOW + 1)

    waiting = {e.data["session"]: e.data for e in events if e.kind == "waiting"}
    assert waiting == {
        "work/planner": {"session": "work/planner", "waiting_for": "plan approval", "plan_file": str(plan)},
        "work/asker": {"session": "work/asker", "waiting_for": "permission prompt"},
        "work/outside": {"session": "work/outside", "waiting_for": "permission prompt"},
    }
    changes = {e.data["session"]: e.data["waiting_for"] for e in events if e.kind == "status_changed"}
    assert changes["work/planner"] == "plan approval"
    # The screen that read unknown, not yet drawn, was captured once more after a second; outside tmux nothing was.
    assert sorted(captured) == sorted([planner, planner, asker])
    assert slept == [1.0]


def test_watch_started_tells_a_plan_approval_from_a_permission_prompt(machine: Machine, no_tmux: Path) -> None:
    fake_tmux(no_tmux, ["%14\tmain\t3\t@14\t"])
    machine.session("work", "planner", status="waiting", waitingFor="permission prompt", tmux="main:@14.%14")
    t = SessionTracker(Selection(), Thresholds(), tmux_interval=0, read_screen=lambda session: "plan_approval")
    t.start(NOW)
    assert [t.summary(m) for m in t.matched()] == [
        {"session": "work/planner", "status": "waiting", "waiting_for": "plan approval"}
    ]


def test_a_pane_that_is_not_the_sessions_own_is_never_captured(machine: Machine, no_tmux: Path) -> None:
    fake_tmux(no_tmux, ["%14\tmain\t3\t@14\t"], pane_pid=1)
    machine.session("work", "planner", status="waiting", waitingFor="permission prompt", tmux="main:@14.%14")
    captured: list[int] = []

    def read_screen(session: ActiveSession) -> str | None:
        captured.append(session.pid)
        return "plan_approval"

    t = SessionTracker(Selection(), Thresholds(), tmux_interval=0, read_screen=read_screen)
    t.start(NOW)
    assert [t.summary(m) for m in t.matched()] == [
        {"session": "work/planner", "status": "waiting", "waiting_for": "permission prompt"}
    ]
    assert captured == []


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


def test_a_window_moved_into_or_out_of_the_tmux_session_joins_or_leaves(machine: Machine, no_tmux: Path) -> None:
    fake_tmux(no_tmux, ["%14\talpha\t3\t@14\t"])
    machine.session("work", "worker", tmux="alpha:@14.%14")
    t = tracker(Selection(Filters(tmux_session_id="$beta", tmux_socket=real_socket(FAKE_TMUX_SOCKET))))
    assert t.matched() == []

    fake_tmux(no_tmux, ["%14\tbeta\t1\t@14\t"])
    [event] = t.poll(NOW + 1)
    assert (event.kind, event.data) == ("session_matched", {"session": "work/worker", "status": "idle"})

    # A tmux server that does not answer keeps the session where it was.
    fake_tmux(no_tmux, None, exit_code=1)
    assert t.poll(NOW + 2) == []
    assert len(t.matched()) == 1
    fake_tmux(no_tmux, ["%14\tbeta\t1\t@14\t"])
    assert t.poll(NOW + 3) == []

    fake_tmux(no_tmux, ["%14\talpha\t4\t@14\t"])
    assert [(e.kind, e.data) for e in t.poll(NOW + 4)] == [
        ("tmux_moved", {"session": "work/worker", "to": "alpha:4"}),
        ("session_unmatched", {"session": "work/worker"}),
    ]


def test_a_new_session_in_the_tmux_session_joins_at_once(machine: Machine, no_tmux: Path) -> None:
    t = tracker(Selection(Filters(tmux_session_id="$beta", tmux_socket=real_socket(FAKE_TMUX_SOCKET))))
    fake_tmux(no_tmux, ["%20\tbeta\t1\t@20\t", "%21\talpha\t1\t@21\t"])
    machine.session("work", "inside", tmux="beta:@20.%20")
    machine.session("work", "outside", tmux="alpha:@21.%21")
    assert [(e.kind, e.data["session"]) for e in t.poll(NOW + 1)] == [("session_started", "work/inside")]


def test_move_window_on_a_real_tmux_server_joins_the_watch(machine: Machine, real_tmux: RealTmux) -> None:
    real_tmux.new_session("alpha")
    real_tmux.new_session("beta")
    pane = real_tmux.new_window("alpha")
    machine.session_of(real_tmux.pane_pid(pane), "work", "worker", tmux=real_tmux.recorded(pane))
    t = tracker(Selection(tmux_session_filters("beta")))
    assert t.matched() == []

    real_tmux.move_window(pane, "beta")
    assert kinds(t.poll(NOW + 1)) == ["session_matched"]


def test_a_pane_the_session_does_not_run_in_never_joins_the_watch(machine: Machine, real_tmux: RealTmux) -> None:
    real_tmux.new_session("alpha")
    real_tmux.new_session("beta")
    pane = real_tmux.new_window("alpha")
    machine.session("work", "stranger", tmux=real_tmux.recorded(pane))
    t = tracker(Selection(tmux_session_filters("beta")))
    assert t.matched() == []

    real_tmux.move_window(pane, "beta")
    assert t.poll(NOW + 1) == []
    assert t.matched() == []
    assert [(x.tmux_state, x.tmux_live) for x in t.tracked.values()] == [("gone", None)]


def test_a_session_on_another_tmux_server_never_joins_the_watch(
    machine: Machine, real_tmux: RealTmux, other_tmux: RealTmux
) -> None:
    real_tmux.new_session("alpha")
    live = other_tmux.new_session("live")
    machine.session_of(other_tmux.pane_pid(live), "work", "worker", tmux=other_tmux.recorded(live))
    t = tracker(Selection(tmux_session_filters("alpha")))
    assert t.matched() == []
    assert [(x.tmux_state, x.tmux_live and x.tmux_live.session) for x in t.tracked.values()] == [("present", "live")]

    later = other_tmux.new_window("live")
    machine.session_of(other_tmux.pane_pid(later), "work", "later", tmux=other_tmux.recorded(later))
    assert t.poll(NOW + 1) == []
    assert t.matched() == []


def test_a_renamed_tmux_session_keeps_its_sessions(machine: Machine, no_tmux: Path) -> None:
    fake_tmux(no_tmux, ["%14\tbeta\t1\t@14\t$7\t"])
    machine.session("work", "worker", tmux="beta:@14.%14")
    t = tracker(Selection(Filters(tmux_session_id="$7", tmux_socket=real_socket(FAKE_TMUX_SOCKET))))
    assert len(t.matched()) == 1

    fake_tmux(no_tmux, ["%14\tgamma\t1\t@14\t$7\t"])
    assert [(e.kind, e.data) for e in t.poll(NOW + 1)] == [("tmux_moved", {"session": "work/worker", "to": "gamma:1"})]
    assert len(t.matched()) == 1


def test_a_renamed_tmux_session_on_a_real_tmux_server_keeps_its_sessions(machine: Machine, real_tmux: RealTmux) -> None:
    real_tmux.new_session("beta")
    pane = real_tmux.new_window("beta")
    machine.session_of(real_tmux.pane_pid(pane), "work", "worker", tmux=real_tmux.recorded(pane))
    t = tracker(Selection(tmux_session_filters("beta")))
    assert len(t.matched()) == 1

    real_tmux.rename_session("beta", "gamma")
    assert kinds(t.poll(NOW + 1)) == ["tmux_moved"]
    assert len(t.matched()) == 1


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


# --- roles ---


def give_role(machine: Machine, name: str, role: str, pid: int) -> None:
    """Write what the session and the auth-switch start hook write: a transcript, and the start log next to it."""
    transcript(machine, name, NOW - 30)
    log_start(machine.project_dir("work"), sid(name), int(NOW * 1000), pid=pid, profile="work", role=role)


def test_a_role_filter_matches_the_role_of_the_process(machine: Machine) -> None:
    give_role(machine, "w1", "worker", machine.session("work", "w1"))
    give_role(machine, "boss", "dispatcher", machine.session("work", "boss"))
    machine.session("work", "plain")
    t = tracker(Selection(Filters(role_globs=("work*",))))
    assert [t.identify(m) for m in t.matched()] == ["work/w1"]


def test_a_session_waiting_for_its_first_prompt_matches_by_its_start_log(machine: Machine) -> None:
    pid = machine.session("work", "fresh", startedAt=int((NOW - 600) * 1000))
    log_start(machine.project_dir("work"), sid("fresh"), int((NOW - 599) * 1000), pid=pid, role="worker")
    t = tracker(Selection(Filters(role_globs=("worker",))))
    assert [t.identify(m) for m in t.matched()] == ["work/fresh"]


def test_a_young_session_whose_role_is_logged_late_joins(machine: Machine) -> None:
    # Its record is there, but neither its transcript nor its start log is, yet.
    pid = machine.session("work", "fresh", startedAt=int((NOW - 5) * 1000))
    t = tracker(Selection(Filters(role_globs=("worker",))))
    assert t.matched() == []
    assert t.poll(NOW + 1) == []

    give_role(machine, "fresh", "worker", pid)
    [event] = t.poll(NOW + 2)
    assert (event.kind, event.data) == ("session_matched", {"session": "work/fresh", "status": "idle"})
    assert t.poll(NOW + 3) == []


def test_an_older_session_whose_role_appears_joins_at_its_next_record_change(machine: Machine) -> None:
    pid = machine.session("work", "slow", startedAt=int((NOW - 600) * 1000))
    t = tracker(Selection(Filters(role_globs=("worker",))))
    give_role(machine, "slow", "worker", pid)
    # Past the settle time, the role is not looked for on every poll.
    assert t.poll(NOW + 1) == []

    machine.update(pid, status="busy")
    [event] = t.poll(NOW + 2)
    assert (event.kind, event.data) == ("session_matched", {"session": "work/slow", "status": "busy"})


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


def transcript(machine: Machine, name: str, *responses_at: float) -> None:
    """Write the transcript of the session ``name`` of the ``work`` profile, with a model response at each time."""
    write_transcript(machine, name, [response(at) for at in responses_at])


def write_transcript(machine: Machine, name: str, records: list[dict[str, Any]]) -> None:
    project = machine.config_dir("work") / "projects" / project_slug(machine.home / "elsewhere")
    write_records(project / f"{sid(name)}.jsonl", records)


def idle_long(events: list[WatchEvent]) -> list[str]:
    return [e.data["threshold"] for e in events if e.kind == "idle_long"]


def test_idle_long_once_per_threshold_until_the_next_model_response(machine: Machine) -> None:
    pid = machine.session("work", "worker")
    transcript(machine, "worker", NOW - 60)
    t = tracker(thresholds=Thresholds(idle_after=(600, 3000)))
    assert t.poll(NOW + 1) == []
    assert t.seconds_until_threshold(NOW + 1) == pytest.approx(539)

    [event] = t.poll(NOW + 540)
    assert (event.kind, event.data) == ("idle_long", {"session": "work/worker", "threshold": "10m"})
    assert t.poll(NOW + 600) == []
    assert idle_long(t.poll(NOW + 2940)) == ["50m"]
    assert t.poll(NOW + 9000) == []

    # A turn without a model response, such as a local command, does not end the stretch.
    machine.update(pid, status="busy")
    t.poll(NOW + 9001)
    machine.update(pid, status="idle")
    assert idle_long(t.poll(NOW + 9002)) == []

    # The next response does: the thresholds count again from it.
    machine.update(pid, status="busy")
    t.poll(NOW + 9010)
    transcript(machine, "worker", NOW - 60, NOW + 9020)
    machine.update(pid, status="idle")
    assert kinds(t.poll(NOW + 9021)) == ["status_changed", "turn_finished"]
    assert idle_long(t.poll(NOW + 9619)) == []
    assert idle_long(t.poll(NOW + 9620)) == ["10m"]


def test_a_resumed_session_is_idle_since_its_process_started(machine: Machine) -> None:
    # The transcript's newest response is from the run before the resume.
    machine.session("work", "worker", startedAt=int((NOW - 60) * 1000))
    transcript(machine, "worker", NOW - 7200)
    t = tracker(thresholds=Thresholds(idle_after=(600,)))
    assert t.poll(NOW + 1) == []
    assert t.poll(NOW + 539) == []
    assert idle_long(t.poll(NOW + 540)) == ["10m"]


def test_a_new_session_id_drops_the_old_transcript_and_starts_a_new_stretch(machine: Machine) -> None:
    pid = machine.session("work", "worker")
    transcript(machine, "worker", NOW - 60)
    t = tracker(thresholds=Thresholds(idle_after=(600,)))
    assert t.poll(NOW + 1) == []

    # /clear: the same process writes a new session id into its record; it has no transcript until its first prompt.
    machine.update(pid, sessionId=sid("cleared"), updatedAt=int((NOW + 300) * 1000))
    assert t.poll(NOW + 300) == []
    assert t.tracked[pid].session.transcript_path is None
    assert t.poll(NOW + 540) == []
    assert idle_long(t.poll(NOW + 900)) == ["10m"]

    # Its first turn writes the new transcript, and its response starts the next stretch.
    machine.update(pid, status="busy")
    t.poll(NOW + 990)
    transcript(machine, "cleared", NOW + 1000)
    machine.update(pid, status="idle")
    t.poll(NOW + 1001)
    assert t.poll(NOW + 1599) == []
    assert idle_long(t.poll(NOW + 1600)) == ["10m"]


def test_a_clear_before_the_watch_starts_counts_from_its_logged_start(machine: Machine) -> None:
    # The process started two hours ago and was cleared a minute ago; the new session id has no transcript yet.
    pid = machine.session("work", "worker", sessionId=sid("cleared"), startedAt=int((NOW - 7200) * 1000))
    transcript(machine, "worker", NOW - 3600)
    log_start(machine.project_dir("work"), sid("cleared"), int((NOW - 60) * 1000), pid=pid, source="clear")
    t = tracker(thresholds=Thresholds(idle_after=(600,)))
    assert t.poll(NOW + 1) == []
    assert t.poll(NOW + 539) == []
    assert idle_long(t.poll(NOW + 540)) == ["10m"]


def test_a_response_without_a_status_change_starts_a_new_stretch(machine: Machine) -> None:
    machine.session("work", "worker")
    transcript(machine, "worker", NOW)
    t = tracker(thresholds=Thresholds(idle_after=(600,)))
    assert t.poll(NOW + 1) == []
    # A background subagent answers, or a turn passes between two polls, while the record stays idle.
    transcript(machine, "worker", NOW, NOW + 300)
    assert t.poll(NOW + 600) == []
    assert t.seconds_until_threshold(NOW + 600) == pytest.approx(300)
    assert idle_long(t.poll(NOW + 900)) == ["10m"]


def test_idle_every_repeats_within_a_stretch_and_reports_only_the_longest_threshold_reached(
    machine: Machine,
) -> None:
    machine.session("work", "worker")
    transcript(machine, "worker", NOW)
    t = tracker(thresholds=Thresholds(idle_after=(600,), idle_every=1800))
    assert idle_long(t.poll(NOW + 600)) == ["10m"]
    assert idle_long(t.poll(NOW + 1800)) == ["30m"]
    assert t.seconds_until_threshold(NOW + 1800) == pytest.approx(1800)
    assert idle_long(t.poll(NOW + 3600)) == ["1h"]
    # A watch that wakes late reports the longest threshold passed, once.
    assert idle_long(t.poll(NOW + 9100)) == ["2h30m"]
    assert idle_long(t.poll(NOW + 9200)) == []


def test_idle_long_for_a_session_already_idle_at_the_start(machine: Machine) -> None:
    machine.session("work", "worker")
    machine.session("work", "busy-one", status="busy")
    transcript(machine, "worker", NOW - 7200)
    transcript(machine, "busy-one", NOW - 7200)
    t = tracker(thresholds=Thresholds(idle_after=(600, 3000)))
    [event] = t.poll(NOW + 1)
    assert (event.data["session"], event.data["threshold"]) == ("work/worker", "50m")


# --- context thresholds ---


def marks(*values: str) -> Thresholds:
    return Thresholds(context_above=tuple(parse_context_mark(v) for v in values))


def context_high(events: list[WatchEvent]) -> list[tuple[str, str]]:
    return [(e.data["session"], e.data["mark"]) for e in events if e.kind == "context_high"]


def test_context_high_once_per_mark_as_the_fill_grows(machine: Machine) -> None:
    machine.session("work", "worker", status="busy")
    records = [response(NOW - 60, used=600_000)]
    write_transcript(machine, "worker", records)
    t = tracker(thresholds=marks("75%", "90%"))
    assert t.poll(NOW + 1) == []

    records.append(response(NOW + 5, used=760_000))
    write_transcript(machine, "worker", records)
    [event] = t.poll(NOW + 10)
    assert (event.kind, event.data) == (
        "context_high",
        {
            "session": "work/worker",
            "used": 760_000,
            "size": 1_000_000,
            "percent": 76,
            "mark": "75%",
            "mark_tokens": 750_000,
            "model": "claude-opus-5-5",
        },
    )
    records.append(response(NOW + 15, used=800_000))
    write_transcript(machine, "worker", records)
    assert t.poll(NOW + 20) == []

    records.append(response(NOW + 25, used=900_000))
    write_transcript(machine, "worker", records)
    assert context_high(t.poll(NOW + 30)) == [("work/worker", "90%")]
    assert t.poll(NOW + 40) == []


def test_a_session_past_several_marks_at_the_start_gets_one_event_for_the_highest(machine: Machine) -> None:
    machine.session("work", "worker")
    machine.session("work", "small")
    write_transcript(machine, "worker", [response(NOW - 60, used=950_000)])
    write_transcript(machine, "small", [response(NOW - 60, used=10_000)])
    t = tracker(thresholds=marks("75%", "90%"))
    assert context_high(t.poll(NOW + 1)) == [("work/worker", "90%")]
    assert t.poll(NOW + 10) == []


def test_a_mark_is_reported_again_after_a_compaction_or_a_drop_below_it(machine: Machine) -> None:
    machine.session("work", "worker", status="busy")
    records = [response(NOW - 60, used=760_000)]
    write_transcript(machine, "worker", records)
    t = tracker(thresholds=marks("75%"))
    assert context_high(t.poll(NOW + 1)) == [("work/worker", "75%")]

    # After a compaction the fill is unknown until the next response, and the mark is armed again.
    records.append(compact_boundary(NOW + 5))
    write_transcript(machine, "worker", records)
    assert t.poll(NOW + 10) == []
    records.append(response(NOW + 15, used=200_000))
    write_transcript(machine, "worker", records)
    assert t.poll(NOW + 20) == []
    records.append(response(NOW + 25, used=780_000))
    write_transcript(machine, "worker", records)
    assert context_high(t.poll(NOW + 30)) == [("work/worker", "75%")]

    # A fill that drops below the mark without a compaction arms it too.
    records.append(response(NOW + 35, used=700_000))
    write_transcript(machine, "worker", records)
    assert t.poll(NOW + 40) == []
    records.append(response(NOW + 45, used=751_000))
    write_transcript(machine, "worker", records)
    assert context_high(t.poll(NOW + 50)) == [("work/worker", "75%")]


def test_a_clear_arms_the_marks_again(machine: Machine) -> None:
    pid = machine.session("work", "worker")
    write_transcript(machine, "worker", [response(NOW - 60, used=760_000)])
    t = tracker(thresholds=marks("75%"))
    assert context_high(t.poll(NOW + 1)) == [("work/worker", "75%")]

    # /clear: a new session id without a transcript, then a first turn that fills the context again.
    machine.update(pid, sessionId=sid("cleared"))
    assert t.poll(NOW + 10) == []
    write_transcript(machine, "cleared", [response(NOW + 15, used=10_000)])
    assert t.poll(NOW + 20) == []
    write_transcript(machine, "cleared", [response(NOW + 15, used=10_000), response(NOW + 25, used=800_000)])
    assert context_high(t.poll(NOW + 30)) == [("work/worker", "75%")]


def test_token_marks_on_200k_and_1m_windows(machine: Machine) -> None:
    machine.session("work", "small-window")
    machine.session("work", "large-window")
    write_transcript(machine, "small-window", [response(NOW - 60, model="claude-sonnet-4-5", used=160_000)])
    write_transcript(machine, "large-window", [response(NOW - 60, used=160_000)])
    t = tracker(thresholds=marks("150k", "75%"))
    events = {e.data["session"]: e.data for e in t.poll(NOW + 1)}

    # On a 200k window 75% is 150k tokens: one event, for the mark listed first.
    assert events["work/small-window"] == {
        "session": "work/small-window",
        "used": 160_000,
        "size": 200_000,
        "percent": 80,
        "mark": "150k",
        "mark_tokens": 150_000,
        "model": "claude-sonnet-4-5",
    }
    assert (events["work/large-window"]["mark"], events["work/large-window"]["mark_tokens"]) == ("150k", 150_000)
    assert events["work/large-window"]["percent"] == 16


def test_the_context_is_read_only_when_the_transcript_changed(machine: Machine) -> None:
    machine.session("work", "worker", status="busy")
    records = [response(NOW - 60, used=10_000)]
    write_transcript(machine, "worker", records)
    reads: list[float] = []
    now = NOW

    def read_context(session: Any, path: Path) -> Any:
        reads.append(now)
        return read_context_window(session, path)

    t = SessionTracker(Selection(), marks("75%"), tmux_interval=0, read_context=read_context)
    t.start(NOW)
    for second in range(1, 8):
        now = NOW + second
        t.poll(now)
    assert reads == [NOW + 1]

    # The transcript is looked at every CONTEXT_INTERVAL seconds, at NOW+4 and NOW+7 above, and NOW+10 and NOW+13
    # here, also while it grows on every poll.
    for second in range(8, 15):
        now = NOW + second
        write_transcript(machine, "worker", [*records, response(now, used=20_000)])
        t.poll(now)
    assert reads == [NOW + 1, NOW + 10, NOW + 13]


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


def test_stop_after_quiet_counts_from_the_last_reported_event(machine: Machine) -> None:
    pid = machine.session("work", "worker")
    clock = Clock()
    clock.on_wait = lambda n: machine.update(pid, name="renamed") if n == 3 else None
    sink, _ = run_watch(tracker(), Ends(stop_after_quiet=10), clock)
    assert sink.kinds() == ["watch_started", "renamed", "watch_ended"]
    assert sink.events[-1].data == {"reason": "quiet", "stop_after_quiet": "10s"}
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
        (["idle_long"], None, {}, "it needs --idle-after or --idle-every"),
        (["context_high"], None, {}, "it needs --context-above"),
        (None, ["lifecycle"], {}, "cannot be excluded"),
        (["session_started"], None, {"fixed_set": True}, "never produces 'session_started'"),
        (["turns"], None, {"enabled_thresholds": ["busy_long"]}, "--busy-after: 'busy_long' is filtered out"),
    ],
)
def test_filter_errors(include: Any, exclude: Any, kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(FilterError, match=message):
        resolve_filter(include, exclude, **kwargs)


@pytest.mark.parametrize(
    ("value", "mark"),
    [
        ("75%", ContextMark("75%", percent=75)),
        ("75", ContextMark("75%", percent=75)),
        ("1%", ContextMark("1%", percent=1)),
        ("99", ContextMark("99%", percent=99)),
        ("100", ContextMark("100", tokens=100)),
        ("150000", ContextMark("150000", tokens=150_000)),
        ("150k", ContextMark("150k", tokens=150_000)),
        ("1M", ContextMark("1M", tokens=1_000_000)),
        ("1.5m", ContextMark("1.5m", tokens=1_500_000)),
    ],
)
def test_context_marks(value: str, mark: ContextMark) -> None:
    assert parse_context_mark(value) == mark


@pytest.mark.parametrize("value", ["0", "0%", "100%", "150%", "-5%", "75.5%", "abc", "0k", "", "10x", "1e5"])
def test_bad_context_marks(value: str) -> None:
    with pytest.raises(ValueError):
        parse_context_mark(value)


def test_a_percentage_mark_rounds_up_to_whole_tokens() -> None:
    assert parse_context_mark("75%").tokens_for(200_000) == 150_000
    assert parse_context_mark("33%").tokens_for(1_000) == 330
    assert parse_context_mark("1%").tokens_for(150) == 2


def test_durations() -> None:
    assert parse_duration("90s") == 90
    assert parse_duration("1h30m") == 5400
    assert parse_duration("45") == 45
    assert parse_duration("3d") == 3 * 86400
    assert parse_duration("2w") == 2 * 604800
    assert parse_duration("1w2d3h4m5s") == 604800 + 2 * 86400 + 3 * 3600 + 4 * 60 + 5
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


def test_command_takes_repeated_idle_thresholds(machine: Machine) -> None:
    machine.session("work", "worker")
    result = invoke(
        "--idle-after=50m", "--idle-after=10m", "--idle-every=30m", "--timeout=1s", "--events-stream=stdout"
    )
    assert result.exit_code == 0, result.output
    assert (
        "cc-session-tools watch: thresholds: idle_long after 10m, 50m, every 30m since the last model response"
        in result.stderr
    )
    assert "idle_long" in result.stderr.split("events: ", 1)[1].splitlines()[0]


def test_command_role_filter(machine: Machine) -> None:
    give_role(machine, "w1", "worker-orders", machine.session("work", "w1"))
    machine.session("work", "plain")
    result = invoke("--role", "worker-*", "--role", "reviewer", "--timeout=1s", "--events-stream=stdout")
    assert result.exit_code == 0, result.output
    assert "cc-session-tools watch: selection: role=worker-*,reviewer; 1 matching now" in result.stderr
    started = json.loads(result.stdout.splitlines()[0])
    assert started["data"]["sessions"] == [{"session": "work/w1", "status": "idle"}]


def test_command_role_filter_with_sessions(machine: Machine) -> None:
    give_role(machine, "w1", "worker-orders", machine.session("work", "w1"))
    machine.session("work", "plain")
    result = invoke("w1", "--role", "worker-*", "--timeout=1s", "--events-stream=stdout")
    assert result.exit_code == 0, result.output
    started = json.loads(result.stdout.splitlines()[0])
    assert started["data"]["sessions"] == [{"session": "work/w1", "status": "idle"}]

    result = invoke("plain", "--role", "worker-*", "--timeout=1s", "--events-stream=stdout")
    assert result.exit_code == 1
    assert "no active session matches 'plain'" in result.stderr


def test_command_tmux_session_current(machine: Machine, no_tmux: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_tmux(no_tmux, ["%1\tcoord\t0\t@1\t", "%2\tcoord\t1\t@2\t", "%3\telse\t0\t@3\t"])
    machine.session("work", "neighbour", tmux="coord:@2.%2")
    machine.session("work", "stranger", tmux="else:@3.%3")
    monkeypatch.setenv("TMUX_PANE", "%1")

    result = invoke("--tmux-session=current", "--timeout=1s", "--events-stream=stdout")

    assert result.exit_code == 0, result.output
    assert (
        "cc-session-tools watch: selection: tmux-session=coord (tmux-session current resolved at the start); "
        "1 matching now" in result.stderr
    )
    started = json.loads(result.stdout.splitlines()[0])
    assert started["data"]["sessions"] == [{"session": "work/neighbour", "status": "idle"}]


@pytest.mark.parametrize(
    ("args", "code", "message"),
    [
        (["--timeout=soon"], 2, "--timeout: cannot read the duration 'soon'"),
        (["--idle-every=0"], 2, "--idle-every: the duration must be longer than zero"),
        (["--idle-after=10m", "--events=turns"], 2, "--idle-after or --idle-every: 'idle_long' is filtered out"),
        (["--events=nope"], 2, "unknown event kind or group 'nope'"),
        (["--until=watch_started"], 2, "--until: 'watch_started' is not an event kind this watch reports"),
        (["--until=renamed", "--events=turns"], 2, "--until: 'renamed'"),
        (["--profile", "x*"], 1, "No Claude Code profile matches 'x*'"),
        (["missing"], 1, "no active session matches 'missing'"),
        (["--tmux-session=nope"], 1, "no tmux session is named 'nope'"),
        (["--tmux-session=current", "--no-tmux"], 2, "--tmux-session and --no-tmux exclude each other"),
    ],
)
def test_command_errors(machine: Machine, args: list[str], code: int, message: str) -> None:
    machine.session("work", "present")
    result = invoke("--timeout=1s", "--events-stream=stdout", *args)
    assert result.exit_code == code, result.output
    assert message in result.stderr
    assert result.stdout == ""
