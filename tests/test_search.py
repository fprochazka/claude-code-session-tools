from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from cc_blackbox import SessionTranscript, list_session_transcripts
from conftest import sid

import cc_session_tools.search as search
from cc_session_tools.search import RipgrepError, build_rg_args, search_session_ids


def test_build_rg_args_exact_vector():
    dirs = [Path("/p/one"), Path("/p/two")]
    assert build_rg_args(["needle"], dirs) == [
        "--files-with-matches",
        "--fixed-strings",
        "--ignore-case",
        "--hidden",
        "--no-ignore",
        "--no-messages",
        "-e",
        "needle",
        "--",
        "/p/one",
        "/p/two",
    ]


def test_build_rg_args_repeats_e_for_every_pattern_in_order():
    args = build_rg_args(["one", "two", "three"], [Path("/p/one")])
    sep = args.index("--")
    assert args[sep - 6 : sep] == ["-e", "one", "-e", "two", "-e", "three"]
    # Directories are the only operands after the separator.
    assert args[sep + 1 :] == ["/p/one"]


def test_build_rg_args_dash_query_is_an_option_value():
    args = build_rg_args(["-foo"], [Path("/p/one")])
    # The dash-prefixed keyword is consumed as the value of its own `-e`, never read as a flag.
    assert args[args.index("-e") + 1] == "-foo"
    assert args[args.index("--") + 1] == "/p/one"


def _fake_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["rg"], returncode=returncode, stdout=stdout, stderr=stderr)


def _patterns(args: list[str]) -> list[str]:
    """The ``-e`` values of a built argv, in order."""
    return [args[i + 1] for i, arg in enumerate(args) if arg == "-e"]


NAMES = ("both", "split", "partial", "none", "sidecar")


def _keyword_fixture(tmp_path: Path) -> list[SessionTranscript]:
    """Sessions exercising session-level matching: split records, sidecars, partial matches."""
    proj = tmp_path / "projects" / "-home-u-proj"
    proj.mkdir(parents=True)

    def write(name: str, *lines: str) -> None:
        (proj / f"{sid(name)}.jsonl").write_text("".join(f'{{"text": "{line}"}}\n' for line in lines))

    # Both keywords in the same record.
    write("both", "worktree and deleted together")
    # Both keywords in the session, but in two DIFFERENT records — no single line holds both.
    write("split", "about a worktree", "it was deleted")
    # Only one of the two keywords.
    write("partial", "just a worktree")
    # Neither keyword — never a match, whatever the mode.
    write("none", "unrelated chatter")
    # One keyword in the transcript, the other only in a subagent transcript of the same session.
    write("sidecar", "the worktree in question")
    (proj / sid("sidecar") / "subagents").mkdir(parents=True)
    (proj / sid("sidecar") / "subagents" / "agent-a1.jsonl").write_text('{"text": "DELETED by the subagent"}\n')
    # A file of another tool next to the transcripts: never a session, even when it matches.
    (proj / f"{sid('none')}-ftoc.jsonl").write_text('{"text": "worktree deleted"}\n')

    return list_session_transcripts(projects_dirs=[tmp_path / "projects"])


def _names(ids: set[str]) -> set[str]:
    return {name for name in NAMES if sid(name) in ids}


@pytest.fixture(params=["rg", "python"])
def backend(request, monkeypatch):
    """Run each content-search test through ripgrep and through the pure-Python fallback."""
    if request.param == "rg":
        if shutil.which("rg") is None:
            pytest.skip("ripgrep (rg) not installed")
    else:
        monkeypatch.setattr(search.shutil, "which", lambda _name: None)
    return request.param


def test_single_keyword_matches_every_session_containing_it(tmp_path, backend):
    sessions = _keyword_fixture(tmp_path)
    assert _names(search_session_ids(["WORKTREE"], sessions)) == {"both", "split", "partial", "sidecar"}


def test_two_keywords_and_across_records_and_sidecars(tmp_path, backend):
    sessions = _keyword_fixture(tmp_path)
    # "split" proves the AND is session-level, not line-level; "sidecar" proves subagent transcripts count.
    assert _names(search_session_ids(["worktree", "deleted"], sessions)) == {"both", "split", "sidecar"}


def test_unmatched_keyword_empties_the_intersection(tmp_path, backend):
    sessions = _keyword_fixture(tmp_path)
    assert search_session_ids(["worktree", "absent-needle-zzz"], sessions) == set()


def test_any_keeps_sessions_holding_only_one_keyword(tmp_path, backend):
    sessions = _keyword_fixture(tmp_path)
    ids = search_session_ids(["worktree", "deleted"], sessions, match_any=True)
    assert _names(ids) == {"both", "split", "partial", "sidecar"}


def test_any_ignores_keywords_nothing_contains(tmp_path, backend):
    sessions = _keyword_fixture(tmp_path)
    ids = search_session_ids(["deleted", "absent-needle-zzz"], sessions, match_any=True)
    assert _names(ids) == {"both", "split", "sidecar"}


def test_only_the_given_sessions_can_match(tmp_path, backend):
    sessions = [s for s in _keyword_fixture(tmp_path) if s.session_id != sid("both")]
    assert _names(search_session_ids(["worktree", "deleted"], sessions)) == {"split", "sidecar"}


def test_blank_queries_and_no_sessions_short_circuit(tmp_path, monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("no search may run without queries or sessions")

    monkeypatch.setattr(search.subprocess, "run", _boom)
    monkeypatch.setattr(search, "sessions_containing", _boom)
    sessions = _keyword_fixture(tmp_path)
    assert search_session_ids([], sessions) == set()
    assert search_session_ids(["   "], sessions, match_any=True) == set()
    assert search_session_ids(["needle"], []) == set()


def test_rg_exit_2_raises(tmp_path, monkeypatch):
    sessions = _keyword_fixture(tmp_path)
    monkeypatch.setattr(search.shutil, "which", lambda _name: "/usr/bin/rg")
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: _fake_completed(2, stderr="boom"))
    with pytest.raises(RipgrepError):
        search_session_ids(["needle"], sessions)
    with pytest.raises(RipgrepError):
        search_session_ids(["one", "two"], sessions, match_any=True)


def test_rg_and_stops_after_an_empty_pass(tmp_path, monkeypatch):
    sessions = _keyword_fixture(tmp_path)
    calls: list[list[str]] = []

    def _run(args, **_k):
        calls.append(_patterns(args))
        return _fake_completed(1)

    monkeypatch.setattr(search.shutil, "which", lambda _name: "/usr/bin/rg")
    monkeypatch.setattr(search.subprocess, "run", _run)
    assert search_session_ids(["first", "second", "third"], sessions) == set()
    # The first pass matched nothing, so the intersection is already empty — no further rg runs.
    assert calls == [["first"]]


def test_rg_any_is_a_single_pass_carrying_every_keyword(tmp_path, monkeypatch):
    sessions = _keyword_fixture(tmp_path)
    calls: list[list[str]] = []

    def _run(args, **_k):
        calls.append(_patterns(args))
        return _fake_completed(1)

    monkeypatch.setattr(search.shutil, "which", lambda _name: "/usr/bin/rg")
    monkeypatch.setattr(search.subprocess, "run", _run)
    assert search_session_ids(["first", "second", "third"], sessions, match_any=True) == set()
    assert calls == [["first", "second", "third"]]
