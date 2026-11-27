"""How long a session was active, read cheaply enough to run over every session of the last weeks.

:func:`scan` gives the wall time, active time, kind and counts of subagents and compactions that
:func:`~cc_session_tools.session_time.analyze` gives, from the same rules: it builds the same steps for the main agent's
turns and the same bursts for the subagents. It is cheaper because it decodes only the records those rules read,
skipping the ``api-request`` records (about half of a transcript's bytes) before decoding and every attachment other
than a queued prompt before building it, and because it keeps no records, tool calls or tokens. On a large session it
takes about a sixth of the time of ``analyze`` and a fraction of its memory.

:func:`active_upper_bound` is the cheaper prefilter: the time from the first record to the last write of any of the
session's files, which no active time can exceed.
"""

from __future__ import annotations

import json
import math
import multiprocessing
import os
from collections import Counter
from collections.abc import Generator, Iterator, Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from cc_blackbox import Entry, SessionTranscript, SystemEntry
from cc_blackbox.transcripts.entries import parse_entry

from .session_time import active_of, activity_bursts, session_kind, sort_steps, step_of, turns_of, wall_of

_SKIPPED_TYPES = b'{"type":"api-request'
"""How the ``api-request``, ``api-request-blob`` and ``api-request-shape`` records start. Claude Code writes them with
the type first; a record that does not start this way is decoded and filtered by its type."""
_SYSTEM_SUBTYPES = frozenset({"turn_duration", "scheduled_task_fire", "compact_boundary"})
MAX_WORKERS = 16
"""The most processes :func:`scan_all` reads sessions in. Reading is bound by decoding JSON, one core per session."""


@dataclass(frozen=True)
class SessionActivity:
    session_id: str
    wall: float
    """Seconds from the first to the last user, assistant or subagent record, as ``timeline`` counts it."""
    active: float
    """Seconds of the union of the main agent's turns and the subagents' bursts. Like ``timeline``, it counts a tool
    call that stays pending for hours, such as an unanswered prompt, as active."""
    subagents: int
    compactions: int
    """The main agent's compactions."""
    kind: str
    kind_source: str


def active_upper_bound(transcript: SessionTranscript) -> float:
    """Seconds from the session's first record to the last write of its main or subagent transcripts, or infinity when
    the first record can't be found cheaply.

    Every turn and subagent burst lies within that span, so a session whose bound is below a threshold cannot have
    more active time. The bound must never undercount, because a session it rules out is never read:

    - The start is the earlier of cc-blackbox's first timestamp, from the head and tail of the main transcript, and
      the first timestamped record of the file read from its start. cc-blackbox drops a line longer than its head
      read, such as a prompt with a large paste, and can then take its first timestamp from the tail.
    - The end is the latest of cc-blackbox's last timestamp and the mtimes of the main and subagent transcripts. A long
      last line can be missing from cc-blackbox's tail read, and a background subagent can outlive the main
      transcript's last record.

    A subagent record written before the main transcript's first record, such as the few-second warmup subagents of
    older Claude Code versions, may stay outside the span; that error is seconds.
    """
    first = _first_timestamp(transcript.path)
    starts = [t for t in (transcript.started_at, first) if t is not None]
    if not starts:
        return math.inf
    start = min(starts).timestamp()
    last = transcript.last_record_at.timestamp() if transcript.last_record_at is not None else start
    for path in (transcript.path, *_subagent_files(transcript)):
        try:
            last = max(last, path.stat().st_mtime)
        except OSError:
            continue
    return max(0.0, last - start)


def scan(transcript: SessionTranscript) -> SessionActivity:
    """The activity of one session, from its main and subagent transcripts."""
    subagents = transcript.subagents()
    own_agents = {s.agent_id for s in subagents}
    compactions = 0
    steps = []
    for index, entry in enumerate(_entries(transcript.path)):
        if isinstance(entry, SystemEntry) and entry.subtype == "compact_boundary" and entry.timestamp is not None:
            compactions += 1
        step = step_of(index, entry, own_agents)
        if step is not None:
            steps.append(step)
    steps = sort_steps(steps)
    turns, _interruptions = turns_of(steps)
    bursts = [b for sub in subagents for b in activity_bursts(_entries(sub.path))]
    wall = wall_of(steps, turns, bursts)
    kind, kind_source = session_kind(
        [start.role for start in transcript.profile_starts()], Counter(t.trigger for t in turns)
    )
    return SessionActivity(
        session_id=transcript.session_id,
        wall=wall.seconds if wall is not None else 0.0,
        active=active_of(turns, bursts),
        subagents=len(subagents),
        compactions=compactions,
        kind=kind,
        kind_source=kind_source,
    )


def scan_all(transcripts: Sequence[SessionTranscript]) -> Generator[SessionActivity]:
    """:func:`scan` each of ``transcripts``, in parallel processes, yielding the results in the order given.

    A caller that stops early cancels the sessions not yet started, but still waits for the ones being read.
    """
    workers = min(len(transcripts), _cpus(), MAX_WORKERS)
    if workers <= 1:
        yield from (scan(t) for t in transcripts)
        return
    # Forked workers start with the parent's modules and working directory. Python 3.14 defaults to forkserver on
    # Linux, whose workers import the package again and change into the parent's working directory, which fails when
    # that directory no longer exists.
    start = "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"
    pool = ProcessPoolExecutor(max_workers=workers, mp_context=multiprocessing.get_context(start))
    try:
        yield from pool.map(scan, transcripts)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def _cpus() -> int:
    """The CPUs this process may run on."""
    if hasattr(os, "sched_getaffinity"):
        return len(os.sched_getaffinity(0)) or 1
    return os.cpu_count() or 1


def _first_timestamp(path: Path) -> datetime | None:
    """The timestamp of the first record that has one, reading the file from its start."""
    try:
        with open(path, "rb") as handle:
            for line in handle:
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                value = data.get("timestamp") if isinstance(data, dict) else None
                if not isinstance(value, str):
                    continue
                try:
                    moment = datetime.fromisoformat(value)
                except ValueError:
                    continue
                return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    except OSError:
        return None
    return None


def _subagent_files(transcript: SessionTranscript) -> Iterator[Path]:
    folder = transcript.sidecar_dir / "subagents"
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        if name.startswith("agent-") and name.endswith(".jsonl"):
            yield folder / name


def _entries(path: Path) -> Iterator[Entry]:
    """The records of a transcript that the turns and bursts read, in file order: user and assistant records, queued
    prompts, and the ``system`` records of turn ends, cron fires and compactions. A line that does not decode, such as
    a record still being written, is skipped."""
    try:
        handle = open(path, "rb")  # noqa: SIM115 - closed below; a missing file is no records
    except OSError:
        return
    with handle:
        for line in handle:
            if line.startswith(_SKIPPED_TYPES):
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            record_type = data.get("type")
            if record_type == "attachment":
                attachment = data.get("attachment")
                if not isinstance(attachment, dict) or attachment.get("type") != "queued_command":
                    continue
            elif record_type == "system":
                if data.get("subtype") not in _SYSTEM_SUBTYPES:
                    continue
            elif record_type not in ("user", "assistant"):
                continue
            entry = parse_entry(data)
            if entry is not None:
                yield entry
