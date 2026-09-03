"""The ``cc-session-tools distill`` command: condense one session transcript into markdown.

Orchestration only — target resolution lives in ``discovery``, classification in
``parse``, markdown in ``render`` and the size figures in ``stats``.

Stdout carries exactly one thing: the rendered markdown under ``--stdout``, or the
single path of the written file otherwise. Everything else — the stats table, every
error — goes to stderr, so a caller can consume stdout without filtering it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .discovery import resolve_target
from .parse import parse_session
from .paths import resolve_projects_dir
from .render import render_markdown
from .stats import TranscriptStats, compute_stats, human_bytes

DEFAULT_OUTPUT_DIR = Path("/tmp/claude-code-session-tools")


def _stats_table(stats: TranscriptStats) -> Table:
    # Cells are wrapped in ``Text`` and highlighting is off on the console, so nothing
    # in a rendered value can be reinterpreted as rich markup.
    table = Table(show_header=False, title="Transcript stats", title_justify="left")
    table.add_column("Metric", style="cyan", no_wrap=True)
    table.add_column("Value")
    table.add_row(Text("User prompts"), Text(str(stats.prompt_count)))
    table.add_row(Text("Subagent calls"), Text(str(stats.subagent_count)))
    table.add_row(Text("Doc size"), Text(f"{human_bytes(stats.byte_size)} ({stats.char_count:,} chars)"))
    table.add_row(Text("Tokens"), Text(f"~{stats.estimated_tokens:,} (est.)"))
    return table


def distill(
    target: str = typer.Argument(
        ...,
        help="A .jsonl path, a session UUID, or the literal 'latest'.",
    ),
    include_thinking: bool = typer.Option(
        False,
        "--include-thinking",
        help="Include assistant thinking blocks.",
    ),
    output_dir: Path = typer.Option(
        DEFAULT_OUTPUT_DIR,
        "--output-dir",
        help="Directory to write <session-id>.md into.",
    ),
    stdout: bool = typer.Option(
        False,
        "--stdout",
        help="Print markdown to stdout instead of writing a file.",
    ),
) -> None:
    """Render a condensed markdown transcript of a session."""
    cwd = os.getcwd()
    path = resolve_target(target, cwd=cwd, projects_dir=resolve_projects_dir())
    if path is None:
        # Plain text, never rich markup: a target can contain brackets.
        typer.echo(f"Error: could not resolve session target: {target}", err=True)
        raise typer.Exit(code=1)

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        typer.echo(f"Error: cannot read {path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    session = parse_session(text, session_id=path.stem, include_thinking=include_thinking)
    markdown = render_markdown(session)
    stats = compute_stats(session, markdown)

    # The stats table is a human aid, so it goes to stderr in both modes: stdout is
    # either the markdown or the output path, and callers parse it as such.
    error_console = Console(stderr=True, highlight=False)

    if stdout:
        sys.stdout.write(markdown)
        error_console.print(_stats_table(stats))
        return

    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = (output_dir / f"{session.session_id}.md").resolve()
    out_path.write_text(markdown, encoding="utf-8")
    typer.echo(str(out_path))
    error_console.print(_stats_table(stats))
