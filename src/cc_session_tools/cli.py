"""The ``cc-session-tools`` entry point: a root Typer app that hosts the subcommands.

This module holds no session logic. It owns the application object, the ``--version``
flag and the top-level error guard that turns an unexpected exception into a plain
one-line message on stderr instead of a traceback.
"""

from __future__ import annotations

import sys

import typer

from . import __version__, distill_cmd, inspect_cmd, limits_cmd, list_active_cmd, search_cmd, timeline_cmd, watch_cmd

app = typer.Typer(
    name="cc-session-tools",
    help="Search and distill Claude Code sessions, list the running ones, inspect one, and watch them change.",
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode=None,
)


def version_callback(value: bool) -> None:
    if value:
        typer.echo(f"cc-session-tools {__version__}")
        raise typer.Exit()


@app.callback()
def root(
    version: bool = typer.Option(
        False,
        "--version",
        "-V",
        help="Show version and exit.",
        callback=version_callback,
        is_eager=True,
    ),
) -> None:
    """Search and distill Claude Code sessions, list the running ones, inspect one, and watch them change."""


app.command("search")(search_cmd.search)
app.command("distill")(distill_cmd.distill)
app.command("timeline")(timeline_cmd.timeline)
app.command("list-active", context_settings={"allow_extra_args": True})(list_active_cmd.list_active)
app.command("inspect")(inspect_cmd.inspect)
app.command("watch")(watch_cmd.watch)
app.command("limits")(limits_cmd.limits)


def main() -> None:
    try:
        app()
    except Exception as exc:
        # Top-level guard: report the failure as one plain line, never a traceback.
        # Errors stay markup-free because session titles may contain brackets.
        typer.echo(f"Error: {exc}", err=True)
        sys.exit(1)
