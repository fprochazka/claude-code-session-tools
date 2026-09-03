"""Forward path→encoded-dir math and projects-dir resolution.

Claude Code stores transcripts at ``~/.claude/projects/<encoded-dir>/<uuid>.jsonl``.
The encoding is lossy (``/``, ``.`` and ``-`` all collapse to ``-``), so it can only
be computed *forward* from a known absolute path — never reversed. Everything here is
deterministic string math; nothing touches the filesystem or requires the path to exist.
"""

from __future__ import annotations

import os
from pathlib import Path


def encode_path(abs_path: str | Path) -> str:
    """Forward-encode an absolute path into its ``~/.claude/projects/`` dir name.

    Replaces every ``/`` and every ``.`` in the absolute path string with ``-``. This is
    lossy and irreversible (a ``/.`` sequence, e.g. the ``.worktrees`` dir, becomes ``--``)
    but forward-deterministic, which is all we need: we always encode from a known path.

    The input is made absolute and lexically normalized first (trailing slash stripped,
    ``//`` collapsed, ``..``/``.`` components resolved) *without* following symlinks, so
    the encoding matches the literal path git and Claude Code record.
    """
    absolute = os.path.abspath(os.fspath(abs_path))
    return absolute.replace("/", "-").replace(".", "-")


def resolve_projects_dir() -> Path:
    """Return the Claude Code projects dir, honoring ``CLAUDE_PROJECTS_DIR``.

    Falls back to ``~/.claude/projects``. The directory is not required to exist.
    """
    override = os.environ.get("CLAUDE_PROJECTS_DIR")
    if override:
        return Path(override).expanduser()
    return Path("~/.claude/projects").expanduser()
