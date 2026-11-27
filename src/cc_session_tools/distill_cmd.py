"""The ``cc-session-tools distill`` command: condense one session transcript into markdown.

Orchestration only — target resolution lives in ``discovery``, the condensed conversation
and its markdown in ``render``, and the size figures in ``stats``.

Stdout carries exactly one thing: the rendered markdown of the main file under ``--stdout``,
or the single path of the main file otherwise. Everything else — the stats table, every
error — goes to stderr, so a caller can consume stdout without filtering it.

A compacted session also gets one file per part before its last compaction, written in both
modes, so the pointer line at the top of the main markdown always names a file that exists.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import typer
from cc_blackbox import ReadDiagnostics
from rich.console import Console
from rich.table import Table
from rich.text import Text

from .discovery import projects_dirs_override, resolve_target
from .render import distill_entries, render_markdown
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
    if stats.pre_compact is not None:
        pre = stats.pre_compact
        table.add_row(
            Text("Pre-compact parts"),
            Text(f"{pre.part_count} ({human_bytes(pre.byte_size)}, ~{pre.estimated_tokens:,} tokens est.)"),
        )
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
        help="Directory to write <session-id>.md, and the pre-compact parts of a compacted session, into.",
    ),
    stdout: bool = typer.Option(
        False,
        "--stdout",
        help=(
            "Print the main markdown to stdout instead of writing it to a file. A compacted session's pre-compact"
            " parts are still written to the output dir."
        ),
    ),
) -> None:
    """Render a condensed markdown transcript of a session."""
    cwd = os.getcwd()
    transcript = resolve_target(target, cwd=cwd, projects_dirs=projects_dirs_override())
    if transcript is None:
        # Plain text, never rich markup: a target can contain brackets.
        typer.echo(f"Error: could not resolve session target: {target}", err=True)
        raise typer.Exit(code=1)

    diagnostics = ReadDiagnostics()
    try:
        entries = list(transcript.entries(diagnostics=diagnostics))
    except OSError as exc:
        typer.echo(f"Error: cannot read {transcript.path}: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    session = distill_entries(
        transcript.session_id, entries, diagnostics, include_thinking=include_thinking, session=transcript
    )
    # Absolute, so an agent can open the file a pointer line names without path arithmetic.
    output_dir = output_dir.resolve()
    main = session.main_part
    markdown = render_markdown(session, main, output_dir=output_dir)
    earlier = [(part, render_markdown(session, part, output_dir=output_dir)) for part in session.parts[:-1]]
    stats = compute_stats(main, markdown, [text for _, text in earlier])

    # The stats table is a human aid, so it goes to stderr in both modes: stdout is
    # either the markdown or the output path, and callers parse it as such.
    error_console = Console(stderr=True, highlight=False)

    if earlier or not stdout:
        output_dir.mkdir(parents=True, exist_ok=True)
    for part, text in earlier:
        (output_dir / session.part_file_name(part)).write_text(text, encoding="utf-8")

    if stdout:
        sys.stdout.write(markdown)
        if earlier:
            typer.echo(f"Pre-compact parts written to {output_dir}", err=True)
        error_console.print(_stats_table(stats))
        return

    out_path = output_dir / session.part_file_name(main)
    out_path.write_text(markdown, encoding="utf-8")
    typer.echo(str(out_path))
    error_console.print(_stats_table(stats))
