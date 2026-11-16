"""The ``cc-session-tools list-active`` command: the Claude Code sessions running on this machine, in every profile.

It prints one table row per session, or with ``--json`` an array of every field. ``inspect`` shows one session.
Sessions come from cc-blackbox; ``active`` resolves the live tmux pane and the git branch.
"""

from __future__ import annotations

import enum
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from typing import get_args

import typer
from cc_blackbox import ActiveSession, ProfileNotFound, SessionStatus, list_active_sessions
from rich.table import Table
from rich.text import Text

from .active import (
    Filters,
    SessionView,
    active_since,
    activity_of,
    apply_filters,
    build_views,
    resolve_profile_globs,
    shorten_home,
    sort_sessions,
    to_json,
    waiting_for,
)
from .console import build_console
from .discovery import resolve_scope
from .timefmt import time_ago
from .watch_events import parse_duration

Status = enum.Enum("Status", {value: value for value in get_args(SessionStatus)}, type=str)
"""The status values Claude Code writes into a registry record, as cc-blackbox knows them."""


def _sessions(profile_globs: list[str] | None) -> list[ActiveSession]:
    try:
        return list_active_sessions(resolve_profile_globs(profile_globs) if profile_globs else None)
    except ProfileNotFound as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _status(session: ActiveSession) -> str:
    reason = waiting_for(session)
    return f"{session.status}: {reason}" if reason is not None else session.status or "-"


def _active(session: ActiveSession) -> Text:
    activity = activity_of(session)
    if activity.at is None:
        return Text("-", style="dim")
    if activity.started_only:
        return Text(f"started {time_ago(activity.at)}", style="dim")
    return Text(time_ago(activity.at))


def _where(view: SessionView) -> Text:
    if view.git is not None and view.git.branch is not None:
        return Text(view.git.branch)
    cwd = view.session.cwd
    return Text(shorten_home(cwd) if cwd is not None else "-", style="dim")


# Long tmux session names are shortened in the middle, so the window index after them stays visible.
MAX_TMUX_SESSION_WIDTH = 48


def _middle_ellipsis(text: str, width: int) -> str:
    if len(text) <= width:
        return text
    head = (width - 1) // 2
    return text[:head] + "…" + text[len(text) - (width - 1 - head) :]


def _pane(view: SessionView) -> Text:
    if view.tmux_pane is None:
        return Text("")
    if view.tmux_live is not None:
        live = view.tmux_live
        return Text(f"{view.tmux_pane} {_middle_ellipsis(live.session, MAX_TMUX_SESSION_WIDTH)}:{live.window_index}")
    label = "gone" if view.tmux_state == "gone" else "tmux unknown"
    return Text(f"{view.tmux_pane} ({label})", style="dim")


def _print_table(views: list[SessionView]) -> None:
    rows = []
    for view in views:
        s = view.session
        rows.append(
            (
                Text(s.profile.name),
                Text(s.name) if s.name else Text(f"pid:{s.pid}", style="dim"),
                Text(_status(s)),
                _active(s),
                _where(view),
                _pane(view),
            )
        )
    # Borderless like the search table, but not expanded: the columns are short and stay next to each other. When a
    # row is wider than the console, rich shrinks every column; the first four are pinned to their content, so only
    # the branch and the tmux session lose their tail. The pane id leads its cell and survives.
    table = Table(show_lines=False, box=None, pad_edge=False)
    for index, header in enumerate(("Profile", "Name", "Status", "Active")):
        width = max([len(header)] + [row[index].cell_len for row in rows])
        table.add_column(header, no_wrap=True, min_width=width)
    table.add_column("Branch or cwd", no_wrap=True, overflow="ellipsis")
    table.add_column("Tmux pane", no_wrap=True, overflow="ellipsis")
    for row in rows:
        table.add_row(*row)
    build_console().print(table)


def list_active(
    ctx: typer.Context,
    profile: list[str] = typer.Option(
        None,
        "--profile",
        "-p",
        help="Only sessions of the profiles whose name matches this glob, for example 'rohlik*'. Repeatable.",
        show_default=False,
    ),
    status: list[Status] = typer.Option(None, "--status", help="Only sessions with this status. Repeatable."),
    here: bool = typer.Option(
        False,
        "--here",
        help="Only sessions whose cwd is in the current git repo or any of its worktrees; outside a repo, the exact "
        "current directory.",
    ),
    tmux: bool = typer.Option(
        None, "--tmux/--no-tmux", help="Only sessions that run in tmux, or only those that do not.", show_default=False
    ),
    name: str = typer.Option(
        None, "--name", help="Only sessions whose name matches this glob, case-insensitively.", show_default=False
    ),
    active_within: str = typer.Option(
        None,
        "--active-within",
        metavar="<duration>",
        help="Only sessions that got a model response within this long, for example 30m, 2h or 1h30m. A session "
        "without a response yet counts from its start.",
        show_default=False,
    ),
    as_json: bool = typer.Option(False, "--json", help="Print every field of every session as a JSON array."),
) -> None:
    """List the running Claude Code sessions of every profile.

    One row per session, by profile and then start time. Active is the time since the session's last model response, in
    its transcript or a subagent's; a session without one shows its start, dimmed. The tmux pane is the pane id with the
    tmux session and window that pane is in now, read from the tmux server. The inspect command shows one session.
    """
    if ctx.args:
        # The single-session mode moved to inspect; say where instead of failing with a bare usage error.
        session = " ".join(ctx.args)
        typer.echo(
            f"Error: list-active only lists sessions. To show one session, run: cc-session-tools inspect {session}",
            err=True,
        )
        raise typer.Exit(code=2)
    since: datetime | None = None
    if active_within is not None:
        try:
            seconds = parse_duration(active_within)
        except ValueError as exc:
            typer.echo(f"Error: --active-within: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        if seconds <= 0:
            typer.echo("Error: --active-within: the duration must be longer than zero", err=True)
            raise typer.Exit(code=2)
        since = datetime.now(UTC) - timedelta(seconds=seconds)
    filters = Filters(
        statuses=frozenset(s.value for s in status or []),
        scope=resolve_scope(os.getcwd()) if here else None,
        in_tmux=tmux,
        name_glob=name,
    )
    sessions = sort_sessions(apply_filters(_sessions(profile), filters))
    if since is not None:
        # After the registry filters: only this one reads transcripts.
        sessions = active_since(sessions, since)

    views = build_views(sessions)
    if as_json:
        array = [to_json(view) for view in views]
        sys.stdout.write(json.dumps(array, indent=2, ensure_ascii=False) + "\n")
        return
    if not views:
        typer.echo("No active sessions found", err=True)
        return
    _print_table(views)
