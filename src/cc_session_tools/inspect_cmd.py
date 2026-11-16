"""The ``cc-session-tools inspect`` command: the full current picture of one session, running or ended.

``inspection`` finds the session and builds its fields. This module prints them, as a field table or as JSON.
"""

from __future__ import annotations

import json
import sys

import typer
from rich.table import Table
from rich.text import Text

from .active import SessionLookupError
from .console import build_console
from .inspection import DEFAULT_DAYS, find, to_inspect_json


def _value(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, dict):
        return ", ".join(f"{k}={'-' if v is None else v}" for k, v in value.items())
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "-"
    return str(value)


def _start(start: dict[str, object]) -> str:
    return f"{start['ts']}  pid {start['pid'] or '-'}  {start['source'] or '-'}  {start['profile'] or '-'}"


def _print_details(data: dict[str, object]) -> None:
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    for key, value in data.items():
        if key == "raw":
            continue
        if key == "profile_starts" and isinstance(value, list) and value:
            for index, start in enumerate(value):
                table.add_row(Text(key if index == 0 else ""), Text(_start(start)))
            continue
        table.add_row(Text(key), Text(_value(value)))
    build_console().print(table)


def inspect(
    session: str = typer.Argument(
        ...,
        help="A running session by session id, id prefix, name, <profile>/<name>, <profile>/pid:<pid> or pid; else an "
        "ended session by session id or id prefix, by its name or any earlier title, or by its pid from the "
        "auth-switch start log.",
        show_default=False,
        metavar="SESSION",
    ),
    days: int = typer.Option(
        DEFAULT_DAYS,
        "--days",
        min=1,
        help="How far back to look for an ended session by name, title or pid: transcripts active in the last this "
        "many days. A session id or id prefix is looked up in every transcript.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print every field as one JSON object."),
) -> None:
    """Show every field of one Claude Code session, running or ended.

    A running session shows what list-active knows about it, with state running. An ended session shows what its
    transcript and the auth-switch start log recorded, with state ended: the profile that ran it last, its last name,
    title and cwd, when it started and was last active, every start, and the command that resumes it.
    """
    try:
        found = find(session, days=days)
    except SessionLookupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    data = to_inspect_json(found)
    if as_json:
        sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    else:
        _print_details(data)
