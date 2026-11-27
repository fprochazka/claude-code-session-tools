from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cc_blackbox import Entry, iter_entries
from conftest import compaction_records

from cc_session_tools.segments import compaction_segments


def _entries(tmp_path: Path, *records: dict[str, Any]) -> list[Entry]:
    path = tmp_path / "sess.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return list(iter_entries(path))


def _user(uuid: str, text: str) -> dict[str, Any]:
    return {"type": "user", "uuid": uuid, "timestamp": "2026-07-24T10:00:00.000Z", "message": {"content": text}}


def _spans(entries: list[Entry]) -> list[tuple[int, int, str, int | None]]:
    return [(s.start, s.end, s.start_source, s.boundary_index) for s in compaction_segments(entries)]


def test_a_transcript_without_compactions_is_one_segment(tmp_path: Path) -> None:
    entries = _entries(tmp_path, _user("u1", "one"), _user("u2", "two"))
    [segment] = compaction_segments(entries)
    assert (segment.start, segment.end, segment.compaction, segment.start_source) == (0, 2, None, "transcript_start")


def test_an_empty_transcript_is_one_empty_segment(tmp_path: Path) -> None:
    assert _spans(_entries(tmp_path)) == [(0, 0, "transcript_start", None)]


def test_a_compaction_without_preserved_segment_splits_at_its_boundary(tmp_path: Path) -> None:
    entries = _entries(
        tmp_path,
        _user("u1", "before"),
        *compaction_records("b1", timestamp="2026-07-24T10:05:00.000Z", logical_parent="u1"),
        _user("u2", "after"),
    )
    assert _spans(entries) == [(0, 1, "transcript_start", None), (1, 4, "boundary", 1)]
    compaction = compaction_segments(entries)[1].compaction
    assert compaction is not None
    assert compaction.uuid == "b1"
    assert compaction.summary is not None


def test_a_preserved_segment_starts_the_next_segment_at_its_head(tmp_path: Path) -> None:
    entries = _entries(
        tmp_path,
        _user("u1", "old"),
        _user("u2", "kept verbatim"),
        _user("u3", "kept verbatim too"),
        *compaction_records("b1", timestamp="2026-07-24T10:05:00.000Z", logical_parent="u3", preserved_head="u2"),
        _user("u4", "after"),
    )
    assert _spans(entries) == [(0, 1, "transcript_start", None), (1, 6, "preserved_head", 3)]


def test_a_preserved_head_missing_from_the_file_falls_back_to_the_boundary(tmp_path: Path) -> None:
    entries = _entries(
        tmp_path,
        _user("u1", "old"),
        *compaction_records("b1", timestamp="2026-07-24T10:05:00.000Z", preserved_head="never-written"),
    )
    assert _spans(entries) == [(0, 1, "transcript_start", None), (1, 3, "boundary", 1)]


def test_a_preserved_head_before_the_previous_segment_falls_back_to_the_boundary(tmp_path: Path) -> None:
    entries = _entries(
        tmp_path,
        _user("u1", "first part"),
        *compaction_records("b1", timestamp="2026-07-24T10:05:00.000Z"),
        _user("u2", "second part"),
        # Points into the first segment, which would put u1 into two segments.
        *compaction_records("b2", timestamp="2026-07-24T10:09:00.000Z", preserved_head="u1"),
        _user("u3", "third part"),
    )
    assert _spans(entries) == [
        (0, 1, "transcript_start", None),
        (1, 4, "boundary", 1),
        (4, 7, "boundary", 4),
    ]


def test_two_compactions_give_three_segments_that_cover_every_record(tmp_path: Path) -> None:
    entries = _entries(
        tmp_path,
        _user("u1", "first"),
        *compaction_records("b1", timestamp="2026-07-24T10:05:00.000Z", trigger="manual"),
        _user("u2", "second"),
        _user("u3", "kept"),
        *compaction_records("b2", timestamp="2026-07-24T10:09:00.000Z", logical_parent="u3", preserved_head="u3"),
        _user("u4", "third"),
    )
    segments = compaction_segments(entries)
    assert _spans(entries) == [
        (0, 1, "transcript_start", None),
        (1, 4, "boundary", 1),
        (4, 8, "preserved_head", 5),
    ]
    assert [s.compaction.trigger if s.compaction else None for s in segments] == [None, "manual", "auto"]
    covered = [index for s in segments for index in range(s.start, s.end)]
    assert covered == list(range(len(entries)))


def test_a_summary_record_without_a_boundary_does_not_split(tmp_path: Path) -> None:
    [_, summary] = compaction_records("b1", timestamp="2026-07-24T10:05:00.000Z")
    entries = _entries(tmp_path, _user("u1", "one"), summary, _user("u2", "two"))
    assert _spans(entries) == [(0, 3, "transcript_start", None)]
