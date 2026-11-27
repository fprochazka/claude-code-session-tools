from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cc_blackbox import AssistantText, ReadDiagnostics, SubagentRun, Thinking, iter_entries
from conftest import compaction_records

from cc_session_tools.render import DistilledSession, RenderedPrompt, distill_entries, render_markdown


def _distill(tmp_path: Path, *records: dict[str, Any] | str, include_thinking: bool = False) -> DistilledSession:
    path = tmp_path / "sess.jsonl"
    path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records), encoding="utf-8")
    diagnostics = ReadDiagnostics()
    entries = list(iter_entries(path, diagnostics=diagnostics))
    return distill_entries("sess", entries, diagnostics, include_thinking=include_thinking)


def _user(content: object, **extra: object) -> dict[str, Any]:
    return {"type": "user", "message": {"role": "user", "content": content}, **extra}


def test_genuine_prompt_is_kept(tmp_path: Path) -> None:
    session = _distill(tmp_path, _user("do the thing", cwd="/repo", timestamp="2026-07-24T10:00:00.000Z"))
    assert [type(i) for i in session.items] == [RenderedPrompt]
    assert isinstance(session.items[0], RenderedPrompt)
    assert session.items[0].text == "do the thing"
    assert session.cwd == "/repo"
    assert session.started_at == session.ended_at == "2026-07-24T10:00:00.000Z"


def test_tool_results_and_meta_injections_are_not_prompts(tmp_path: Path) -> None:
    session = _distill(
        tmp_path,
        _user([{"type": "tool_result", "tool_use_id": "x", "content": "output"}]),
        _user("skill docs injection", isMeta=True),
    )
    assert session.items == []


def test_assistant_text_kept_tool_use_dropped(tmp_path: Path) -> None:
    session = _distill(
        tmp_path,
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "text", "text": "here is my answer"},
                    {"type": "tool_use", "name": "Bash", "id": "t1", "input": {"command": "ls"}},
                ]
            },
        },
    )
    assert [type(i) for i in session.items] == [AssistantText]
    assert isinstance(session.items[0], AssistantText)
    assert session.items[0].text == "here is my answer"


def test_thinking_excluded_by_default_included_on_flag(tmp_path: Path) -> None:
    record = {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}}
    assert _distill(tmp_path, record).items == []
    included = _distill(tmp_path, record, include_thinking=True)
    assert [type(i) for i in included.items] == [Thinking]
    assert isinstance(included.items[0], Thinking)
    assert included.items[0].text == "hmm"


def test_subagent_prompt_and_final_are_paired(tmp_path: Path) -> None:
    session = _distill(
        tmp_path,
        _user("kick off research"),
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Agent",
                        "id": "toolu_1",
                        "input": {"description": "Research foo", "subagent_type": "researcher", "prompt": "find foo"},
                    }
                ]
            },
        },
        _user(
            [{"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "foo is bar"}]}],
            toolUseResult={"agentId": "a1"},
        ),
    )
    assert [type(i) for i in session.items] == [RenderedPrompt, SubagentRun]
    sub = session.items[1]
    assert isinstance(sub, SubagentRun)
    assert (sub.description, sub.subagent_type, sub.prompt, sub.result) == (
        "Research foo",
        "researcher",
        "find foo",
        "foo is bar",
    )
    assert session.subagent_count == 1
    markdown = render_markdown(session)
    assert "### ⤷ Subagent · Research foo (researcher)" in markdown
    assert "foo is bar" in markdown


def test_tool_use_result_as_string_does_not_crash(tmp_path: Path) -> None:
    session = _distill(tmp_path, _user("a prompt", toolUseResult="plain string"))
    assert [type(i) for i in session.items] == [RenderedPrompt]


def test_malformed_and_unknown_types_are_counted_not_thrown(tmp_path: Path) -> None:
    session = _distill(tmp_path, "{not json", {"type": "progress"}, {"type": "totally-new-type"}, _user("real"))
    assert session.diagnostics.malformed_lines == 1
    assert session.diagnostics.unknown_types == {"totally-new-type": 1}
    assert [type(i) for i in session.items] == [RenderedPrompt]
    assert "unknown entry types encountered: totally-new-type×1" in render_markdown(session)


def test_system_reminder_stripped_from_prompt(tmp_path: Path) -> None:
    session = _distill(tmp_path, _user("<system-reminder>ctx</system-reminder>actual ask"))
    assert [i.text for i in session.items if isinstance(i, RenderedPrompt)] == ["actual ask"]


def test_slash_and_shell_commands_are_one_line_notes(tmp_path: Path) -> None:
    session = _distill(
        tmp_path,
        _user("<command-name>/review</command-name><command-args>42</command-args>"),
        _user("<bash-input>git status</bash-input><bash-stdout>clean</bash-stdout>"),
        _user("<local-command-stdout>noise only</local-command-stdout>"),
    )
    assert [i.text for i in session.items if isinstance(i, RenderedPrompt)] == [
        "_(ran /review 42)_",
        "_(ran shell: `git status`)_",
    ]


def test_headings_name_who_sent_a_prompt(tmp_path: Path) -> None:
    session = _distill(
        tmp_path,
        _user("typed by the user", origin={"kind": "human"}),
        _user(
            '<cross-session-message from="did:ci">build failed</cross-session-message>',
            origin={"kind": "peer"},
            isMeta=True,
        ),
        _user("<task-notification>done</task-notification>", origin={"kind": "task-notification"}),
        _user("from the coordinator", origin={"kind": "coordinator"}),
        {
            "type": "attachment",
            "attachment": {"type": "queued_command", "prompt": "also this", "commandMode": "prompt"},
        },
    )
    headings = [line for line in render_markdown(session).splitlines() if line.startswith("## ")]
    assert headings == [
        "## 👤 User",
        "## 📨 Peer session",
        "## 🔔 Task notification",
        "## 📨 Message from coordinator",
        "## 👤 User",
    ]
    assert session.prompt_count == 5


def _said(uuid: str, text: str, minute: int) -> dict[str, Any]:
    return _user(text, uuid=uuid, timestamp=f"2026-07-24T10:{minute:02d}:00.000Z", cwd="/repo")


def _answer(uuid: str, text: str, minute: int) -> dict[str, Any]:
    return {
        "type": "assistant",
        "uuid": uuid,
        "timestamp": f"2026-07-24T10:{minute:02d}:00.000Z",
        "message": {"content": [{"type": "text", "text": text}]},
    }


def _texts(items: list[Any]) -> list[str]:
    return [item.text for item in items if isinstance(item, RenderedPrompt | AssistantText)]


def test_a_session_without_compactions_renders_as_before(tmp_path: Path) -> None:
    session = _distill(tmp_path, _said("u1", "do the thing", 0), _answer("a1", "done", 1))
    assert len(session.parts) == 1
    assert render_markdown(session) == (
        "# Session sess\n"
        "_Project: /repo  ·  1 prompts  ·  0 subagents  ·  2026-07-24T10:00:00.000Z → 2026-07-24T10:01:00.000Z_\n"
        "\n"
        "## 👤 User\n"
        "do the thing\n"
        "\n"
        "## 🤖 Assistant\n"
        "done\n"
    )


def test_a_compaction_without_preserved_segment_splits_at_the_boundary(tmp_path: Path) -> None:
    session = _distill(
        tmp_path,
        _said("u1", "first ask", 0),
        _answer("a1", "first answer", 1),
        *compaction_records("b1", timestamp="2026-07-24T10:05:00.000Z", logical_parent="a1", trigger="manual"),
        _said("u2", "<command-name>/compact</command-name>", 5),
        _said("u3", "second ask", 6),
        _answer("a3", "second answer", 7),
    )
    first, main = session.parts
    assert _texts(first.items) == ["first ask", "first answer"]
    assert _texts(main.items) == ["_(ran /compact)_", "second ask", "second answer"]
    assert (main.started_at, main.ended_at) == ("2026-07-24T10:05:00.000Z", "2026-07-24T10:07:00.000Z")
    assert (first.started_at, first.ended_at) == ("2026-07-24T10:00:00.000Z", "2026-07-24T10:01:00.000Z")

    out = tmp_path / "out"
    markdown = render_markdown(session, output_dir=out)
    lines = markdown.splitlines()
    assert lines[0] == "# Session sess"
    assert lines[1] == (
        "> Compacted session: this part starts at compaction 1 of 1 (manual, 2026-07-24 10:05 UTC, 180k tokens"
        f" before it). The conversation before it is distilled in {out / 'sess.pre-compact-1.md'}."
    )
    assert lines[3].startswith("_Project: /repo  ·  2 prompts  ·  0 subagents  ·  2026-07-24T10:05:00.000Z")
    assert lines[5:7] == [
        "## 📦 Compaction summary",
        "This session is being continued from a previous conversation. Summary: the plan so far.",
    ]
    assert "first ask" not in markdown
    assert "Continued after" not in markdown

    earlier = render_markdown(session, first, output_dir=out)
    assert "Compaction summary" not in earlier
    assert "first ask" in earlier and "second ask" not in earlier
    assert earlier.rstrip().splitlines()[-1] == f"> Continued after compaction 1 in {out / 'sess.md'}."


def test_the_preserved_stretch_belongs_to_the_part_after_the_compaction(tmp_path: Path) -> None:
    session = _distill(
        tmp_path,
        _said("u1", "old ask", 0),
        _answer("a1", "old answer", 1),
        _said("u2", "kept ask", 2),
        _answer("a2", "kept answer", 3),
        *compaction_records("b1", timestamp="2026-07-24T10:05:00.000Z", logical_parent="a2", preserved_head="u2"),
        _said("u3", "new ask", 6),
    )
    first, main = session.parts
    assert _texts(first.items) == ["old ask", "old answer"]
    assert _texts(main.items) == ["kept ask", "kept answer", "new ask"]
    markdown = render_markdown(session)
    # The summary comes first, then the stretch Claude Code kept next to it.
    assert markdown.index("Compaction summary") < markdown.index("kept ask") < markdown.index("new ask")


def test_two_compactions_chain_their_parts(tmp_path: Path) -> None:
    session = _distill(
        tmp_path,
        _said("u1", "one", 0),
        *compaction_records("b1", timestamp="2026-07-24T10:05:00.000Z", summary="first summary"),
        _said("u2", "two", 6),
        *compaction_records("b2", timestamp="2026-07-24T10:09:00.000Z", pre_tokens=950, summary="second summary"),
        _said("u3", "three", 10),
    )
    assert [_texts(part.items) for part in session.parts] == [["one"], ["two"], ["three"]]
    assert [session.part_file_name(part) for part in session.parts] == [
        "sess.pre-compact-1.md",
        "sess.pre-compact-2.md",
        "sess.md",
    ]
    first, middle, main = (render_markdown(session, part) for part in session.parts)
    assert first.splitlines()[1] == "> Compacted session: this part is the conversation before compaction 1 of 2."
    assert first.rstrip().endswith("> Continued after compaction 1 in sess.pre-compact-2.md.")
    assert middle.splitlines()[1].startswith("> Compacted session: this part starts at compaction 1 of 2 (auto, ")
    assert middle.splitlines()[1].endswith("The conversation before it is distilled in sess.pre-compact-1.md.")
    assert "first summary" in middle and "second summary" not in middle
    assert middle.rstrip().endswith("> Continued after compaction 2 in sess.md.")
    assert "(auto, 2026-07-24 10:09 UTC, 950 tokens before it)" in main.splitlines()[1]
    assert main.splitlines()[1].endswith("distilled in sess.pre-compact-2.md.")
    assert "second summary" in main


def test_a_subagent_spawned_before_a_compaction_keeps_its_later_result(tmp_path: Path) -> None:
    spawn = {
        "type": "assistant",
        "uuid": "a1",
        "timestamp": "2026-07-24T10:01:00.000Z",
        "message": {
            "content": [
                {
                    "type": "tool_use",
                    "name": "Agent",
                    "id": "toolu_1",
                    "input": {"description": "Research", "prompt": "go"},
                }
            ]
        },
    }
    result = _user(
        [{"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "the finding"}]}],
        uuid="r1",
        timestamp="2026-07-24T10:08:00.000Z",
        toolUseResult={"agentId": "a1b2"},
    )
    session = _distill(
        tmp_path,
        _said("u1", "start research", 0),
        spawn,
        *compaction_records("b1", timestamp="2026-07-24T10:05:00.000Z", logical_parent="a1"),
        result,
        _said("u2", "thanks", 9),
    )
    first, main = session.parts
    [run] = [item for item in first.items if isinstance(item, SubagentRun)]
    assert run.result == "the finding"
    assert main.subagent_count == 0
    assert _texts(main.items) == ["thanks"]


def test_a_missing_summary_or_preserved_head_still_splits(tmp_path: Path) -> None:
    session = _distill(
        tmp_path,
        _said("u1", "before", 0),
        *compaction_records("b1", timestamp="2026-07-24T10:05:00.000Z", preserved_head="never-written", summary=None),
        _said("u2", "after", 6),
    )
    first, main = session.parts
    assert (_texts(first.items), _texts(main.items)) == (["before"], ["after"])
    assert "## 📦 Compaction summary\n_(the summary is missing from the transcript)_" in render_markdown(session)


def test_diagnostics_go_into_the_main_part_only(tmp_path: Path) -> None:
    session = _distill(
        tmp_path,
        {"type": "totally-new-type"},
        _said("u1", "before", 0),
        *compaction_records("b1", timestamp="2026-07-24T10:05:00.000Z"),
    )
    assert "unknown entry types" not in render_markdown(session, session.parts[0])
    assert "unknown entry types" in render_markdown(session)


def test_items_are_placed_by_their_record_or_by_the_item_before_them(tmp_path: Path) -> None:
    two_blocks = {
        "type": "assistant",
        "uuid": "a1",
        "timestamp": "2026-07-24T10:01:00.000Z",
        "message": {"content": [{"type": "text", "text": "first block"}, {"type": "text", "text": "second block"}]},
    }
    # A record without a uuid cannot be found, so its text takes the position of the item before it: the last item
    # before the compaction, although the record itself comes after the boundary.
    no_uuid = {
        "type": "assistant",
        "timestamp": "2026-07-24T10:06:00.000Z",
        "message": {"content": [{"type": "text", "text": "unplaced"}]},
    }
    session = _distill(
        tmp_path,
        _said("u1", "ask", 0),
        two_blocks,
        *compaction_records("b1", timestamp="2026-07-24T10:05:00.000Z", logical_parent="a1"),
        no_uuid,
        _said("u2", "after", 7),
        _answer("a2", "answer after", 8),
    )
    first, main = session.parts
    assert _texts(first.items) == ["ask", "first block", "second block", "unplaced"]
    assert _texts(main.items) == ["after", "answer after"]
