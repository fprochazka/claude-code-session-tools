"""What a Claude Code pane shows, classified from its ``tmux capture-pane -p -e`` text.

A coordinating session types into other sessions' panes through tmux: ``/compact``, a brief as ``@<file>``, the answer
to a startup dialog. Typing is safe only into an empty prompt, and the registry record cannot tell: startup dialogs come
before the session has a usable record, and text a human has half typed is in no record at all. So this module reads
the screen.

It is a heuristic over Claude Code's TUI text, box drawing and colors, matched against captures of Claude Code 2.1.325,
and it breaks when the UI changes. Every rule therefore checks for what it has seen and answers ``unknown`` for
anything else: a wrong ``prompt_empty`` makes a coordinator type into a dialog or over someone's half-typed text, while
``unknown`` only makes it wait.

The input must be captured with ``-e``. The dimmed placeholder (``Try "…"``) and suggestion read the same as typed text
without attributes, and the prompt glyph turns grey while the session works, also when no spinner line is visible. A
capture without escape sequences never yields a prompt state.

An empty prompt also needs the cursor. A capture drops trailing spaces, so typed spaces look like an empty prompt, but
they move the cursor. ``prompt_empty`` therefore requires nothing in the box but dimmed text on the glyph line, and the
cursor right after the glyph. Typed text, a ``[Pasted text #1 +40 lines]`` marker and an ``[Image #1]`` marker are
drawn without dim, and read as ``prompt_has_input``. Anything else in the box is ``unknown``.

Known gaps:

- Busy detection without a spinner relies on the grey prompt glyph of the dark theme (256-color 241 in the captures).
  Other greys and dim count as busy too; any other glyph color gives ``unknown``.
- There are no captures of the light themes.
- Vim mode's NORMAL mode reads as ``prompt_empty``, although it takes typed keys as commands. Its screen differs from
  INSERT mode only in the footer, which loses ``-- INSERT --`` and then looks like the footer of a session without vim
  mode, and tmux 3.4 reports no cursor shape. Whether a session has vim mode on is in its settings, not on its screen.
- Only a Bash permission prompt was captured. Edit and WebFetch permission prompts should match the same ``Do you want
  to …?`` rule, untested.
- The plan approval dialog clips a plan too long for the pane and keeps its title on screen, with a ``↓`` at the end of
  the title line when the plan scrolls. Only in a pane of about 12 lines or fewer does the title go, and the choices
  with it but the selected one; the screen then reads ``unknown``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

ScreenState = Literal[
    "prompt_empty", "prompt_has_input", "dialog", "permission_prompt", "plan_approval", "busy", "unknown"
]

SCREEN_STATES: tuple[ScreenState, ...] = (
    "prompt_empty",
    "prompt_has_input",
    "dialog",
    "permission_prompt",
    "plan_approval",
    "busy",
    "unknown",
)

PLAN_APPROVAL_TITLE = "Ready to code?"

_PROMPT_GLYPH = "❯"
_NBSP = " "
_RULE = "─"
_DIALOG_BORDERS = ("─", "▔")
_MIN_RULE = 10
_EMPTY_CURSOR_COLUMN = 2
"""Where the cursor sits in an empty prompt: right after the glyph and its space."""
_MAX_INPUT_LINES = 80
"""A prompt box taller than this is not one the classifier recognises."""
_MAX_FOOTER_LINES = 6
"""Non-empty lines under the prompt box: the status line and the permission mode line, with room to spare."""
_SPINNER_LOOKBACK = 4
"""Non-empty lines above the prompt box searched for the spinner; a tip line can sit between them."""
_FOOTER_LOOKBACK = 3
"""A dialog's ``Esc to cancel`` footer is among this many last non-empty lines; a closing rule can follow it."""

# ``✻ Hallucinating… (7s · ↓ 663 tokens · thought for 2s)``. A finished turn keeps the glyph but reads
# ``✻ Cooked for 10s · done 12:24``, with no ellipsis and no running time, so it does not match.
_SPINNER = re.compile(r"^[·✢✳✶✻✽*] \S[^…]*… \((\d+[smh]\b|.*esc to interrupt)")
_DIALOG_SELECTION = re.compile(r"^\s+❯ \S")
_NUMBERED_SELECTION = re.compile(r"^\s+❯ \d+\. \S")
_PERMISSION_QUESTION = re.compile(r"^\s*Do you want to .+\?$")
_DIALOG_FOOTER = "Esc to cancel"
_PLAN_QUESTION = "Would you like to proceed?"
_PLAN_FOOTER = "ctrl+g to edit in"
_PLAN_TITLE = re.compile(r"^Ready to code\?(?:\s+[↑↓]{1,2})?$")
"""The title line of the plan approval dialog, with the scroll arrows it shows when the plan does not fit."""
_NUMBERED_OPTION = re.compile(r"^(\s*)(?:❯ )?(\d+)\.\s+(\S.*)$")

_ESCAPE = re.compile(r"\x1b(?:\[([0-9;:?]*)([@-~])|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")


@dataclass(frozen=True)
class Cell:
    char: str
    fg: str | None
    """The foreground color as its SGR parameters (``5;241``, ``2;10;20;30``, ``31``), ``None`` for the default."""
    dim: bool


@dataclass(frozen=True)
class Option:
    """One choice of a dialog, as its line on the screen shows it."""

    key: str | None
    """The number to press, such as ``1``; ``None`` for a dialog whose choices have no numbers, chosen with the arrow
    keys and Enter."""
    label: str
    selected: bool
    """Whether the ``❯`` selection is on it, so Enter picks it."""


@dataclass(frozen=True)
class Classification:
    state: ScreenState
    title: str | None = None
    """The title line of a ``dialog``, ``permission_prompt`` or ``plan_approval``, ``None`` for every other state."""
    options: tuple[Option, ...] | None = field(default=None, compare=False)
    """The choices of a ``dialog``, ``permission_prompt`` or ``plan_approval``, top to bottom, ``None`` for every other
    state. Left out of comparisons, so a classification compares by what the screen is, not by every choice on it."""


def _apply_sgr(params: str, fg: str | None, dim: bool) -> tuple[str | None, bool]:
    parts = params.replace(":", ";").split(";") if params else ["0"]
    index = 0
    while index < len(parts):
        code = parts[index]
        if code in ("", "0"):
            fg, dim = None, False
        elif code == "2":
            dim = True
        elif code == "22":
            dim = False
        elif code == "39":
            fg = None
        elif code in ("38", "48"):
            mode = parts[index + 1] if index + 1 < len(parts) else ""
            width = 2 if mode == "5" else 4 if mode == "2" else 1
            if code == "38":
                fg = ";".join(parts[index + 1 : index + 1 + width])
            index += width
        elif (code.isdecimal() and 30 <= int(code) <= 37) or (code.isdecimal() and 90 <= int(code) <= 97):
            fg = code
        index += 1
    return fg, dim


def parse(captured: str) -> list[list[Cell]]:
    """Split a ``capture-pane -e`` text into lines of cells with their foreground color and dim attribute.

    tmux writes an attribute change only where it happens, so the state carries over from one line to the next.
    """
    fg: str | None = None
    dim = False
    lines: list[list[Cell]] = []
    for raw in captured.split("\n"):
        cells: list[Cell] = []
        position = 0
        for match in _ESCAPE.finditer(raw):
            cells.extend(Cell(char, fg, dim) for char in raw[position : match.start()])
            if match.group(2) == "m":
                fg, dim = _apply_sgr(match.group(1), fg, dim)
            position = match.end()
        cells.extend(Cell(char, fg, dim) for char in raw[position:])
        lines.append(cells)
    return lines


def plain_lines(captured: str) -> list[str]:
    """The captured text without escape sequences or trailing whitespace, and without the empty lines at the end."""
    lines = [_ESCAPE.sub("", line).rstrip() for line in captured.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _is_rule(text: str, chars: tuple[str, ...] = (_RULE,)) -> bool:
    """A horizontal rule at column 0. The prompt box's upper rule can carry the session name: ``──── name ─``."""
    return any(text.startswith(char * _MIN_RULE) for char in chars)


def _is_border(text: str) -> bool:
    """A dialog's top border: a line of nothing but one rule character."""
    stripped = text.rstrip()
    return any(len(stripped) >= _MIN_RULE and set(stripped) == {char} for char in _DIALOG_BORDERS)


def _greyed(cell: Cell) -> bool:
    """The prompt glyph as Claude Code draws it while the session works: grey (``5;241`` in the dark theme)."""
    if cell.dim:
        return True
    if cell.fg in ("90", "5;8"):
        return True
    if cell.fg is not None and cell.fg.startswith("5;"):
        number = cell.fg[2:]
        return number.isdecimal() and 232 <= int(number) <= 255
    return False


def _prompt_box(texts: list[str]) -> tuple[int, int] | None:
    """The line of the ``❯`` glyph and of the rule under the box, when the screen ends with the prompt box."""
    for glyph in range(len(texts) - 1, 0, -1):
        text = texts[glyph]
        if not text.startswith(_PROMPT_GLYPH) or not _is_rule(texts[glyph - 1]):
            continue
        after = text[len(_PROMPT_GLYPH) : len(_PROMPT_GLYPH) + 1]
        if after not in ("", " ", _NBSP):
            return None
        for bottom in range(glyph + 1, min(len(texts), glyph + 1 + _MAX_INPUT_LINES)):
            if _is_rule(texts[bottom]):
                footer = [line for line in texts[bottom + 1 :] if line.strip()]
                return (glyph, bottom) if len(footer) <= _MAX_FOOTER_LINES else None
        return None
    return None


def _classify_prompt(
    cells: list[list[Cell]], texts: list[str], glyph: int, bottom: int, cursor: tuple[int, int] | None
) -> Classification:
    above = [line for line in texts[: glyph - 1] if line.strip()][-_SPINNER_LOOKBACK:]
    if any(_SPINNER.match(line) for line in above):
        return Classification("busy")
    glyph_cell = cells[glyph][0]
    if _greyed(glyph_cell):
        return Classification("busy")
    if glyph_cell.fg is not None:
        return Classification("unknown")
    first = [cell for cell in cells[glyph][2:] if not cell.char.isspace()]
    rest = [cell for line in cells[glyph + 1 : bottom] for cell in line if not cell.char.isspace()]
    content = first + rest
    if content and not any(cell.dim for cell in content):
        return Classification("prompt_has_input")
    # Typed spaces leave no mark in a capture, and the cursor is the only sign of them. Typing replaces the dimmed
    # placeholder and suggestion, which fit on the glyph line; dimmed text anywhere else was never seen.
    empty = not rest and all(cell.dim for cell in first)
    if empty and cursor == (_EMPTY_CURSOR_COLUMN, glyph):
        return Classification("prompt_empty")
    return Classification("unknown")


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _options(block: list[str]) -> tuple[Option, ...]:
    """The choices around the ``❯`` selection: its line and the lines next to it that are drawn the same way.

    Numbered choices have their number in the selection's number column; a line indented deeper under one, such as a
    hint, is skipped. Choices without numbers start their text in the selection's text column. A blank line or any
    other line ends the list.
    """
    selected = next(i for i, line in enumerate(block) if _DIALOG_SELECTION.match(line))
    numbered = _NUMBERED_OPTION.match(block[selected])
    options: dict[int, Option] = {}
    if numbered is not None:
        column = len(numbered.group(1)) + len(_PROMPT_GLYPH) + 1

        def option(index: int) -> Option | None:
            match = _NUMBERED_OPTION.match(block[index])
            if match is None or index != selected and len(match.group(1)) != column:
                return None
            return Option(match.group(2), match.group(3).strip(), index == selected)

    else:
        column = block[selected].index(_PROMPT_GLYPH) + len(_PROMPT_GLYPH) + 1

        def option(index: int) -> Option | None:
            line = block[index]
            if index == selected:
                return Option(None, line.strip()[len(_PROMPT_GLYPH) :].strip(), True)
            return Option(None, line.strip(), False) if line.strip() and _indent(line) == column else None

    for step in (-1, 1):
        index = selected
        while 0 <= index < len(block):
            found = option(index)
            if found is not None:
                options[index] = found
            elif step < 0 or not block[index].strip() or _indent(block[index]) <= column:
                break
            index += step
    return tuple(options[index] for index in sorted(options))


def _classify_dialog(texts: list[str]) -> Classification:
    filled = [index for index, line in enumerate(texts) if line.strip()]
    footer = next((i for i in reversed(filled[-_FOOTER_LOOKBACK:]) if _DIALOG_FOOTER in texts[i]), None)
    if footer is None:
        return Classification("unknown")
    top = next((i for i in range(footer - 1, -1, -1) if _is_border(texts[i])), None)
    if top is None:
        return Classification("unknown")
    block = texts[top + 1 : footer]
    if not any(_DIALOG_SELECTION.match(line) for line in block):
        return Classification("unknown")
    title = next((line.strip() for line in block if line.strip()), None)
    if title is None:
        return Classification("unknown")
    numbered = any(_NUMBERED_SELECTION.match(line) for line in block)
    if numbered and any(_PERMISSION_QUESTION.match(line) for line in block):
        return Classification("permission_prompt", title, _options(block))
    return Classification("dialog", title, _options(block))


def _classify_plan_approval(texts: list[str]) -> Classification:
    """The dialog of ``ExitPlanMode``: ``Ready to code?``, the plan, the question, numbered choices, and a
    ``ctrl+g to edit in …`` footer instead of ``Esc to cancel``. Only the choices after the question count, because
    the plan above them can hold numbered lines too."""
    filled = [index for index, line in enumerate(texts) if line.strip()]
    footer = next((i for i in reversed(filled[-_FOOTER_LOOKBACK:]) if texts[i].strip().startswith(_PLAN_FOOTER)), None)
    if footer is None:
        return Classification("unknown")
    top = next((i for i in range(footer - 1, -1, -1) if _is_border(texts[i])), None)
    if top is None:
        return Classification("unknown")
    block = texts[top + 1 : footer]
    title = next((i for i, line in enumerate(block) if _PLAN_TITLE.match(line.strip())), None)
    question = next((i for i in range(len(block) - 1, -1, -1) if block[i].rstrip().endswith(_PLAN_QUESTION)), None)
    if title is None or question is None or question < title:
        return Classification("unknown")
    choices = block[question + 1 :]
    if not any(_NUMBERED_SELECTION.match(line) for line in choices):
        return Classification("unknown")
    return Classification("plan_approval", PLAN_APPROVAL_TITLE, _options(choices))


def classify(captured: str, cursor: tuple[int, int] | None = None) -> Classification:
    """Classify a pane from its ``tmux capture-pane -p -e`` text and its cursor, ``(#{cursor_x}, #{cursor_y})``.

    - ``prompt_empty``: the prompt box is at the bottom, idle, holds nothing or only a dimmed placeholder or suggestion
      on the line of the glyph, and the cursor is right after the glyph. Without the cursor it is ``unknown``.
    - ``prompt_has_input``: the same, with typed text in it.
    - ``busy``: the prompt box shows the spinner above it or the greyed glyph, whatever the box holds.
    - ``dialog``: a bordered box with a ``❯`` selection and an ``Esc to cancel`` footer, such as a startup dialog.
    - ``permission_prompt``: such a box that asks ``Do you want to …?`` with numbered options.
    - ``plan_approval``: the plan approval dialog of ``ExitPlanMode``, a bordered box titled ``Ready to code?`` that
      asks ``Would you like to proceed?`` with numbered options and ends with a ``ctrl+g to edit in …`` line.
    - ``unknown``: anything else, including a prompt box captured without ``-e`` and a screen that matches both the
      prompt box and the dialog rules.
    """
    cells = parse(captured)
    texts = ["".join(cell.char for cell in line).rstrip() for line in cells]
    box = _prompt_box(texts)
    dialog = _classify_dialog(texts)
    if dialog.state == "unknown":
        dialog = _classify_plan_approval(texts)
    if box is None:
        return dialog
    if dialog.state != "unknown" or "\x1b[" not in captured:
        # A dialog drawn under a prompt box takes the keys, so the box says nothing about what typing does.
        return Classification("unknown")
    return _classify_prompt(cells, texts, *box, cursor)
