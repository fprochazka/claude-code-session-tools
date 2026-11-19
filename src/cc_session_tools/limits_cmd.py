"""The ``cc-session-tools limits`` command: the rate limits of every Claude Code profile's account.

Claude Code passes the rate limits only to its status line command. claude-code-auth-switch's launchers put a status
line in front of the user's that records them per profile, and cc-blackbox reads that file. A profile shows data only
after a session started through such a launcher got a response.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from typing import Any

import typer
from cc_blackbox import ClaudeProfile, ProfileNotFound, RateLimits, RateLimitWindow, list_profiles
from rich.table import Table
from rich.text import Text

from .active import resolve_profile_globs
from .console import build_console
from .timefmt import time_ago

# The windows in the order the table shows them, with their column headers.
WINDOW_COLUMNS = {"five_hour": "5h", "seven_day": "7d", "spend_limit": "Spend"}
# Under the first mark a window is green, under the second yellow, and red from there, as in a common status line.
WARN_PERCENT = 65
CRITICAL_PERCENT = 85


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def window_json(window: RateLimitWindow, now: datetime) -> dict[str, Any]:
    """One window of ``--json``. ``reset`` is computed against ``now``, the moment the command runs."""
    return {
        "used_percentage": window.used_percent,
        "resets_at": _iso(window.resets_at),
        "reset": window.is_reset(now),
        "measured_at": _iso(window.measured_at),
        "changed_at": _iso(window.changed_at),
        "session_id": window.session_id,
        "raw": dict(window.raw),
    }


def limits_json(profile: ClaudeProfile, limits: RateLimits | None, now: datetime) -> dict[str, Any]:
    """One profile of ``--json``. Every key but the profile's own is ``null`` without data."""
    return {
        "profile": profile.name,
        "config_dir": str(profile.config_dir),
        "path": str(limits.path) if limits is not None else None,
        "measured_at": _iso(limits.measured_at) if limits is not None else None,
        "written_at": _iso(limits.written_at) if limits is not None else None,
        "session_id": limits.session_id if limits is not None else None,
        "model": limits.model if limits is not None else None,
        "claude_version": limits.claude_version if limits is not None else None,
        "windows": {name: window_json(window, now) for name, window in limits.windows.items()} if limits else None,
    }


def duration_text(seconds: float) -> str:
    """A short duration such as ``2d4h``, ``1h12m`` or ``40m``."""
    minutes = max(0, round(seconds / 60))
    days, hours, minutes = minutes // 1440, minutes // 60 % 24, minutes % 60
    if days:
        return f"{days}d{hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h{minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m"


def clock_text(when: datetime, now: datetime) -> str:
    """The local time of ``when``, with the weekday when it is not within the next day."""
    local = when.astimezone()
    if abs((when - now).total_seconds()) < 86400:
        return local.strftime("%H:%M")
    return local.strftime("%a %H:%M")


def window_text(window: RateLimitWindow | None, now: datetime) -> Text:
    if window is None:
        return Text("–", style="dim")
    percent = f"{window.used_percent:.0f}%"
    if window.is_reset(now):
        when = f" {clock_text(window.resets_at, now)}" if window.resets_at is not None else ""
        return Text(f"reset{when} · was {percent}", style="dim")
    style = (
        "green" if window.used_percent < WARN_PERCENT else "yellow" if window.used_percent < CRITICAL_PERCENT else "red"
    )
    text = Text(percent, style=style)
    if window.resets_at is not None:
        remaining = (window.resets_at - now).total_seconds()
        text.append(f" · resets in {duration_text(remaining)} ({clock_text(window.resets_at, now)})")
    usd, limit = window.raw.get("used_usd"), window.raw.get("limit_usd")
    if isinstance(usd, int | float) and isinstance(limit, int | float):
        text.append(f" · ${usd:,.2f} of ${limit:,.2f}")
    return text


def _print_table(rows: list[tuple[ClaudeProfile, RateLimits | None]], now: datetime) -> None:
    present = {name for _, limits in rows if limits is not None for name in limits.windows}
    # The subscription windows always have a column, the spend limit only when a profile has one.
    columns = [name for name in WINDOW_COLUMNS if name in present or name != "spend_limit"]
    columns += sorted(present - set(WINDOW_COLUMNS))
    table = Table(show_lines=False, box=None, pad_edge=False)
    table.add_column("Profile", no_wrap=True)
    for name in columns:
        table.add_column(WINDOW_COLUMNS.get(name, name), no_wrap=True)
    table.add_column("Measured", no_wrap=True)
    for profile, limits in rows:
        if limits is None:
            table.add_row(Text(profile.name), Text("no data", style="dim"), *[Text("") for _ in columns[1:]], Text(""))
            continue
        measured = Text(time_ago(limits.measured_at, now=now)) if limits.measured_at is not None else Text("-")
        cells = [window_text(limits.windows.get(name), now) for name in columns]
        table.add_row(Text(profile.name), *cells, measured)
    build_console().print(table)


def _profiles(globs: list[str] | None) -> list[ClaudeProfile]:
    profiles = list_profiles()
    if not globs:
        return profiles
    try:
        names = set(resolve_profile_globs(globs, profiles))
    except ProfileNotFound as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    return [p for p in profiles if p.name in names]


def limits(
    profile: list[str] = typer.Option(
        None,
        "--profile",
        "-p",
        help="Only the profiles whose name matches this glob, for example 'work*'. Repeatable.",
        show_default=False,
    ),
    as_json: bool = typer.Option(False, "--json", help="Print every profile's limits as a JSON array."),
) -> None:
    """Show the rate limits of every profile's account: the 5-hour and weekly windows, and a gateway's spend limit.

    claude-code-auth-switch's launchers record what Claude Code passes to the status line, so a profile has data once
    a session started through a launcher got a response. The values are the last ones a session reported, as old as
    Measured says, and stay valid until their window resets; usage on other machines or claude.ai shows with the next
    response here. A window whose reset time has passed shows as reset, with the usage from before the reset.
    """
    now = datetime.now(UTC)
    rows = [(p, p.rate_limits()) for p in _profiles(profile)]
    if as_json:
        sys.stdout.write(json.dumps([limits_json(p, lim, now) for p, lim in rows], indent=2, ensure_ascii=False) + "\n")
        return
    if not rows:
        typer.echo("No Claude Code profiles found", err=True)
        return
    _print_table(rows, now)
