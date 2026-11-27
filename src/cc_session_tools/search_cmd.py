"""The ``cc-session-tools search`` command: list (and optionally grep) the Claude Code
sessions that belong to the current directory and its git worktrees, newest-first.

This module is a thin orchestration layer: scope and rows come from ``discovery``, content
search from ``search``, relative times from ``timefmt``. It narrows the rows by content when
asked, applies the limit, and renders a ``rich`` table.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import typer
from rich.table import Table
from rich.text import Text

from .console import build_console
from .discovery import SessionRow, build_session_rows, projects_dirs_override, resolve_scope
from .search import RipgrepError, search_session_ids
from .timefmt import time_ago
from .watch_events import parse_since


def search(
    limit: int = typer.Option(
        20,
        "-n",
        "--limit",
        help="Maximum number of sessions to show (newest first). A value <= 0 means no limit.",
    ),
    rg: list[str] = typer.Option(
        None,
        "--rg",
        help="Only show sessions whose transcript contains this literal text (case-insensitive; uses ripgrep when "
        "installed). "
        "Repeatable: every keyword must match somewhere in the session.",
    ),
    rg_any: bool = typer.Option(
        False,
        "--rg-any",
        help="Match sessions containing ANY of the --rg keywords instead of all of them. "
        "Only meaningful alongside --rg.",
    ),
    all_projects: bool = typer.Option(
        False,
        "--all",
        help="List sessions from every project instead of the current directory and its git worktrees.",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Only sessions whose last interaction is this recent: a duration such as 2h, 3d or 2w, or an ISO date "
        "such as 2026-11-01.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print the rows as a JSON array instead of a table."),
) -> None:
    """List Claude Code sessions for the current directory, newest interaction first."""
    cwd = os.getcwd()

    # An absent repeatable option reaches us as None or as an empty list depending on how the command is invoked;
    # both mean "no content search", and neither may trigger a search.
    keywords = [kw for kw in (rg or []) if kw.strip()]
    try:
        cutoff = parse_since(since) if since is not None else None
    except ValueError as e:
        typer.echo(f"Error: --since: {e}", err=True)
        raise typer.Exit(code=1) from e

    scope = resolve_scope(cwd, all_projects=all_projects)
    rows = build_session_rows(scope, projects_dirs=projects_dirs_override())
    if cutoff is not None:
        rows = [row for row in rows if row.last_ts >= cutoff]

    if keywords:
        try:
            matching_ids = search_session_ids(keywords, [row.transcript for row in rows], match_any=rg_any)
        except RipgrepError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2) from exc
        rows = [row for row in rows if row.session_id in matching_ids]

    if limit > 0:
        rows = rows[:limit]

    if as_json:
        sys.stdout.write(json.dumps([_row_json(row) for row in rows], indent=2, ensure_ascii=False) + "\n")

    console = build_console()

    if not rows:
        where = "any project" if all_projects else cwd
        if keywords:
            matched = ", ".join(repr(kw) for kw in keywords)
            # Name the semantics that actually ran, so a fruitless AND is distinguishable
            # from a fruitless OR without re-reading the invocation.
            how = "matching any of" if rg_any else "matching"
            message = f"No sessions found for {where} {how} {matched}"
        else:
            message = f"No sessions found for {where}"
        if cutoff is not None:
            message += f" since {cutoff.astimezone():%Y-%m-%d %H:%M}"
        # Plain stderr, never a rich console: keywords and paths may contain brackets
        # that rich would swallow as markup, and stdout stays parseable when piped.
        typer.echo(message, err=True)
        return
    if as_json:
        return

    # Expand to the full console width so the table always occupies at least
    # MIN_CONSOLE_WIDTH; the slack goes to the flexible Name column, which is the
    # one that actually needs the room (titles run to ~160 chars).
    # Borderless (``box=None``, ``pad_edge=False``) keeps the aligned columns and the
    # header row without box-drawing art, and starts the first column flush left.
    table = Table(show_lines=False, expand=True, box=None, pad_edge=False)
    table.add_column("When", no_wrap=True)
    table.add_column("Session", no_wrap=True)
    table.add_column("Name")
    # The last column only carries signal inside a git repo (worktrees, live or
    # deleted, produce distinct relative paths); outside one every cell is blank, so
    # the column is dropped to reduce noise. Under ``--all`` the same slot carries the
    # session's own cwd, which is the only thing that tells the rows apart.
    show_location = all_projects or scope.in_git_repo
    # Profiles often share one projects dir; the column appears only when the rows come from more than one.
    show_profile = len({row.transcript.projects_dir.path for row in rows}) > 1
    if show_profile:
        table.add_column("Profile", no_wrap=True)
    if show_location:
        table.add_column("Project" if all_projects else "Worktree", no_wrap=True)

    for row in rows:
        # A derived (non-genuine) name renders dimmed per D2. Building a Text with an
        # explicit style — rather than embedding ``[dim]`` markup — keeps titles that
        # contain literal brackets from being reinterpreted as rich markup.
        name_cell = Text(row.name, style="dim") if row.name_is_derived else Text(row.name)
        cells = [Text(time_ago(row.last_ts)), Text(row.session_id), name_cell]
        if show_profile:
            cells.append(Text(row.profile))
        if show_location:
            cells.append(Text(row.worktree_rel))
        table.add_row(*cells)

    console.print(table)


def _row_json(row: SessionRow) -> dict[str, Any]:
    cwd = row.transcript.cwd
    return {
        "session_id": row.session_id,
        "name": row.name,
        "name_is_derived": row.name_is_derived,
        "last_interaction": row.last_ts.isoformat(),
        "last_interaction_from_transcript": row.last_ts_from_transcript,
        "profile": row.profile,
        "location": row.worktree_rel,
        "cwd": str(cwd) if cwd is not None else None,
        "transcript_path": str(row.path),
    }
