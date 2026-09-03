import json

from cc_session_tools.parse import (
    AssistantText,
    HumanPrompt,
    Subagent,
    Thinking,
    parse_session,
)


def _jsonl(*entries: dict) -> str:
    return "\n".join(json.dumps(e) for e in entries)


def test_genuine_prompt_is_kept():
    text = _jsonl(
        {"type": "user", "cwd": "/repo", "timestamp": "t1", "message": {"content": "do the thing"}},
    )
    session = parse_session(text, "sess")
    assert [type(e) for e in session.events] == [HumanPrompt]
    assert session.events[0].text == "do the thing"
    assert session.cwd == "/repo"


def test_tool_result_user_entry_is_dropped():
    text = _jsonl(
        {
            "type": "user",
            "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "output"}]},
        },
    )
    session = parse_session(text, "sess")
    assert session.events == []
    assert session.diagnostics.dropped_entries == 1


def test_meta_user_entry_is_dropped():
    text = _jsonl(
        {"type": "user", "isMeta": True, "message": {"content": "skill docs injection"}},
    )
    session = parse_session(text, "sess")
    assert session.events == []


def test_assistant_text_kept_tool_use_dropped():
    text = _jsonl(
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
    session = parse_session(text, "sess")
    assert [type(e) for e in session.events] == [AssistantText]
    assert session.events[0].text == "here is my answer"


def test_thinking_excluded_by_default_included_on_flag():
    text = _jsonl(
        {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": "hmm"}]}},
    )
    assert parse_session(text, "sess").events == []
    included = parse_session(text, "sess", include_thinking=True)
    assert [type(e) for e in included.events] == [Thinking]
    assert included.events[0].text == "hmm"


def test_subagent_prompt_and_final_are_paired():
    text = _jsonl(
        {"type": "user", "message": {"content": "kick off research"}},
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Agent",
                        "id": "toolu_1",
                        "input": {
                            "description": "Research foo",
                            "subagent_type": "researcher",
                            "prompt": "find facts about foo",
                        },
                    }
                ]
            },
        },
        {
            "type": "user",
            "toolUseResult": {"agentId": "agent-abc"},
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_1",
                        "content": [{"type": "text", "text": "foo is bar"}],
                    }
                ]
            },
        },
    )
    session = parse_session(text, "sess")
    assert [type(e) for e in session.events] == [HumanPrompt, Subagent]
    sub = session.events[1]
    assert sub.description == "Research foo"
    assert sub.subagent_type == "researcher"
    assert sub.prompt == "find facts about foo"
    assert sub.result == "foo is bar"
    assert session.subagent_count == 1


def test_toolUseResult_as_string_does_not_crash():
    text = _jsonl(
        {"type": "user", "toolUseResult": "plain string", "message": {"content": "a prompt"}},
    )
    session = parse_session(text, "sess")
    # A string toolUseResult is not a subagent anchor; the entry is a normal prompt.
    assert [type(e) for e in session.events] == [HumanPrompt]


def test_malformed_and_unknown_types_are_counted_not_thrown():
    text = "\n".join(
        [
            "{not json",
            json.dumps({"type": "progress"}),
            json.dumps({"type": "totally-new-type"}),
            json.dumps({"type": "user", "message": {"content": "real"}}),
        ]
    )
    session = parse_session(text, "sess")
    assert session.diagnostics.malformed_lines == 1
    assert session.diagnostics.dropped_entries == 1  # progress
    assert session.diagnostics.unknown_types == {"totally-new-type": 1}
    assert [type(e) for e in session.events] == [HumanPrompt]


def test_system_reminder_stripped_from_prompt():
    text = _jsonl(
        {
            "type": "user",
            "message": {"content": "<system-reminder>ctx</system-reminder>actual ask"},
        },
    )
    session = parse_session(text, "sess")
    assert [type(e) for e in session.events] == [HumanPrompt]
    assert session.events[0].text == "actual ask"
