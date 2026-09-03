from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

import cc_session_tools.search as search
from cc_session_tools.search import (
    RipgrepError,
    RipgrepNotFoundError,
    build_rg_args,
    match_path_to_session_id,
    search_session_ids,
)


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


def test_match_path_top_level_jsonl():
    projects = Path("/home/u/.claude/projects")
    hit = projects / "encoded-proj" / "abc-123.jsonl"
    assert match_path_to_session_id(hit, projects) == "abc-123"


def test_match_path_subagent_folds_to_parent():
    projects = Path("/home/u/.claude/projects")
    hit = projects / "encoded-proj" / "abc-123" / "subagents" / "agent-xyz.jsonl"
    assert match_path_to_session_id(hit, projects) == "abc-123"


def test_match_path_tool_result_folds_to_parent():
    projects = Path("/home/u/.claude/projects")
    hit = projects / "encoded-proj" / "abc-123" / "tool-results" / "toolu_x.txt"
    assert match_path_to_session_id(hit, projects) == "abc-123"


def test_match_path_non_jsonl_top_level_is_none():
    projects = Path("/home/u/.claude/projects")
    hit = projects / "encoded-proj" / "notes.txt"
    assert match_path_to_session_id(hit, projects) is None


def test_match_path_outside_projects_dir_is_none():
    projects = Path("/home/u/.claude/projects")
    assert match_path_to_session_id(Path("/etc/passwd"), projects) is None


def test_match_path_too_shallow_is_none():
    projects = Path("/home/u/.claude/projects")
    # A bare file directly in projects dir has no <proj>/<file> depth.
    assert match_path_to_session_id(projects / "loose.jsonl", projects) is None


def test_search_blank_queries_short_circuit(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("subprocess.run must not be invoked for blank queries")

    monkeypatch.setattr(search.subprocess, "run", _boom)
    assert search_session_ids(["   "], [Path("/p/one")], Path("/p")) == set()


def test_search_no_queries_short_circuits(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("subprocess.run must not be invoked without queries")

    monkeypatch.setattr(search.subprocess, "run", _boom)
    assert search_session_ids([], [Path("/p/one")], Path("/p")) == set()


def test_search_empty_dirs_short_circuits(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("subprocess.run must not be invoked with no dirs")

    monkeypatch.setattr(search.subprocess, "run", _boom)
    assert search_session_ids(["needle"], [], Path("/p")) == set()


def _fake_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["rg"], returncode=returncode, stdout=stdout, stderr=stderr)


def _patterns(args: list[str]) -> list[str]:
    """The ``-e`` values of a built argv, in order."""
    return [args[i + 1] for i, arg in enumerate(args) if arg == "-e"]


def test_search_exit_1_is_empty(monkeypatch):
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: _fake_completed(1))
    assert search_session_ids(["needle"], [Path("/p/proj")], Path("/p")) == set()


def test_search_exit_0_maps_stdout(monkeypatch):
    projects = Path("/home/u/.claude/projects")
    stdout = "\n".join(
        [
            str(projects / "proj" / "sess-1.jsonl"),
            str(projects / "proj" / "sess-2" / "subagents" / "agent-a.jsonl"),
            str(projects / "proj" / "sess-2" / "tool-results" / "toolu_b.txt"),
            "",  # blank line ignored
        ]
    )
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: _fake_completed(0, stdout=stdout))
    assert search_session_ids(["needle"], [projects / "proj"], projects) == {"sess-1", "sess-2"}


def test_search_exit_2_raises(monkeypatch):
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: _fake_completed(2, stderr="boom"))
    with pytest.raises(RipgrepError):
        search_session_ids(["needle"], [Path("/p/proj")], Path("/p"))


def test_search_missing_rg_raises(monkeypatch):
    def _enoent(*_a, **_k):
        raise FileNotFoundError("rg")

    monkeypatch.setattr(search.subprocess, "run", _enoent)
    with pytest.raises(RipgrepNotFoundError):
        search_session_ids(["needle"], [Path("/p/proj")], Path("/p"))


def test_search_intersects_per_keyword_passes(monkeypatch):
    projects = Path("/home/u/.claude/projects")
    per_query = {
        "one": f"{projects / 'proj' / 'sess-1.jsonl'}\n{projects / 'proj' / 'sess-2.jsonl'}\n",
        "two": f"{projects / 'proj' / 'sess-2.jsonl'}\n{projects / 'proj' / 'sess-3.jsonl'}\n",
    }

    def _run(args, **_k):
        (pattern,) = _patterns(args)
        return _fake_completed(0, stdout=per_query[pattern])

    monkeypatch.setattr(search.subprocess, "run", _run)
    assert search_session_ids(["one", "two"], [projects / "proj"], projects) == {"sess-2"}


def test_search_early_exits_after_an_empty_pass(monkeypatch):
    calls: list[list[str]] = []

    def _run(args, **_k):
        calls.append(_patterns(args))
        return _fake_completed(1)

    monkeypatch.setattr(search.subprocess, "run", _run)
    assert search_session_ids(["first", "second", "third"], [Path("/p/proj")], Path("/p")) == set()
    # The first pass matched nothing, so the intersection is already empty — no further rg runs.
    assert calls == [["first"]]


def test_search_any_is_a_single_pass_carrying_every_keyword(monkeypatch):
    calls: list[list[str]] = []

    def _run(args, **_k):
        calls.append(_patterns(args))
        return _fake_completed(1)

    monkeypatch.setattr(search.subprocess, "run", _run)
    assert search_session_ids(["first", "second", "third"], [Path("/p/proj")], Path("/p"), match_any=True) == set()
    # The OR is expressible in one rg invocation — the whole point of the single-pass design.
    assert calls == [["first", "second", "third"]]


def test_search_any_no_queries_short_circuits(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("subprocess.run must not be invoked without queries")

    monkeypatch.setattr(search.subprocess, "run", _boom)
    assert search_session_ids([], [Path("/p/one")], Path("/p"), match_any=True) == set()
    assert search_session_ids(["  "], [Path("/p/one")], Path("/p"), match_any=True) == set()


def test_search_any_empty_dirs_short_circuits(monkeypatch):
    def _boom(*_a, **_k):
        raise AssertionError("subprocess.run must not be invoked with no dirs")

    monkeypatch.setattr(search.subprocess, "run", _boom)
    assert search_session_ids(["needle"], [], Path("/p"), match_any=True) == set()


def test_search_any_exit_2_raises(monkeypatch):
    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: _fake_completed(2, stderr="boom"))
    with pytest.raises(RipgrepError):
        search_session_ids(["one", "two"], [Path("/p/proj")], Path("/p"), match_any=True)


def test_search_any_missing_rg_raises(monkeypatch):
    def _enoent(*_a, **_k):
        raise FileNotFoundError("rg")

    monkeypatch.setattr(search.subprocess, "run", _enoent)
    with pytest.raises(RipgrepNotFoundError):
        search_session_ids(["one", "two"], [Path("/p/proj")], Path("/p"), match_any=True)


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_search_real_rg_happy_path(tmp_path):
    projects = tmp_path / "projects"
    proj = projects / "encoded-proj"
    (proj / "s-hit").mkdir(parents=True)
    (proj / "s-hit" / "subagents").mkdir()

    # Top-level session containing the term.
    (proj / "s-top.jsonl").write_text('{"text": "here is UNIQUETERM in the transcript"}\n')
    # Top-level session WITHOUT the term.
    (proj / "s-miss.jsonl").write_text('{"text": "nothing to see"}\n')
    # Subagent sidecar containing the term → folds to its parent session s-hit.
    (proj / "s-hit" / "subagents" / "agent-1.jsonl").write_text('{"text": "UNIQUETERM lives in a subagent"}\n')

    ids = search_session_ids(["uniqueterm"], [proj], projects)
    assert ids == {"s-top", "s-hit"}


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_search_real_rg_no_match(tmp_path):
    projects = tmp_path / "projects"
    proj = projects / "encoded-proj"
    proj.mkdir(parents=True)
    (proj / "s-top.jsonl").write_text('{"text": "nothing relevant"}\n')

    assert search_session_ids(["absent-needle-zzz"], [proj], projects) == set()


def _keyword_fixture(tmp_path) -> tuple[Path, Path]:
    """A projects tree exercising session-level matching: split records, sidecars, partial matches."""
    projects = tmp_path / "projects"
    proj = projects / "encoded-proj"
    proj.mkdir(parents=True)

    # Both keywords in the same record.
    (proj / "s-both.jsonl").write_text('{"text": "worktree and deleted together"}\n')
    # Both keywords in the session, but in two DIFFERENT records — no single line holds both.
    (proj / "s-split.jsonl").write_text('{"text": "about a worktree"}\n{"text": "it was deleted"}\n')
    # Only one of the two keywords.
    (proj / "s-partial.jsonl").write_text('{"text": "just a worktree"}\n')
    # Neither keyword — never a match, whatever the mode.
    (proj / "s-none.jsonl").write_text('{"text": "unrelated chatter"}\n')
    # One keyword in the transcript, the other only in a subagent sidecar of the same session.
    (proj / "s-sidecar" / "subagents").mkdir(parents=True)
    (proj / "s-sidecar.jsonl").write_text('{"text": "the worktree in question"}\n')
    (proj / "s-sidecar" / "subagents" / "agent-1.jsonl").write_text('{"text": "DELETED by the subagent"}\n')

    return projects, proj


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_search_real_rg_single_keyword_matches_every_session_containing_it(tmp_path):
    projects, proj = _keyword_fixture(tmp_path)
    assert search_session_ids(["worktree"], [proj], projects) == {"s-both", "s-split", "s-partial", "s-sidecar"}


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_search_real_rg_two_keywords_and_across_records_and_sidecars(tmp_path):
    projects, proj = _keyword_fixture(tmp_path)
    ids = search_session_ids(["worktree", "deleted"], [proj], projects)
    # s-split proves the AND is session-level, not line-level; s-sidecar proves sidecars count.
    assert ids == {"s-both", "s-split", "s-sidecar"}
    assert "s-partial" not in ids


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_search_real_rg_unmatched_keyword_empties_the_intersection(tmp_path):
    projects, proj = _keyword_fixture(tmp_path)
    assert search_session_ids(["worktree", "absent-needle-zzz"], [proj], projects) == set()


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_search_real_rg_any_keeps_sessions_holding_only_one_keyword(tmp_path):
    projects, proj = _keyword_fixture(tmp_path)
    ids = search_session_ids(["worktree", "deleted"], [proj], projects, match_any=True)
    # s-partial has only "worktree" — under AND it drops out, under OR it stays.
    assert ids == {"s-both", "s-split", "s-partial", "s-sidecar"}
    assert "s-none" not in ids


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_search_real_rg_any_ignores_keywords_nothing_contains(tmp_path):
    projects, proj = _keyword_fixture(tmp_path)
    # An unmatched keyword contributes nothing to the union instead of emptying it.
    ids = search_session_ids(["deleted", "absent-needle-zzz"], [proj], projects, match_any=True)
    assert ids == {"s-both", "s-split", "s-sidecar"}


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep (rg) not installed")
def test_search_real_rg_any_with_no_matching_keyword_is_empty(tmp_path):
    projects, proj = _keyword_fixture(tmp_path)
    assert search_session_ids(["absent-needle-zzz", "also-absent-yyy"], [proj], projects, match_any=True) == set()
