from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import cc_session_tools.transcript as transcript
from cc_session_tools.transcript import read_session_meta

FIXTURES = Path(__file__).parent / "fixtures"


def test_ai_title_is_genuine_name():
    meta = read_session_meta(FIXTURES / "ai_title.jsonl")
    assert meta.session_id == "ai_title"
    assert meta.name == "My Great Session"
    assert meta.name_is_derived is False
    assert meta.cwd == "/home/u/proj"
    # Max timestamp across records, not the first line.
    assert meta.last_ts == datetime(2026, 2, 13, 14, 22, 15, tzinfo=UTC)


def test_summary_is_genuine_name_and_cwd_from_first_carrying_record():
    meta = read_session_meta(FIXTURES / "summary_only.jsonl")
    assert meta.name == "Retro Summary Name"
    assert meta.name_is_derived is False
    # The summary record has no cwd; cwd must come from the first record that does.
    assert meta.cwd == "/home/u/proj2"


def test_clean_first_user_message_is_derived():
    meta = read_session_meta(FIXTURES / "clean_user.jsonl")
    assert meta.name == "Fix the login bug please"
    assert meta.name_is_derived is True
    assert meta.cwd == "/home/u/proj3"


def test_injection_noise_is_skipped_for_first_real_message():
    meta = read_session_meta(FIXTURES / "injection_noise.jsonl")
    # bash-input, command-name, a tool_result array, and a malformed line are all
    # skipped; the first genuine human message wins.
    assert meta.name == "The real question is here"
    assert meta.name_is_derived is True
    assert meta.cwd == "/home/u/proj4"


def test_last_ts_is_true_max_not_literal_last_line():
    meta = read_session_meta(FIXTURES / "ts_out_of_order.jsonl")
    # The final timestamped line is 08:00:05.200, but the true max is 08:00:05.500,
    # and the trailing ai-title (no timestamp) must not disturb it.
    assert meta.last_ts == datetime(2026, 2, 13, 8, 0, 5, 500000, tzinfo=UTC)
    assert meta.name == "Ordered Session"


def test_no_timestamp_falls_back_to_mtime(tmp_path):
    src = (FIXTURES / "no_timestamp.jsonl").read_text()
    target = tmp_path / "no_timestamp.jsonl"
    target.write_text(src)
    fixed = datetime(2020, 5, 1, 12, 0, 0, tzinfo=UTC).timestamp()
    os.utime(target, (fixed, fixed))

    meta = read_session_meta(target)
    assert meta.last_ts == datetime.fromtimestamp(fixed, tz=UTC)
    # Name chain still resolves off timestamp-less metadata records.
    assert meta.name == "Also No Time"
    assert meta.name_is_derived is False
    assert meta.cwd is None


def test_large_file_reads_head_and_tail_only(tmp_path, monkeypatch):
    # Shrink the slice thresholds so a modest transcript takes the head/tail path
    # instead of being read whole — exercising the boundary trimming.
    monkeypatch.setattr(transcript, "_HEAD_BYTES", 200)
    monkeypatch.setattr(transcript, "_TAIL_BYTES", 200)

    lines: list[str] = [
        # HEAD: real cwd + first human message, both must land in the head slice.
        json.dumps(
            {
                "type": "user",
                "cwd": "/head/cwd",
                "sessionId": "s",
                "timestamp": "2026-01-01T00:00:00.000Z",
                "message": {"role": "user", "content": "real head message"},
            }
        ),
    ]
    # MIDDLE: padding to push total size past the threshold. This region is never
    # read, so its decoys — an ai-title that would top the name chain and a rival
    # cwd — must not leak into the result. Boundaries cut through these lines,
    # proving a straddling record is dropped without a json.loads crash.
    for i in range(20):
        lines.append(
            json.dumps(
                {
                    "type": "user",
                    "sessionId": "s",
                    "timestamp": f"2026-03-01T00:00:{i:02d}.000Z",
                    "message": {"content": "padding " + "x" * 30},
                }
            )
        )
    lines.append(json.dumps({"type": "ai-title", "aiTitle": "MIDDLE DECOY TITLE", "sessionId": "s"}))
    lines.append(
        json.dumps(
            {
                "type": "user",
                "cwd": "/middle/decoy",
                "sessionId": "s",
                "timestamp": "2026-03-01T09:99:99.000Z",
                "message": {"content": "decoy"},
            }
        )
    )
    for i in range(20):
        lines.append(
            json.dumps(
                {
                    "type": "assistant",
                    "sessionId": "s",
                    "timestamp": f"2026-03-02T00:00:{i:02d}.000Z",
                    "message": {"content": [{"type": "text", "text": "y" * 30}]},
                }
            )
        )
    lines += [
        # TAIL: a padding line (the boundary chops it, so it is the dropped partial
        # first line) followed by the record carrying the true max timestamp.
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "s",
                "timestamp": "2026-05-01T00:00:00.000Z",
                "message": {"content": [{"type": "text", "text": "tail padding"}]},
            }
        ),
        json.dumps(
            {
                "type": "user",
                "sessionId": "s",
                "timestamp": "2026-06-01T00:00:00.000Z",
                "message": {"content": "tail max"},
            }
        ),
    ]

    target = tmp_path / "big.jsonl"
    target.write_text("\n".join(lines) + "\n")
    assert target.stat().st_size > transcript._HEAD_BYTES + transcript._TAIL_BYTES

    meta = read_session_meta(target)
    assert meta.cwd == "/head/cwd"  # head value, middle decoy never seen
    assert meta.name == "real head message"  # head-derived, not the middle ai-title decoy
    assert meta.name_is_derived is True
    assert meta.last_ts == datetime(2026, 6, 1, tzinfo=UTC)  # tail max


def test_git_branch_and_version_come_from_the_first_record_carrying_them():
    meta = read_session_meta(FIXTURES / "branch_version.jsonl")
    # The opening record carries neither key and the next one carries an empty
    # gitBranch (detached HEAD); both are skipped, so the branch is the first
    # non-empty one while the version is taken from the earlier record that has it.
    assert meta.git_branch == "fp-feature"
    assert meta.version == "2.0.31"


def test_git_branch_and_version_are_none_when_absent():
    meta = read_session_meta(FIXTURES / "clean_user.jsonl")
    assert meta.git_branch is None
    assert meta.version is None
