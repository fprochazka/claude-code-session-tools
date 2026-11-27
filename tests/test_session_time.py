from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from transcripts import START, Transcript, permission, usage

from cc_session_tools.session_activity import scan
from cc_session_tools.session_time import (
    Interval,
    SessionTime,
    TimedCall,
    analyze,
    bursts,
    role_of,
    session_kind,
    union,
)

HOUR = 3600


def run(tx: Transcript, tmp_path: Path) -> SessionTime:
    """Analyse the transcript, and check that the cheap scan of ``search --min-active`` reads the same activity."""
    transcript = tx.load(tmp_path / "projects")
    st = analyze(transcript)
    activity = scan(transcript)
    assert (activity.active, activity.wall) == (st.totals.active, st.totals.wall)
    assert (activity.kind, activity.kind_source) == (st.kind, st.kind_source)
    assert activity.subagents == len(st.subagents)
    assert activity.compactions == sum(1 for c in st.compactions if c.lane == "main")
    return st


def call(st: SessionTime, call_id: str) -> TimedCall:
    for c in [*st.main.calls, *(c for s in st.subagents for c in s.lane.calls)]:
        if c.id == call_id:
            return c
    raise AssertionError(call_id)


def at(t: float) -> Interval:
    return Interval(START + timedelta(seconds=t), START + timedelta(seconds=t))


# 1. Call wall time: serial calls of one response chain, concurrency-safe calls overlap.


def test_a_serial_call_starts_when_the_previous_one_of_its_response_ended(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    r = tx.request(1)
    first = r.bash(2, "./gradlew build")
    second = r.bash(3, "sleep 10")
    tx.result(30, first)
    tx.result(40, second)
    st = run(tx, tmp_path)
    assert (call(st, first).wall, call(st, second).wall) == (28, 10)
    assert call(st, second).start == call(st, first).end


def test_parallel_agent_calls_of_one_response_each_keep_their_own_wall(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    r = tx.request(1)
    one = r.agent(10, "a1", description="impl: S1")
    two = r.agent(11, "a2", description="impl: S2")
    one.request(12).text(500)
    two.request(12).text(600)
    tx.agent_returned(600, one)
    tx.agent_returned(660, two)
    tx.turn_end(661)
    st = run(tx, tmp_path)
    assert (call(st, one.call_id).wall, call(st, two.call_id).wall) == (590, 649)
    assert st.totals.foreground_subagents == 650  # the union, not 590 + 649


def test_a_read_that_ends_before_a_bash_of_its_response_does_not_delay_it(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    r = tx.request(1)
    bash = r.bash(2, "pytest")
    read = r.call(3, "Read", file_path="/home/user/projects/example/a.py")
    tx.result(4, read)
    tx.result(32, bash)
    st = run(tx, tmp_path)
    assert (call(st, bash).wall, call(st, read).wall) == (30, 1)
    assert st.totals.tools == 30


def test_a_serial_call_never_starts_after_its_own_result(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    r = tx.request(1)
    first = r.bash(2, "make")
    second = r.call(3, "Edit", file_path="a.py")
    tx.result(20, second)
    tx.result(30, first)
    st = run(tx, tmp_path)
    assert call(st, second).start == call(st, second).end
    assert call(st, second).wall == 0


def test_calls_of_different_responses_do_not_chain(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    first = tx.request(1).bash(2, "make")
    second = tx.request(3).bash(4, "make test")
    tx.result(30, first)
    tx.result(34, second)
    assert call(run(tx, tmp_path), second).wall == 30


# 2. Results held back behind a foreground Agent call.


def test_a_result_that_came_with_a_long_foreground_agent_is_clamped_and_counted(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    r = tx.request(1)
    sub = r.agent(2, "a1")
    diff = r.bash(3, "git diff")
    sub.request(4).text(500)
    tx.agent_returned(600, sub)
    tx.result(601, diff)
    st = run(tx, tmp_path)
    assert call(st, diff).batched and call(st, diff).wall == 0
    assert not call(st, sub.call_id).batched and call(st, sub.call_id).wall == 598
    assert st.totals.batched_calls == 1


def test_a_quick_call_next_to_a_background_agent_is_not_batched(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    r = tx.request(1)
    sub = r.agent(2, "b1", background=True)
    diff = r.bash(3, "git diff")
    tx.agent_returned(2.1, sub)
    tx.result(5, diff)
    sub.request(4).text(900)
    st = run(tx, tmp_path)
    assert not call(st, diff).batched and call(st, diff).wall == 2


# 3. Parallel and background work is a union per lane.


def test_overlapping_work_is_counted_once_and_walls_stay_elapsed(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    r = tx.request(1)
    one = r.agent(2, "b1", background=True)
    two = r.agent(3, "b2", background=True)
    tx.agent_returned(2.5, one)
    tx.agent_returned(3.5, two)
    tx.turn_end(4)
    one.request(5).text(200)
    two.request(100).text(300)
    tx.handback(310, one)
    tx.request(311).text(312)
    tx.turn_end(313)
    st = run(tx, tmp_path)
    assert sum(s.active for s in st.subagents) == (200 - 2) + (300 - 3)
    assert st.totals.active == 303  # main turns 0..4 and 310..313 ∪ subagent bursts 2..300
    assert st.totals.idle_background == 296  # the gap 4..310, covered by the subagents up to 300


def test_main_busy_and_idle_add_up_to_the_wall(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(10)
    tx.turn_end(11)
    tx.prompt(100)
    tx.request(101).text(150)
    tx.turn_end(151)
    st = run(tx, tmp_path)
    t = st.totals
    assert (t.wall, t.main_busy, t.main_idle) == (151, 62, 89)
    assert t.main_busy + sum(g.seconds for g in st.gaps) == t.wall


# 4. Turns and what opened them.


def test_each_kind_of_prompt_opens_a_turn_with_its_trigger(tmp_path: Path) -> None:
    tx = Transcript()
    times = iter(range(0, 10_000, 100))

    def turn() -> None:
        t = next(times)
        tx.request(t + 1).text(t + 2)
        tx.turn_end(t + 3)

    tx.prompt(0, "Start.")
    r = tx.request(1)
    sub = r.agent(2, "b1", background=True)
    tx.agent_returned(3, sub)
    tx.turn_end(4)
    next(times)
    sub.request(5).text(50)
    tx.prompt(100, "Typed by a human.")
    turn()
    tx.prompt(200, "<cross-session-message>hi</cross-session-message>", origin="peer", sender="other-session")
    turn()
    tx.handback(300, sub)
    turn()
    tx.prompt(400, "[Subagent hand-back] done", origin="peer", sender="b1")
    turn()
    tx.task_notification(500, "bash-1")
    turn()
    tx.scheduled(600, wakeup=True)
    turn()
    tx.scheduled(700)
    turn()
    tx.sdk_prompt(800)
    turn()
    st = run(tx, tmp_path)
    assert [t.trigger for t in st.turns] == [
        "human", "human", "peer", "handback", "handback", "task-notification", "cron", "cron", "sdk",
    ]  # fmt: skip
    assert st.turns[1].prompt == "Typed by a human."


def test_a_local_command_is_not_a_turn(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(2)
    tx.turn_end(3)
    tx.local_command(1000, "/compact")
    tx.compaction(1001, trigger="manual", duration_ms=40_000)
    tx.prompt(2000)
    tx.request(2001).text(2002)
    tx.turn_end(2003)
    st = run(tx, tmp_path)
    assert [(t.trigger, t.seconds) for t in st.turns] == [("human", 3), ("human", 3)]


def test_a_prompt_queued_during_a_long_tool_call_joins_the_turn(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    bash = tx.request(1).bash(2, "./gradlew test")
    tx.queued(300, "Also check the docs.")
    tx.result(900, bash)
    tx.request(901).text(902)
    tx.turn_end(903)
    st = run(tx, tmp_path)
    assert [(t.trigger, t.seconds) for t in st.turns] == [("human", 903)]


def test_a_turn_without_a_turn_duration_record_ends_at_its_last_activity(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(50)
    tx.prompt(500)
    tx.request(501).text(510)
    st = run(tx, tmp_path)
    assert [(t.start, t.end) for t in st.turns] == [
        (at(0).start, at(50).start),
        (at(500).start, at(510).start),
    ]
    assert not st.turns[0].ended_by_record


def test_an_interrupted_turn_is_flagged(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(2)
    tx.prompt(3, "[Request interrupted by user]", origin=None)
    tx.turn_end(4)
    st = run(tx, tmp_path)
    assert st.turns[0].interrupted and st.totals.interruptions == 1


# 5. Idle gaps.


def test_idle_gaps_are_classified_by_what_ended_them_and_by_background_work(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "b1", background=True)
    tx.agent_returned(3, sub)
    tx.turn_end(4)
    sub.request(5).text(200)
    tx.handback(600, sub)
    tx.request(601).text(602)
    tx.turn_end(603)
    tx.prompt(1000)
    tx.request(1001).text(1002)
    tx.turn_end(1003)
    st = run(tx, tmp_path)
    assert [(g.ended_by, g.seconds, g.background_seconds) for g in st.gaps] == [
        ("handback", 596, 196),
        ("human", 397, 0),
    ]
    assert st.totals.idle_by_trigger == {"handback": 596, "human": 397}


# 6. User waits.


@pytest.mark.parametrize("source", ["user_temporary", "user_permanent", "user_reject"])
def test_a_prompt_the_user_answered_is_a_user_wait(tmp_path: Path, source: str) -> None:
    tx = Transcript()
    tx.prompt(0)
    r = tx.request(1)
    asked = r.bash(2, "rm -rf build")
    auto = r.bash(3, "ls")
    tx.result(62, asked, permission=permission(source, grant_scope="once"))
    tx.result(63, auto, permission=permission("config", reason_type="classifier"))
    st = run(tx, tmp_path)
    assert call(st, asked).user_wait and not call(st, auto).user_wait
    assert (st.totals.user_waits, st.totals.tools, st.has_permission_decisions) == (60, 1, True)


def test_dialogs_are_user_waits_and_old_transcripts_recognise_them_by_name(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    r = tx.request(1, logged=False)
    ask = r.call(2, "AskUserQuestion", question="Which one?")
    tx.result(302, ask)
    plan = tx.request(303, logged=False).call(304, "ExitPlanMode", plan="The plan.")
    tx.result(604, plan)
    st = run(tx, tmp_path)
    assert st.totals.user_waits == 600
    assert not st.has_permission_decisions


# 7. Subagents: bursts, rounds, nesting, SubagentHandback.


def test_a_resumed_subagent_has_bursts_rounds_span_and_active_time(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "v1", description="validate: C4", background=True)
    tx.agent_returned(3, sub)
    sub.request(3).text(120)
    tx.turn_end(4)
    tx.request(1000).send_message(1001, sub, "Re-check after the fix.")
    sub.request(1002).text(1100)
    tx.request(3000).send_message(3001, sub, "And once more.")
    sub.request(3002).text(3050)
    st = run(tx, tmp_path)
    [found] = st.subagents
    assert [(b.start, b.end) for b in found.bursts] == [
        (at(2).start, at(120).start),
        (at(1001).start, at(1100).start),
        (at(3001).start, at(3050).start),
    ]
    assert found.span == Interval(at(2).start, at(3050).start)
    assert (found.active, found.rounds) == (118 + 99 + 49, 3)


def test_a_nested_subagent_knows_its_parent_and_the_turn_that_launched_its_ancestor(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    parent = tx.request(1).agent(2, "p1", description="impl: S1")
    nested = parent.request(3).agent(4, "n1", agent_type="test-runner", description="run tests")
    nested.request(5).text(20)
    parent.agent_returned(21, nested)
    parent.request(22).text(23)
    tx.agent_returned(24, parent)
    tx.turn_end(25)
    st = run(tx, tmp_path)
    by_id = {s.agent_id: s for s in st.subagents}
    assert (by_id["n1"].parent_id, by_id["n1"].depth, by_id["n1"].turn) == ("p1", 2, 0)
    assert st.turns[0].subagents == ["p1"]


def test_subagent_handback_is_not_tool_time(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "b1", background=True)
    tx.agent_returned(3, sub)
    r = sub.request(4)
    work = r.bash(5, "pytest")
    sub.result(65, work)
    report = sub.request(66).call(67, "SubagentHandback", message="Done.")
    sub.result(127, report)
    st = run(tx, tmp_path)
    assert st.subagents[0].lane.tool_seconds() == 60


# 8. Model time.


def test_model_time_comes_from_the_api_requests_and_is_unknown_without_them(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    r = tx.request(1)
    r.text(5)
    r.text(9)
    tx.request(20).text(23)
    tx.turn_end(24)
    assert run(tx, tmp_path / "a").totals.model == 8 + 3

    old = Transcript()
    old.prompt(0)
    old.request(1, logged=False).text(5)
    assert run(old, tmp_path / "b").totals.model is None


# 9. Tokens.


def test_tokens_are_counted_once_per_message_with_total_and_new(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    r = tx.request(1, tokens=usage(fresh=10, cache_creation=100, cache_read=1000, output=5))
    r.text(2)
    r.text(3)
    r.call(4, "Read", file_path="a")
    tx.request(10, tokens=usage(fresh=1, cache_creation=0, cache_read=2000, output=7)).text(11)
    st = run(tx, tmp_path)
    assert (st.main.requests, st.main.tokens_total, st.main.tokens_new, st.main.first_context) == (
        2,
        1115 + 2008,
        115 + 8,
        1110,
    )


# 10. Wall, active and away.


def test_the_wall_ignores_bookkeeping_and_a_long_gap_before_a_human_prompt_is_away(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(10)
    tx.turn_end(11)
    tx.prompt(3 * HOUR)
    tx.request(3 * HOUR + 1).text(3 * HOUR + 10)
    tx.turn_end(3 * HOUR + 11)
    tx.system("informational", 13 * 24 * HOUR, content="A notice long after the session.")
    st = run(tx, tmp_path)
    assert st.totals.wall == 3 * HOUR + 11
    assert (st.totals.away, st.totals.active) == (3 * HOUR - 11, 22)
    assert st.totals.idle_by_trigger == {}


def test_a_long_gap_before_a_peer_message_is_waiting_not_away(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(10)
    tx.turn_end(11)
    tx.prompt(3 * HOUR, "Status?", origin="peer", sender="other-session")
    tx.request(3 * HOUR + 1).text(3 * HOUR + 10)
    tx.turn_end(3 * HOUR + 11)
    st = run(tx, tmp_path)
    assert (st.totals.away, st.totals.idle_by_trigger) == (0, {"peer": 3 * HOUR - 11})


# 11. Roles.


@pytest.mark.parametrize(
    ("description", "agent_type", "expected"),
    [
        ("impl: C4", "general-purpose", ("implement", "prefix", "C4")),
        ("validate: C3", "general-purpose", ("validate", "prefix", "C3")),
        ("run: B2", "test-runner", ("run", "prefix", "B2")),
        ("explore: A1", None, ("explore", "prefix", "A1")),
        ("Review the parser change", "general-purpose", ("validate", "guess", None)),
        ("Implement the parser", "general-purpose", ("implement", "guess", None)),
        ("Research the API", "general-purpose", ("explore", "guess", None)),
        ("Babysit the PR", "general-purpose", ("watch", "guess", None)),
        ("Run checks", "general-purpose", ("run", "guess", None)),
        ("Anything", "some-test-runner", ("run", "guess", None)),
        ("Summarize", "general-purpose", ("other", "guess", None)),
    ],
)
def test_the_role_comes_from_the_prefix_or_a_guess(
    description: str, agent_type: str | None, expected: tuple[str, str, str | None]
) -> None:
    assert role_of(description, agent_type) == expected


@pytest.mark.parametrize(
    ("description", "agent_type", "expected"),
    [
        ("Phase 5 implementer: checkpoint fixes", "general-purpose", "implement"),
        ("Phase 4 implementer: backtest, then MR", "general-purpose", "implement"),
        ("Apply the second checkpoint findings", "general-purpose", "implement"),
        ("Phase 3 MR actor: fixes and reviews", "general-purpose", "implement"),
        ("Babysit actor: fix the reviewer's findings", "general-purpose", "implement"),
        ("Babysit watcher for the PR", "general-purpose", "watch"),
        ("Fix the review findings", "general-purpose", "implement"),
        ("Re-check the fix", "general-purpose", "validate"),
        ("Map the plugin's review flow", "Explore", "explore"),
        ("Anything", "pr-watcher", "watch"),
        ("Collect the backtest results", "general-purpose", "other"),
        ("Re-validate step 4.1 rework", "general-purpose", "validate"),
        ("re-run the suite", "general-purpose", "run"),
        ("re-verify the fix", "general-purpose", "validate"),
        ("retest the parser", "general-purpose", "run"),
        ("Check the row versions via tools", "general-purpose", "explore"),
        ("Check the build", "general-purpose", "run"),
        ("Compare review rows of two branches", "general-purpose", "explore"),
        ("Restack !N and !M with the test", "general-purpose", "implement"),
        ("Rebase the branch onto main", "general-purpose", "implement"),
        ("Deep dive: the parser internals", "general-purpose", "explore"),
        ("Probe the cache hit rate", "general-purpose", "explore"),
        ("Mine the logs for errors", "general-purpose", "explore"),
        ("Read the ticket context", "general-purpose", "explore"),
        ("Search the code for usages", "general-purpose", "explore"),
        ("List the open pull requests", "general-purpose", "explore"),
        ("Assess the upgrade risk", "general-purpose", "explore"),
        ("Report MR status", "general-purpose", "other"),
        ("Report the test results", "general-purpose", "run"),
        ("rewrite the parser", "general-purpose", "implement"),
        ("Update search index mapping", "general-purpose", "implement"),
        ("Make the list endpoint paginate", "general-purpose", "implement"),
        ("Address review findings", "general-purpose", "implement"),
        ("Resolve review threads", "general-purpose", "implement"),
        ("Upgrade gradle and rebuild", "general-purpose", "implement"),
        ("Migrate the config, then compare outputs", "general-purpose", "implement"),
    ],
)
def test_the_role_guess_matches_whole_words(description: str, agent_type: str, expected: str) -> None:
    assert role_of(description, agent_type) == (expected, "guess", None)


def test_a_subagent_carries_its_role(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "a1", description="validate: C4")
    tx.agent_returned(3, sub)
    [found] = run(tx, tmp_path).subagents
    assert (found.role, found.role_source, found.step) == ("validate", "prefix", "C4")


# 12. Session kind.


def test_the_kind_comes_from_the_start_log_role(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(2)
    projects = tmp_path / "projects"
    tx.log_start(projects, 0, role="worker")
    st = run(tx, tmp_path)
    assert (st.kind, st.kind_source) == ("worker", "start-log")


@pytest.mark.parametrize(
    ("roles", "triggers", "expected"),
    [
        (["worker", None], {"human": 5}, ("worker", "start-log")),
        ([None, "dispatcher"], {}, ("dispatcher", "start-log")),
        ([], {"human": 1, "cron": 10, "peer": 5}, ("loop", "origins")),
        ([], {"human": 3, "cron": 10, "peer": 5}, ("interactive", "origins")),
        ([], {"human": 0, "sdk": 10}, ("interactive", "origins")),
        ([], {}, ("interactive", "origins")),
    ],
)
def test_the_kind_falls_back_to_what_opened_the_turns(
    roles: list[str | None], triggers: dict[str, int], expected: tuple[str, str]
) -> None:
    assert session_kind(roles, triggers) == expected


# Compactions and segments.


def test_compactions_wait_only_when_they_blocked_and_segments_follow_file_order(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(2)
    tx.turn_end(3)
    tx.compaction(100, trigger="manual", duration_ms=20)  # precomputed
    tx.prompt(101)
    tx.request(102).text(103)
    tx.turn_end(104)
    tx.compaction(200, trigger="auto", duration_ms=45_000)
    # written late: the prompt of the third segment carries a time before the second compaction
    tx.prompt(150)
    tx.request(201).text(202)
    tx.turn_end(203)
    st = run(tx, tmp_path)
    assert [(c.trigger, c.segment, c.wait_seconds) for c in st.compactions] == [("manual", 1, 0), ("auto", 2, 45)]
    assert st.totals.compaction_waits == 45
    assert [t.segment for t in st.turns] == [0, 1, 2]


# Helpers.


def test_union_and_bursts() -> None:
    s = START
    intervals = [Interval(s, s + timedelta(seconds=10)), Interval(s + timedelta(seconds=5), s + timedelta(seconds=20))]
    assert union(intervals) == 20
    moments = [s, s + timedelta(minutes=4), s + timedelta(minutes=10), s + timedelta(minutes=11)]
    assert [b.seconds for b in bursts(moments)] == [240, 60]


# What the validation of C4 pinned.


def test_a_local_command_never_opens_or_extends_a_turn(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(2)
    tx.turn_end(3)
    tx.local_command(1000, "/model")
    tx.task_notification(4600, "bash-1", queued=True)
    tx.request(4601).text(4610)
    tx.turn_end(4611)
    st = run(tx, tmp_path)
    assert [(t.trigger, t.start, t.end) for t in st.turns][1:] == [
        ("task-notification", at(4600).start, at(4611).start)
    ]


def test_activity_long_after_a_local_command_is_an_other_turn(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(2)
    tx.turn_end(3)
    tx.local_command(1000, "/model")
    tx.request(4601).text(4610)
    tx.turn_end(4611)
    st = run(tx, tmp_path)
    assert [(t.trigger, t.start) for t in st.turns][1:] == [("other", at(4610).start)]


def test_a_prompt_soon_after_a_local_command_starts_its_turn_at_the_prompt(tmp_path: Path) -> None:
    tx = Transcript()
    tx.local_command(0, "/model")
    tx.prompt(30, "Go on.")
    tx.request(31).text(40)
    tx.turn_end(41)
    st = run(tx, tmp_path)
    assert [(t.trigger, t.start, t.prompt) for t in st.turns] == [("human", at(30).start, "Go on.")]


def test_an_old_slash_command_that_the_model_answers_is_a_human_turn(tmp_path: Path) -> None:
    tx = Transcript()
    tx.local_command(0, "/review")
    tx.request(5).text(20)
    tx.turn_end(21)
    st = run(tx, tmp_path)
    assert [(t.trigger, t.start, t.seconds) for t in st.turns] == [("human", at(0).start, 21)]


def test_a_subagent_waiting_on_its_own_long_call_is_active(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "b1", background=True)
    tx.agent_returned(3, sub)
    tx.turn_end(4)
    work = sub.request(4).bash(5, "pytest")
    sub.result(605, work)
    tx.handback(700, sub)
    tx.request(701).text(702)
    tx.turn_end(703)
    st = run(tx, tmp_path)
    assert st.subagents[0].active == 603
    assert st.gaps[0].background_seconds == 601  # the gap 4..700, the subagent active until 605


def test_rounds_count_only_the_parents_resumes(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    parent = tx.request(1).agent(2, "p1", background=True)
    tx.agent_returned(3, parent)
    child = parent.request(4).agent(5, "c1", background=True)
    parent.agent_returned(6, child)
    child.request(7).text(20)
    parent.queued(21, "[Subagent hand-back] Done.", mode=None, origin="peer", sender="c1")
    parent.request(22).text(30)
    tx.request(100).send_message(101, parent)
    parent.request(102).text(110)
    by_id = {s.agent_id: s for s in run(tx, tmp_path).subagents}
    assert (by_id["p1"].rounds, by_id["c1"].rounds) == (2, 1)


def test_an_old_subagent_is_resumed_by_a_plain_prompt(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "b1", background=True)
    tx.agent_returned(3, sub)
    sub.request(4).text(10)
    sub.prompt(1000, "[Subagent hand-back] from a child", origin=None)
    sub.prompt(2000, "Re-check the fix.", origin=None)
    sub.request(2001).text(2010)
    [found] = run(tx, tmp_path).subagents
    assert found.rounds == 2


def test_a_slow_background_launch_can_hold_back_a_result(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    r = tx.request(1)
    sub = r.agent(2, "b1", background=True)
    bash = r.bash(3, "make")
    tx.agent_returned(42, sub)
    tx.result(44, bash)
    st = run(tx, tmp_path)
    assert call(st, bash).batched


def test_a_shorter_call_that_ended_with_a_foreground_agent_is_batched(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    r = tx.request(1)
    sub = r.agent(2, "a1")
    bash = r.bash(5, "make")
    tx.agent_returned(33, sub)
    tx.result(33.1, bash)
    assert call(run(tx, tmp_path), bash).batched


def test_the_agent_result_status_says_background_over_the_input(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "b1")  # run_in_background is False in the input
    launched = {"status": "async_launched", "agentId": "b1"}
    tx.result(300, sub.call_id, "Async agent launched.", tool_use_result=launched)
    tx.turn_end(301)
    sub.has_meta = False
    st = run(tx, tmp_path)
    assert call(st, sub.call_id).background
    assert st.totals.foreground_subagents == 0
    assert st.subagents[0].shape == "background"


def test_the_meta_shape_decides_when_the_result_names_no_status(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "b1", background=True)
    sub.background = False  # the meta says foreground, the input says background
    tx.result(300, sub.call_id, "Done.", tool_use_result={"agentId": "b1"})
    tx.turn_end(301)
    st = run(tx, tmp_path)
    assert not call(st, sub.call_id).background
    assert st.totals.foreground_subagents == 298


def test_a_subagent_without_meta_is_linked_through_the_agent_result(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    parent = tx.request(1).agent(2, "p1")
    child = parent.request(3).agent(4, "c1")
    child.request(5).text(20)
    parent.agent_returned(21, child)
    parent.request(22).text(23)
    tx.agent_returned(24, parent)
    tx.turn_end(25)
    parent.has_meta = False
    child.has_meta = False
    by_id = {s.agent_id: s for s in run(tx, tmp_path).subagents}
    assert (by_id["p1"].tool_use_id, by_id["p1"].shape, by_id["p1"].turn) == (parent.call_id, "foreground", 0)
    assert (by_id["c1"].parent_id, by_id["c1"].depth, by_id["c1"].turn) == ("p1", 2, 0)


def test_a_prompt_with_an_origin_and_no_turn_origin_opens_a_turn(tmp_path: Path) -> None:
    tx = Transcript()
    for n, origin in enumerate(["human", "peer", "task-notification"]):
        record = tx.prompt(n * 100, "Next.", origin=origin)
        record.pop("turnOrigin")
        tx.request(n * 100 + 1).text(n * 100 + 2)
        tx.turn_end(n * 100 + 3)
    assert [t.trigger for t in run(tx, tmp_path).turns] == ["human", "peer", "task-notification"]


def test_an_old_prompt_without_origins_is_classified_by_its_text(tmp_path: Path) -> None:
    tx = Transcript()
    texts = [
        "<task-notification><task-id>b1</task-id></task-notification>",
        '<cross-session-message from="other">hi</cross-session-message>',
        "Please continue.",
    ]
    for n, text in enumerate(texts):
        tx.prompt(n * 100, text, origin=None)
        tx.request(n * 100 + 1, logged=False).text(n * 100 + 2)
        tx.turn_end(n * 100 + 3)
    tx.prompt(400, "A reminder only the model sees.", origin=None, isMeta=True)
    assert [t.trigger for t in run(tx, tmp_path).turns] == ["task-notification", "peer", "human"]


def test_activity_no_prompt_opened_is_an_other_turn(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(2)
    tx.turn_end(3)
    tx.request(500).text(510)
    tx.turn_end(511)
    assert [(t.trigger, t.seconds) for t in run(tx, tmp_path).turns] == [("human", 3), ("other", 1)]


def test_a_call_without_a_result_has_no_end_and_no_wall(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    hung = tx.request(1).bash(2, "./gradlew test")
    tx.prompt(10, "[Request interrupted by user for tool use]", origin=None)
    tx.turn_end(11)
    st = run(tx, tmp_path)
    assert (call(st, hung).end, call(st, hung).wall, st.totals.tools) == (None, 0, 0)
    assert st.turns[0].interrupted


def test_an_api_error_is_counted_and_is_not_model_time(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(5)
    error = {
        "id": "msg_err",
        "type": "message",
        "role": "assistant",
        "model": "<synthetic>",
        "content": [{"type": "text", "text": "API Error"}],
        "usage": usage(fresh=0, output=0, cache_read=0, cache_creation=0),
    }
    tx.envelope("assistant", 6, message=error, isApiErrorMessage=True, error="rate_limit", apiErrorStatus=429)
    tx.turn_end(7)
    st = run(tx, tmp_path)
    assert (st.totals.api_errors, st.main.api_errors[0].error, st.totals.model, st.main.requests) == (
        1,
        "rate_limit",
        4,
        1,
    )


def test_busy_gaps_and_the_edges_add_up_to_the_wall(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "b1", background=True)
    tx.agent_returned(3, sub)
    tx.turn_end(4)
    tx.prompt(100)
    tx.request(101).text(102)
    tx.turn_end(103)
    sub.request(5).text(250)  # the subagent works on after the last turn
    st = run(tx, tmp_path)
    t = st.totals
    trailing = (st.wall.end - st.turns[-1].end).total_seconds()
    leading = (st.turns[0].start - st.wall.start).total_seconds()
    assert (t.wall, t.main_busy, leading, trailing) == (250, 7, 0, 147)
    assert t.main_busy + sum(g.seconds for g in st.gaps) + leading + trailing == t.wall == t.main_busy + t.main_idle


def test_overlapping_user_waits_are_counted_once(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    one = tx.request(1).bash(2, "rm -rf a")
    two = tx.request(3).bash(10, "rm -rf b")
    tx.result(62, one, permission=permission("user_temporary"))
    tx.result(70, two, permission=permission("user_temporary"))
    assert run(tx, tmp_path).totals.user_waits == 68


def test_a_turn_opened_by_a_queued_prompt_starts_the_wall(tmp_path: Path) -> None:
    tx = Transcript()
    tx.queued(0, "Start.")
    tx.request(5).text(10)
    tx.turn_end(11)
    t = run(tx, tmp_path).totals
    assert (t.wall, t.main_busy, t.main_idle) == (11, 11, 0)


def test_calls_of_one_old_response_chain_by_its_message_id(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    r = tx.request(1, logged=False)
    first = r.bash(2, "make")
    second = r.bash(3, "make test")
    tx.result(30, first)
    tx.result(40, second)
    st = run(tx, tmp_path)
    assert call(st, second).request_ref is None
    assert call(st, second).wall == 10


def test_a_subagent_compaction_is_kept_with_its_lane_and_does_not_block_the_main_agent(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    sub = tx.request(1).agent(2, "b1", background=True)
    tx.agent_returned(3, sub)
    tx.turn_end(4)
    sub.request(5).text(10)
    sub.compaction(20, duration_ms=30_000)
    st = run(tx, tmp_path)
    assert [(c.lane, c.segment, c.wait_seconds) for c in st.compactions] == [("b1", 1, 30)]
    assert st.totals.compaction_waits == 0


# Turns that went quiet.

DAY = 24 * HOUR


def synthetic(tx: Transcript, t: float, text: str = "No response requested.") -> None:
    """An assistant message Claude Code wrote itself, not the model."""
    message = {
        "id": f"msg_synthetic_{t}",
        "type": "message",
        "role": "assistant",
        "model": "<synthetic>",
        "content": [{"type": "text", "text": text}],
        "usage": usage(fresh=0, output=0, cache_read=0, cache_creation=0),
    }
    tx.envelope("assistant", t, message=message)


def spans(st: SessionTime) -> list[tuple[str, float, float]]:
    return [(t.trigger, (t.start - START).total_seconds(), (t.end - START).total_seconds()) for t in st.turns]


def test_a_command_closes_an_old_turn_that_never_ended(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(50)
    tx.local_command(3 * DAY, "/my-skill")
    tx.request(3 * DAY + 1).text(3 * DAY + 10)
    tx.turn_end(3 * DAY + 11)
    assert spans(run(tx, tmp_path)) == [("human", 0, 50), ("human", 3 * DAY, 3 * DAY + 11)]


def test_a_queued_prompt_long_after_an_unanswered_prompt_opens_its_own_turn(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(2)
    tx.turn_end(3)
    tx.prompt(100)
    tx.task_notification(3 * DAY, "bash-1", queued=True)
    tx.request(3 * DAY + 1).text(3 * DAY + 10)
    tx.turn_end(3 * DAY + 11)
    assert spans(run(tx, tmp_path)) == [("human", 0, 3), ("task-notification", 3 * DAY, 3 * DAY + 11)]


def test_activity_long_after_an_unanswered_prompt_is_an_other_turn(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(2)
    tx.turn_end(3)
    tx.prompt(100)
    tx.request(3 * DAY - 5).text(3 * DAY)
    tx.turn_end(3 * DAY + 11)
    assert spans(run(tx, tmp_path)) == [("human", 0, 3), ("other", 3 * DAY, 3 * DAY + 11)]


def test_a_synthetic_message_is_not_activity(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    tx.request(1).text(50)
    synthetic(tx, 3 * DAY)
    tx.turn_end(3 * DAY + 1)
    assert spans(run(tx, tmp_path)) == [("human", 0, 50)]


def test_a_ten_minute_pause_inside_a_turn_does_not_split_it_but_sixteen_minutes_do(tmp_path: Path) -> None:
    short = Transcript()
    short.prompt(0)
    short.request(1).text(10)
    short.request(11).text(610)  # a response that streamed for 10 minutes before its first record
    short.turn_end(611)
    assert spans(run(short, tmp_path / "short")) == [("human", 0, 611)]

    long = Transcript()
    long.prompt(0)
    long.request(1).text(10)
    long.request(11).text(970)
    long.turn_end(971)
    assert spans(run(long, tmp_path / "long")) == [("human", 0, 10), ("other", 970, 971)]


def test_a_turn_whose_call_runs_for_days_stays_one_turn(tmp_path: Path) -> None:
    tx = Transcript()
    tx.prompt(0)
    work = tx.request(1).bash(2, "./run-the-long-batch.sh")
    tx.queued(DAY, "Still running?")
    tx.result(3 * DAY, work)
    tx.request(3 * DAY + 1).text(3 * DAY + 2)
    tx.turn_end(3 * DAY + 3)
    assert spans(run(tx, tmp_path)) == [("human", 0, 3 * DAY + 3)]
