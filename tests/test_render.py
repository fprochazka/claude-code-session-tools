from __future__ import annotations

import json
from pathlib import Path

from cc_blackbox import AssistantText, ReadDiagnostics, SubagentRun, Thinking, iter_entries

from cc_session_tools.render import DistilledSession, RenderedPrompt, distill_entries, render_markdown


def _distill(tmp_path: Path, *records: dict | str, include_thinking: bool = False) -> DistilledSession:
    path = tmp_path / "sess.jsonl"
    path.write_text("".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in records), encoding="utf-8")
    diagnostics = ReadDiagnostics()
    entries = list(iter_entries(path, diagnostics=diagnostics))
    return distill_entries("sess", entries, diagnostics, include_thinking=include_thinking)


def _user(content: object, **extra: object) -> dict:
    return {"type": "user", "message": {"role": "user", "content": content}, **extra}


def test_genuine_prompt_is_kept(tmp_path):
    session = _distill(tmp_path, _user("do the thing", cwd="/repo", timestamp="2026-07-24T10:00:00.000Z"))
    assert [type(i) for i in session.items] == [RenderedPrompt]
    assert session.items[0].text == "do the thing"
    assert session.cwd == "/repo"
    assert session.started_at == session.ended_at == "2026-07-24T10:00:00.000Z"


def test_tool_results_and_meta_injections_are_not_prompts(tmp_path):
    session = _distill(
        tmp_path,
        _user([{"type": "tool_result", "tool_use_id": "x", "content": "output"}]),
        _user("skill docs injection", isMeta=True),
    )
    assert session.items == []


def test_assistant_text_kept_tool_use_dropped(tmp_path):
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
    assert session.items[0].text == "here is my answer"


def test_thinking_excluded_by_default_included_on_flag(tmp_path):
    record = {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}}
    assert _distill(tmp_path, record).items == []
    included = _distill(tmp_path, record, include_thinking=True)
    assert [type(i) for i in included.items] == [Thinking]
    assert included.items[0].text == "hmm"


def test_subagent_prompt_and_final_are_paired(tmp_path):
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


def test_tool_use_result_as_string_does_not_crash(tmp_path):
    session = _distill(tmp_path, _user("a prompt", toolUseResult="plain string"))
    assert [type(i) for i in session.items] == [RenderedPrompt]


def test_malformed_and_unknown_types_are_counted_not_thrown(tmp_path):
    session = _distill(tmp_path, "{not json", {"type": "progress"}, {"type": "totally-new-type"}, _user("real"))
    assert session.diagnostics.malformed_lines == 1
    assert session.diagnostics.unknown_types == {"totally-new-type": 1}
    assert [type(i) for i in session.items] == [RenderedPrompt]
    assert "unknown entry types encountered: totally-new-type×1" in render_markdown(session)


def test_system_reminder_stripped_from_prompt(tmp_path):
    session = _distill(tmp_path, _user("<system-reminder>ctx</system-reminder>actual ask"))
    assert [i.text for i in session.items if isinstance(i, RenderedPrompt)] == ["actual ask"]


def test_slash_and_shell_commands_are_one_line_notes(tmp_path):
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


def test_headings_name_who_sent_a_prompt(tmp_path):
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
