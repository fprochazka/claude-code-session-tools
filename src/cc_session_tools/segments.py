"""Split a transcript at its compactions into the stretches of conversation the model worked with.

A compaction continues in the same transcript file. Claude Code writes a ``compact_boundary`` system record and a
summary record after it, and from about 2.1.266 keeps the last stretch of the conversation verbatim in the new context.
Those preserved records stay before the boundary in the file and are not written again. So the context after a
compaction is its summary, the records from the preserved head to the boundary, and everything after the boundary.

Each record of the transcript lands in exactly one segment: a segment after a compaction starts at the compaction's
preserved head when the transcript has one, else at its boundary record.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from cc_blackbox import Compaction, Entry, SystemEntry, compactions

SegmentStart = Literal["transcript_start", "preserved_head", "boundary"]


@dataclass(frozen=True)
class Segment:
    """One stretch of a transcript between two compactions, as indexes into the entry list."""

    start: int
    end: int
    """Exclusive: the next segment's ``start``, or the length of the entry list for the last segment."""
    compaction: Compaction | None
    """The compaction that opened this segment; ``None`` for the first segment."""
    boundary_index: int | None
    """The index of that compaction's ``compact_boundary`` record; ``None`` for the first segment."""
    start_source: SegmentStart
    """``preserved_head`` when the segment starts at the records the compaction kept verbatim, ``boundary`` when it
    starts at the boundary record because the transcript names no preserved head, or names one this module cannot
    place, and ``transcript_start`` for the first segment."""


def _is_boundary(entry: Entry) -> bool:
    return isinstance(entry, SystemEntry) and entry.subtype == "compact_boundary"


def _preserved_head(entries: Sequence[Entry], head_uuid: str | None, low: int, boundary: int) -> int | None:
    # The preserved stretch ends at the boundary and cannot reach into the previous segment, which would leave a record
    # in two segments. A head outside that range, or missing from the file, is not used.
    if head_uuid is None:
        return None
    for index in range(low, boundary):
        if entries[index].uuid == head_uuid:
            return index
    return None


def compaction_segments(entries: Sequence[Entry]) -> list[Segment]:
    """Return the segments of a transcript in file order: one for a transcript without compactions, N+1 for N.

    ``entries`` is the whole main transcript in file order. A summary record without a boundary does not split.
    """
    boundaries = [index for index, entry in enumerate(entries) if _is_boundary(entry)]
    # compactions() yields one Compaction per boundary record, in file order.
    found = list(compactions(entries))
    starts: list[tuple[int, Compaction | None, int | None, SegmentStart]] = [(0, None, None, "transcript_start")]
    for boundary, compaction in zip(boundaries, found, strict=True):
        previous_start = starts[-1][0]
        head = _preserved_head(entries, compaction.preserved_head_uuid, previous_start, boundary)
        if head is not None:
            starts.append((head, compaction, boundary, "preserved_head"))
        else:
            starts.append((boundary, compaction, boundary, "boundary"))
    ends = [start for start, *_ in starts[1:]] + [len(entries)]
    return [
        Segment(start=start, end=end, compaction=compaction, boundary_index=boundary, start_source=source)
        for (start, compaction, boundary, source), end in zip(starts, ends, strict=True)
    ]
