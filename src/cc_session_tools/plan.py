"""The plan file of a session in plan mode, found through its transcript.

In plan mode the model writes its plan into a file, and ``ExitPlanMode`` shows that file in the plan approval dialog.
The file's path is random and depends on the ``plansDirectory`` setting, so it cannot be guessed. Claude Code records
it in the transcript: an ``attachment`` record of type ``plan_mode`` carries ``planFilePath`` when the session enters
plan mode, before the file exists, and one of type ``plan_mode_exit`` carries it again after an approval. The model
revises a rejected plan in the same file. The ``ExitPlanMode`` tool call holds the path too, but Claude Code writes it
only after the dialog is answered, so it cannot tell where the plan of an open dialog is.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cc_blackbox import AttachmentEntry, iter_entries

PLAN_ATTACHMENTS = frozenset({"plan_mode", "plan_mode_exit"})


@dataclass(frozen=True)
class PlanFile:
    path: Path
    slug: str
    """The record's ``slug``, which is the file's base name."""


def read_plan_file(transcript: Path) -> PlanFile | None:
    """The plan file of the newest ``plan_mode`` or ``plan_mode_exit`` attachment in ``transcript``, or ``None`` for a
    session that never entered plan mode or whose transcript cannot be read.

    Reads the whole transcript, decoding only the attachment records: about 0.3 seconds for 170 MB.
    """
    found: PlanFile | None = None
    try:
        for entry in iter_entries(transcript, types=("attachment",)):
            if not isinstance(entry, AttachmentEntry) or entry.attachment_type not in PLAN_ATTACHMENTS:
                continue
            path = entry.attachment.get("planFilePath")
            if not isinstance(path, str) or not path:
                continue
            slug = entry.raw.get("slug")
            found = PlanFile(Path(path), slug if isinstance(slug, str) and slug else Path(path).stem)
    except OSError:
        return None
    return found


def plan_json(plan: PlanFile | None) -> dict[str, Any] | None:
    """The ``plan`` object of ``inspect --json``: the file, and its content's hash and mtime when it exists."""
    if plan is None:
        return None
    try:
        content = plan.path.read_bytes()
        modified = plan.path.stat().st_mtime
    except OSError:
        return {"file": str(plan.path), "slug": plan.slug, "exists": False, "sha256": None, "modified_at": None}
    return {
        "file": str(plan.path),
        "slug": plan.slug,
        "exists": True,
        "sha256": hashlib.sha256(content).hexdigest(),
        "modified_at": datetime.fromtimestamp(modified, UTC).isoformat(),
    }
