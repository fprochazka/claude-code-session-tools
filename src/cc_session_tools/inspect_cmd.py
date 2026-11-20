"""The ``cc-session-tools inspect`` command: the full current picture of one session, running or ended.

``inspection`` finds the session and builds its fields. This module prints them, as a field table or as JSON.
"""

from __future__ import annotations

import json
import sys
from typing import Any

import typer
from rich.table import Table
from rich.text import Text

from .active import SessionLookupError, context_window_text
from .console import build_console
from .inspection import DEFAULT_DAYS, find, to_inspect_json
from .watch_events import format_duration


def _value(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, dict):
        return ", ".join(f"{k}={'-' if v is None else v}" for k, v in value.items())
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "-"
    return str(value)


def _start(start: dict[str, object]) -> str:
    line = f"{start['ts']}  pid {start['pid'] or '-'}  {start['source'] or '-'}  {start['profile'] or '-'}"
    if start["role"] is not None:
        line += f"  role {start['role']}"
        if start["role_prompt_mode"] is not None:
            line += f" (prompt {start['role_prompt_mode']})"
    return line


def _usd(value: object) -> str:
    if not isinstance(value, float):
        return "$?"
    return f"${value:.2f}" if value >= 0.01 or value == 0 else f"${value:.4f}"


def _count(value: object) -> str:
    return f"{value:,}" if isinstance(value, int) else "-"


def _cost_lines(cost: dict[str, Any] | None, *, running: bool) -> list[str]:
    """The cost as ``inspect`` prints it: the totals, then one line per model.

    Claude Code records the cost when a process of the session exits, so for a running session it leaves out the
    current run. The first line says which totals these are, so nobody reads them as the current cost.
    """
    if cost is None:
        return ["- (recorded when the session exits; it has not exited yet)" if running else "-"]
    parts = [_usd(cost["total_cost_usd"])]
    if cost["has_unknown_model_cost"]:
        parts[0] += " (too low: a model without a known price was used)"
    durations = (("run", "duration_seconds"), ("API", "api_duration_seconds"), ("tools", "tool_duration_seconds"))
    for label, key in durations:
        if cost[key] is not None:
            parts.append(f"{format_duration(cost[key])} {label}")
    if cost["lines_added"] is not None or cost["lines_removed"] is not None:
        parts.append(f"+{_count(cost['lines_added'])} -{_count(cost['lines_removed'])} lines")
    when = "at its last exit before this run, which is not included" if running else "at its last exit"
    lines = [f"{', '.join(parts)}; recorded {when}"]
    for model, usage in cost["model_usage"].items():
        lines.append(
            f"{model}  {_usd(usage['cost_usd'])}  input {_count(usage['input_tokens'])}  "
            f"output {_count(usage['output_tokens'])}  cache read {_count(usage['cache_read_input_tokens'])}  "
            f"cache write {_count(usage['cache_creation_input_tokens'])}"
        )
    return lines


def _screen(screen: dict[str, Any]) -> str:
    summary = str(screen["state"])
    if screen["title"]:
        summary += f": {screen['title']}"
    if screen["reason"]:
        summary += f" ({screen['reason'].replace('_', ' ')})"
    return summary


def _print_details(data: dict[str, object]) -> None:
    table = Table(show_header=False, box=None, pad_edge=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")
    for key, value in data.items():
        if key == "raw":
            continue
        if key == "cost":
            lines = _cost_lines(value, running=data["state"] == "running")  # type: ignore[arg-type]
            for index, line in enumerate(lines):
                table.add_row(Text(key if index == 0 else ""), Text(line))
            continue
        if key == "context_window" and isinstance(value, dict):
            text = context_window_text(value)
            text.append(f"  {value['model'] or 'unknown model'}, size from {value['size_source']}", style="dim")
            table.add_row(Text(key), text)
            continue
        if key == "screen" and isinstance(value, dict):
            table.add_row(Text(key), Text(_screen(value)))
            continue
        if key == "profile_starts" and isinstance(value, list) and value:
            for index, start in enumerate(value):
                table.add_row(Text(key if index == 0 else ""), Text(_start(start)))
            continue
        table.add_row(Text(key), Text(_value(value)))
    console = build_console()
    console.print(table)
    screen = data.get("screen")
    if isinstance(screen, dict) and screen["lines"] is not None:
        console.print()
        console.print(Text(f"Last {len(screen['lines'])} lines of {screen['pane']}:", style="cyan"))
        for line in screen["lines"]:
            console.print(Text(line), no_wrap=True, overflow="crop", soft_wrap=False)


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
    screen: bool = typer.Option(
        False,
        "--screen",
        help="Also capture the session's tmux pane and say what it shows: prompt_empty, prompt_has_input, dialog, "
        "permission_prompt, plan_approval, busy or unknown, with the choices of a dialog and the last lines. Typing "
        "into the pane is safe only at prompt_empty.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Print every field as one JSON object."),
) -> None:
    """Show every field of one Claude Code session, running or ended.

    A running session shows what list-active knows about it, with state running. An ended session shows what its
    transcript and the auth-switch start log recorded, with state ended: the profile that ran it last, its last name,
    title and cwd, when it started and last got a model response, every start, and the command that resumes it. Both
    show the cost Claude Code recorded when the session last exited; for a running session it leaves out the current
    run.

    --screen reads the pane the way it looks now. The classification matches Claude Code's screen layout and answers
    unknown whenever it is not certain, so treat anything but prompt_empty as unsafe to type into.
    """
    try:
        found = find(session, days=days)
    except SessionLookupError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    data = to_inspect_json(found, screen=screen)
    if as_json:
        sys.stdout.write(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    else:
        _print_details(data)
