"""The console every table is printed on."""

from __future__ import annotations

from rich.console import Console

# Rich sizes output to the detected terminal and falls back to 80 columns when piped.
# Either is far too narrow for a row carrying a UUID, a session name and a worktree
# path: the cells wrap into stacks that are unreadable. Never render below this.
MIN_CONSOLE_WIDTH = 210


def build_console() -> Console:
    """A console that renders at the detected width, but never narrower than the minimum."""
    console = Console(highlight=False)
    if console.width >= MIN_CONSOLE_WIDTH:
        return console
    return Console(highlight=False, width=MIN_CONSOLE_WIDTH)
