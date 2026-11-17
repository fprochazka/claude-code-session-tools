from __future__ import annotations

from pathlib import Path

import pytest

from cc_session_tools.screen import Classification, classify, parse, plain_lines

FIXTURES = Path(__file__).parent / "fixtures" / "screen"

# Captured with `tmux capture-pane -p -e` from throwaway Claude Code 2.1.325 sessions, one per state.
EXPECTED = {
    "busy-spinner-tip": Classification("busy"),
    "busy-streaming": Classification("busy"),
    "busy-thinking": Classification("busy"),
    "busy-tool": Classification("busy"),
    "busy-typed": Classification("busy"),
    "dialog-imports": Classification("dialog", "Allow external CLAUDE.md file imports?"),
    "dialog-mcp": Classification("dialog", "New MCP server found in this project: ccst-dummy"),
    "dialog-model-picker": Classification("dialog", "Select model"),
    "dialog-trust": Classification("dialog", "Accessing workspace:"),
    "permission-bash": Classification("permission_prompt", "Bash command"),
    "prompt-empty-after-interrupt": Classification("prompt_empty"),
    "prompt-empty-after-interrupt-cursor": Classification("prompt_empty"),
    "prompt-empty-after-turn": Classification("prompt_empty"),
    "prompt-empty-placeholder": Classification("prompt_empty"),
    "prompt-empty-placeholder-nostatusline": Classification("prompt_empty"),
    "prompt-empty-shell-running": Classification("prompt_empty"),
    "prompt-empty-suggestion": Classification("prompt_empty"),
    "prompt-empty-vim-insert": Classification("prompt_empty"),
    # Wrong, and a known gap: vim NORMAL mode takes the keys as commands. See the test below.
    "prompt-empty-vim-normal": Classification("prompt_empty"),
    "prompt-has-input": Classification("prompt_has_input"),
    "prompt-has-input-image": Classification("prompt_has_input"),
    "prompt-has-input-paste": Classification("prompt_has_input"),
    "prompt-has-input-placeholder-lookalike": Classification("prompt_has_input"),
    "prompt-has-input-vim-normal": Classification("prompt_has_input"),
    "prompt-has-input-slash-menu": Classification("prompt_has_input"),
    "prompt-multiline": Classification("prompt_has_input"),
    "unknown-bash-mode": Classification("unknown"),
    "unknown-transcript-view": Classification("unknown"),
    "unknown-typed-spaces": Classification("unknown"),
}

# The cursor, (#{cursor_x}, #{cursor_y}), recorded with the capture.
CURSORS = {
    "prompt-empty-after-interrupt-cursor": (2, 79),
    "prompt-empty-suggestion": (2, 79),
    "unknown-typed-spaces": (5, 79),
    "prompt-empty-vim-insert": (2, 79),
    "prompt-empty-vim-normal": (2, 79),
    "prompt-has-input-vim-normal": (11, 79),
    # Captured before the cursor was recorded. Every empty prompt captured since, and the live contract test, has the
    # cursor right after the glyph.
    "prompt-empty-after-interrupt": (2, 79),
    "prompt-empty-after-turn": (2, 78),
    "prompt-empty-placeholder": (2, 78),
    "prompt-empty-placeholder-nostatusline": (2, 79),
    "prompt-empty-shell-running": (2, 78),
}
EMPTY = [name for name, c in EXPECTED.items() if c.state == "prompt_empty"]

RULE = "─" * 40


def fixture(name: str) -> str:
    return (FIXTURES / f"{name}.ansi").read_text(encoding="utf-8")


def test_every_fixture_has_an_expectation() -> None:
    assert sorted(path.stem for path in FIXTURES.glob("*.ansi")) == sorted(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_captured_screen_is_classified(name: str) -> None:
    assert classify(fixture(name), CURSORS.get(name)) == EXPECTED[name]


@pytest.mark.parametrize("name", EMPTY)
def test_an_empty_prompt_without_the_cursor_after_the_glyph_is_unknown(name: str) -> None:
    x, y = CURSORS[name]
    assert classify(fixture(name)).state == "unknown"
    assert classify(fixture(name), (x + 3, y)).state == "unknown"
    assert classify(fixture(name), (x, y - 1)).state == "unknown"


def test_vim_normal_mode_shows_the_footer_of_a_session_without_vim_mode() -> None:
    """Why NORMAL mode reads as ``prompt_empty``: its screen differs from INSERT mode only in the footer, which loses
    ``-- INSERT --`` and then looks like the footer of a session without vim mode."""
    insert = plain_lines(fixture("prompt-empty-vim-insert"))
    normal = plain_lines(fixture("prompt-empty-vim-normal"))
    assert insert[-1].split() == ["--", "INSERT", "--", "⏸", "manual", "mode", "on", "·", "←", "for", "agents"]
    assert normal[-1].split() == ["⏸", "manual", "mode", "on", "·", "?", "for", "shortcuts"]
    assert plain_lines(fixture("prompt-empty-after-interrupt-cursor"))[-1] == normal[-1]


def test_typed_spaces_are_told_from_an_empty_prompt_only_by_the_cursor() -> None:
    assert classify(fixture("unknown-typed-spaces"), (2, 79)).state == "prompt_empty"


def test_the_placeholder_and_the_same_text_typed_differ_only_in_the_dim_attribute() -> None:
    placeholder = plain_lines(fixture("prompt-empty-placeholder"))
    typed = plain_lines(fixture("prompt-has-input-placeholder-lookalike"))
    assert any(line.startswith('❯ Try "create a util logging.py that..."') for line in placeholder)
    assert any(line.startswith('❯ Try "create a util logging.py that..."') for line in typed)


@pytest.mark.parametrize("name", [name for name, c in EXPECTED.items() if c.title is None])
def test_a_prompt_box_captured_without_escape_sequences_is_unknown(name: str) -> None:
    assert classify("\n".join(plain_lines(fixture(name))), CURSORS.get(name)).state == "unknown"


@pytest.mark.parametrize("name", ["dialog-trust", "dialog-mcp", "dialog-imports", "permission-bash"])
def test_dialogs_are_recognised_without_escape_sequences(name: str) -> None:
    assert classify("\n".join(plain_lines(fixture(name)))) == EXPECTED[name]


def box(prompt_line: str, *, above: str = "", footer: str = "  ⏸ manual mode on") -> str:
    return f"{above}\n\x1b[38;5;245m{RULE}\n{prompt_line}\n\x1b[38;5;245m{RULE}\x1b[39m\n{footer}\n"


def test_a_prompt_with_dim_and_plain_text_mixed_is_unknown() -> None:
    assert classify(box("\x1b[39m❯ typed \x1b[2msuggested\x1b[0m")).state == "unknown"


def test_a_prompt_glyph_in_an_unknown_color_is_unknown() -> None:
    assert classify(box("\x1b[38;5;199m❯ \x1b[39m")).state == "unknown"


def test_a_spinner_above_the_box_is_busy_whatever_the_glyph() -> None:
    above = "✶ Pondering… (12s · ↑ 2.1k tokens · esc to interrupt)"
    assert classify(box("\x1b[39m❯ ", above=above)).state == "busy"


def test_a_finished_turn_line_above_the_box_is_not_a_spinner() -> None:
    assert classify(box("\x1b[39m❯ ", above="✻ Cooked for 10s · done 12:24"), (2, 2)).state == "prompt_empty"


def test_dimmed_text_under_the_glyph_line_is_unknown() -> None:
    assert classify(box("\x1b[39m❯ \x1b[2mTry this\x1b[0m"), (2, 2)).state == "prompt_empty"
    assert classify(box("\x1b[39m❯ \x1b[2mTry this\n  and that\x1b[0m"), (2, 2)).state == "unknown"


def test_a_prompt_echoed_in_the_history_is_not_the_prompt_box() -> None:
    screen = f"\x1b[39m❯ an earlier prompt\n● an answer\n{RULE}\n  Showing detailed transcript · ctrl+o to toggle\n"
    assert classify(screen).state == "unknown"


def test_a_prompt_box_with_more_than_a_footer_below_it_is_unknown() -> None:
    footer = "\n".join(f"line {index}" for index in range(10))
    assert classify(box("\x1b[39m❯ ", footer=footer)).state == "unknown"


def test_a_screen_that_shows_both_a_prompt_box_and_a_dialog_is_unknown() -> None:
    dialog = "  Pick one\n  ❯ 1. Yes\n    2. No\n  Esc to cancel"
    assert classify(f"{RULE}\n{dialog}\n").state == "dialog"
    assert classify(box("\x1b[39m❯ ", footer=dialog)).state == "unknown"


def test_a_dialog_without_a_selection_is_unknown() -> None:
    screen = f"{RULE}\n  Some notice\n\n  Press any key\n\n  Enter to confirm · Esc to cancel\n"
    assert classify(screen).state == "unknown"


def test_numbered_options_without_a_permission_question_are_a_dialog() -> None:
    screen = f"{RULE}\n Pick one\n ❯ 1. First\n   2. Second\n Esc to cancel\n"
    assert classify(screen) == Classification("dialog", "Pick one")


@pytest.mark.parametrize("text", ["", "\n\n", "$ ls\nfile.txt\n$ "])
def test_an_empty_or_foreign_screen_is_unknown(text: str) -> None:
    assert classify(text).state == "unknown"


def test_attributes_carry_over_from_one_line_to_the_next() -> None:
    lines = parse("\x1b[2;38;5;241mab\ncd\x1b[22;39me\x1b[38:2:1:2:3mf")
    assert [(c.char, c.fg, c.dim) for c in lines[1]] == [
        ("c", "5;241", True),
        ("d", "5;241", True),
        ("e", None, False),
        ("f", "2;1;2;3", False),
    ]


def test_plain_lines_drop_escapes_trailing_whitespace_and_trailing_empty_lines() -> None:
    assert plain_lines("\x1b[1ma \x1b]8;;https://x\x1b\\b\x1b]8;;\x1b\\  \n\n  \n") == ["a b"]
