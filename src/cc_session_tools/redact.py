"""Text cleanup and short number formats for the ``timeline`` and ``tool-stats`` output.

Their output quotes prompts, subagent descriptions and commands, and is often pasted into tickets and chats. So
:func:`redact` writes the session's repo as ``<repo>``, the home dir as ``~``, a session scratchpad as ``<scratchpad>``
and any other file of Claude Code's per-session temp dir under ``<claude-tmp>``, a host name, in a URL or not, as
``<host>``, a cloud storage bucket as ``<bucket>``, long hex ids as ``<hex>``, and the value of anything that looks
like a credential as ``<redacted>``. Paths are also recognised in the dashed form Claude Code names its project dirs
with (``-home-user-projects-example``). It is a safety net for copy-paste, not a guarantee: text it does not recognise
passes unchanged.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from cc_blackbox import project_slug

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_CLAUDE_TMP = re.compile(
    r"[^\s\"'`=(:]*/claude-\d+(?![\w-])(?:/[^/\s\"'`]+(?:/" + _UUID + r")?)?(?P<scratchpad>/scratchpad(?![\w-]))?"
)
"""Claude Code's per-session temp dir, ``<tmp>/claude-<uid>/<project slug>/<session id>/``, which holds the
scratchpad and the output files of background tasks."""
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b((?:access[_-]?)?(?:token|secret|password|passwd|api[_-]?key|authorization|bearer)\b[\"']?\s*[=:]?\s*)"
    r"(?!<redacted>)[^\s\"',;]{6,}"
)
_TOKEN_SHAPED = re.compile(r"\b(?:sk-|ghp_|gho_|github_pat_|glpat-|xox[abprs]-|AKIA|ya29\.)[A-Za-z0-9_\-.]{8,}")
_URL_HOST = re.compile(r"(https?://)[^\s/'\"<>]+")
_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_WIDE_TLD = r"(?:com|net|org|io|cz|sk|de|eu|uk|us|co|dev|app|ai|cloud|internal|local|lan|corp|intra)"
_PUBLIC_TLD = r"(?:com|net|org|io|cz|eu|de|co|uk)"
_NAME_START = r"(?<![\w./-])"
_NAME_END = r"(?![\w.-]*[\w-])"
_HOSTS = (
    re.compile(rf"{_NAME_START}{_LABEL}\.{_PUBLIC_TLD}{_NAME_END}"),
    re.compile(rf"{_NAME_START}(?:{_LABEL}\.){{2,}}{_WIDE_TLD}{_NAME_END}"),
    re.compile(rf"{_NAME_START}(?:{_LABEL}\.)+{_WIDE_TLD}(?=:\d)"),
    re.compile(
        rf"(?:(?<=@)|(?<=//)|(?<=-h )|(?<=--host=)|(?<=--host )|(?<=ssh ))(?:{_LABEL}\.)+{_WIDE_TLD}{_NAME_END}"
    ),
)
"""Host names outside a URL: lowercase labels ending in a top-level domain. A two-label name such as ``example.com``
counts only with a common public domain, so file and module names such as ``compose.dev``, ``server.app`` or
``notes.ai`` stay. A longer name (``db1.example.internal``), or one after ``@``, ``//``, ``-h``, ``--host`` or ``ssh``
or before ``:<port>``, may also end in a private or newer domain. A name inside a path (after ``/``) stays."""
_BUCKET = re.compile(r"\b(gs|s3)://[^\s/'\"<>]+")
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{24,}\b")
_SPACES = re.compile(r"\s+")
_LEGACY_WORKTREE = re.compile(r"^(.+)-worktrees/[^/]+$")

_REPO_PATHS: ContextVar[tuple[str, ...]] = ContextVar("repo_paths", default=())


def session_paths(cwd: str | os.PathLike[str] | None, *, home: str | None = None) -> tuple[str, ...]:
    """The paths :func:`redact` writes as ``<repo>`` for a session launched in ``cwd``: the cwd itself and, for a git
    worktree under ``.worktrees/`` or a legacy ``<name>-worktrees/`` dir, its repo. Longest first. The home dir and
    its parents are left out, so they stay ``~``."""
    if not cwd:
        return ()
    home = (home if home is not None else str(Path.home())).rstrip("/")
    path = os.path.normpath(os.fspath(cwd))
    found = [path]
    if "/.worktrees/" in path:
        found.append(path.split("/.worktrees/")[0])
    if (legacy := _LEGACY_WORKTREE.match(path)) is not None:
        found.append(legacy.group(1))
    kept = [p for p in found if p != "/" and not (home + "/").startswith(p.rstrip("/") + "/")]
    return tuple(sorted(dict.fromkeys(kept), key=len, reverse=True))


@contextmanager
def redacting_session(cwd: str | os.PathLike[str] | None) -> Iterator[None]:
    """Within the block, :func:`redact` also writes ``cwd`` and its repo as ``<repo>``."""
    token = _REPO_PATHS.set(session_paths(cwd))
    try:
        yield
    finally:
        _REPO_PATHS.reset(token)


def _claude_tmp(match: re.Match[str]) -> str:
    return "<scratchpad>" if match.group("scratchpad") else "<claude-tmp>"


def _replace_path(text: str, path: str, placeholder: str) -> str:
    text = re.sub(re.escape(path) + r"(?![\w.-])", placeholder, text)
    slug = project_slug(path)
    return re.sub(r"(?<![\w-])" + re.escape(slug) + r"(?![A-Za-z0-9])", placeholder, text)


def redact(
    text: str, *, home: str | None = None, paths: tuple[str, ...] | None = None, limit: int | None = None
) -> str:
    """``text`` on one line, with private bits replaced, cut to ``limit`` characters with an ellipsis.

    ``paths`` are written as ``<repo>``; by default they are the session's, see :func:`redacting_session`.
    """
    home = (home if home is not None else str(Path.home())).rstrip("/")
    text = _SPACES.sub(" ", text).strip()
    text = _CLAUDE_TMP.sub(_claude_tmp, text)
    text = _TOKEN_SHAPED.sub("<redacted>", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1<redacted>", text)
    text = _URL_HOST.sub(r"\1<host>", text)
    text = _BUCKET.sub(r"\1://<bucket>", text)
    for host in _HOSTS:
        text = host.sub("<host>", text)
    text = _LONG_HEX.sub("<hex>", text)
    for path in paths if paths is not None else _REPO_PATHS.get():
        text = _replace_path(text, path, "<repo>")
    if home:
        text = _replace_path(text, home, "~")
    if limit is not None and len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def span(seconds: float) -> str:
    """A duration at the precision that matters at its size: ``45s``, ``5m55s``, ``26m``, ``1h21m``, ``1d14h``."""
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m{secs:02d}s" if minutes < 10 else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h{minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d{hours:02d}h"


def count(n: int, noun: str) -> str:
    """``1 turn``, ``3 turns``: a count with its noun, plural from 2 on and for 0."""
    return f"{n} {noun}" if n == 1 else f"{n} {noun}s"


def tokens(count: int) -> str:
    """``0``, ``812``, ``719k``, ``23.2M``."""
    if count >= 1_000_000:
        return f"{count / 1e6:.1f}M"
    return f"{count / 1e3:.0f}k" if count >= 1000 else str(count)
