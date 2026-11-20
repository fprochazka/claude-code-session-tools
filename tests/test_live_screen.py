"""The live contract test of the screen classifier: start a real Claude Code session in a temporary tmux session, pass
its startup dialogs, type into its prompt, run a busy turn into a permission prompt, and classify the pane at each step.

It is excluded from the default run and from CI. Run it with ``uv run pytest -m live -s`` after a Claude Code update to
confirm that the classifier still reads the screen; a failure prints the screen it could not classify. It needs tmux
and a logged-in ``claude`` on PATH, or the command in ``CC_SESSION_TOOLS_LIVE_CLAUDE`` (such as an auth-switch wrapper
``claude-personal``). It runs against that real profile and spends a few model calls on ``CC_SESSION_TOOLS_LIVE_MODEL``
(``haiku``).

By default the session runs in one stable directory, ``~/.cache/cc-session-tools/live-test``, so the test adds at most
one project entry to the profile's config, ever. Claude Code remembers the trust, MCP server and imports answers there,
so after the first run those dialogs do not show; the test answers each one that shows, always the same way, and does
not require them. ``CC_SESSION_TOOLS_LIVE_FRESH_DIR=1`` runs in a new temporary directory instead and requires all
three dialogs to show and classify; each such run adds one more project entry to the profile's config. The unit tests
cover the dialogs through fixtures either way. Every run, in either mode, leaves the session's transcript under the
profile's ``projects/`` dir; the test does not delete it.

The session runs with hooks disabled and in the default permission mode, so the Bash command it is asked to run waits
for approval; the test declines it. A second session runs in plan mode, is asked for a short plan, and shows the plan
approval dialog; the test rejects the plan with Esc, so nothing is edited. It never touches a tmux session it did not
start.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from cc_session_tools.screen import Classification, classify, plain_lines

pytestmark = pytest.mark.live

CLAUDE = os.environ.get("CC_SESSION_TOOLS_LIVE_CLAUDE", "claude")
MODEL = os.environ.get("CC_SESSION_TOOLS_LIVE_MODEL", "haiku")
TIMEOUT = 60.0
POLL = 0.2
FRESH = os.environ.get("CC_SESSION_TOOLS_LIVE_FRESH_DIR") == "1"
SETTLE = 1.0
"""A screen is drawn before it takes keys; a key sent at once can be lost, and Enter then picks the default."""

# Variables that tie a process to the Claude Code session it runs in. The tmux server may have them from the session
# that started it; the test session must not inherit them.
_SESSION_ENV = (
    "CLAUDECODE",
    "CLAUDE_PID",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    "CLAUDE_CODE_MESSAGING_TOKEN",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_BRIDGE_SESSION_ID",
    "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SESSION_NAME",
)


class Pane:
    def __init__(self, target: str) -> None:
        self.target = target
        self.seen: list[str] = []

    def _tmux(self, command: str, *args: str) -> str:
        result = subprocess.run(["tmux", command, "-t", self.target, *args], capture_output=True, text=True)
        if result.returncode != 0:
            pytest.fail(f"the Claude Code session exited; states seen: {self.seen}")
        return result.stdout

    def capture(self) -> tuple[str, tuple[int, int] | None]:
        """The screen and the cursor, which counts only when it did not move during the capture."""
        before = self._tmux("display-message", "-p", "#{cursor_x} #{cursor_y}").split()
        captured = self._tmux("capture-pane", "-p", "-e")
        after = self._tmux("display-message", "-p", "#{cursor_x} #{cursor_y}").split()
        cursor = (int(after[0]), int(after[1])) if before == after and len(after) == 2 else None
        return captured, cursor

    def keys(self, *keys: str, literal: bool = False) -> None:
        subprocess.run(["tmux", "send-keys", "-t", self.target, *(["-l"] if literal else []), *keys], check=True)
        time.sleep(0.5)

    def wait_for_line(self, line: str) -> None:
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            if any(text.strip() == line for text in plain_lines(self.capture()[0])):
                return
            time.sleep(POLL)
        pytest.fail(f"the pane never showed the line {line!r}")

    def _poll(self, done: Callable[[Classification], bool], what: str, timeout: float = TIMEOUT) -> Classification:
        """Poll until ``done`` holds for the pane's classification, recording every state seen on the way."""
        deadline = time.monotonic() + timeout
        captured, cursor = "", None
        while time.monotonic() < deadline:
            captured, cursor = self.capture()
            found = classify(captured, cursor)
            if not self.seen or self.seen[-1] != found.state:
                self.seen.append(found.state)
            if done(found):
                time.sleep(SETTLE)
                return found
            time.sleep(POLL)
        screen = "\n".join(plain_lines(captured)[-40:])
        pytest.fail(f"the pane never showed {what}; last seen {classify(captured, cursor)} at {cursor}:\n{screen}")

    def wait_for(self, expected: Classification, timeout: float = TIMEOUT) -> Classification:
        return self._poll(lambda found: found == expected, str(expected), timeout)

    def wait_for_any(self, *states: str) -> Classification:
        return self._poll(lambda found: found.state in states, " or ".join(states))

    def wait_until_not(self, current: Classification) -> Classification:
        return self._poll(lambda found: found != current and found.state != "unknown", f"anything but {current}")


STABLE_DIR = Path.home() / ".cache" / "cc-session-tools" / "live-test"
TRUST = "Accessing workspace:"
MCP = "New MCP server found in this project"
IMPORTS = "Allow external CLAUDE.md file imports?"


def _prepare(root: Path) -> tuple[Path, Path]:
    """Write the project, the file its CLAUDE.md imports and the settings, with the same content on every run."""
    project = root / "project"
    project.mkdir(parents=True, exist_ok=True)
    (root / "external.md").write_text("Answer briefly.\n")
    (project / "CLAUDE.md").write_text(f"# Live screen test\n\n@{root / 'external.md'}\n")
    (project / ".mcp.json").write_text('{"mcpServers": {"ccst-live-dummy": {"command": "true", "args": []}}}\n')
    (project / "target.txt").write_text("")
    settings = root / "settings.json"
    settings.write_text('{"disableAllHooks": true}\n')
    return project, settings


@contextlib.contextmanager
def _session(permission_mode: str) -> Iterator[Pane]:
    if shutil.which("tmux") is None or shutil.which(CLAUDE) is None:
        pytest.skip(f"needs tmux and {CLAUDE} on PATH")
    root = Path(tempfile.mkdtemp(prefix="ccst-live-", dir="/tmp")) if FRESH else STABLE_DIR
    project, settings = _prepare(root)
    name = f"ccst-live-{uuid.uuid4().hex[:8]}"
    unset = [arg for var in _SESSION_ENV for arg in ("-u", var)]
    mode = ["--permission-mode", permission_mode]
    command = ["env", *unset, CLAUDE, "--model", MODEL, "--settings", str(settings), *mode]
    subprocess.run(
        ["tmux", "new-session", "-d", "-s", name, "-x", "200", "-y", "50", "-c", str(project), "--", *command],
        check=True,
    )
    try:
        yield Pane(f"{name}:")
    finally:
        subprocess.run(["tmux", "kill-session", "-t", f"={name}"], check=False, capture_output=True)
        if FRESH:
            shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def pane() -> Iterator[Pane]:
    with _session("default") as started:
        yield started


@pytest.fixture
def plan_pane() -> Iterator[Pane]:
    with _session("plan") as started:
        yield started


def _answer(pane: Pane, title: str) -> None:
    """Answer a startup dialog the same way on every run, so the choices Claude Code stores never change."""
    if title == TRUST:
        pane.keys("Down")  # the dialog selects "No, exit" first
        pane.wait_for_line("❯ Yes, I trust this folder")
        pane.keys("Enter")
    elif title.startswith(MCP) or title == IMPORTS:
        pane.keys("Enter")  # "Continue without using this MCP server", "No, disable external imports"
    else:
        pytest.fail(f"an unexpected startup dialog: {title!r}")
    pane.wait_until_not(Classification("dialog", title))


def _pass_startup(pane: Pane) -> list[str]:
    """Answer every startup dialog that shows, until the prompt is empty; return their titles in order."""
    titles: list[str] = []
    while True:
        found = pane.wait_for_any("prompt_empty", "dialog")
        if found.state == "prompt_empty":
            return titles
        assert found.title is not None
        titles.append(found.title)
        _answer(pane, found.title)


def test_the_classifier_reads_a_real_session(pane: Pane) -> None:
    titles = _pass_startup(pane)
    print(f"startup dialogs answered: {titles}")
    if FRESH:
        assert titles[0] == TRUST, titles
        assert len(titles) == 3 and titles[1].startswith(MCP) and titles[2] == IMPORTS, titles

    pane.keys("half typed", literal=True)
    pane.wait_for(Classification("prompt_has_input"))
    pane.keys("-N", "20", "BSpace")
    pane.wait_for(Classification("prompt_empty"))
    pane.keys("   ", literal=True)  # a capture drops the spaces; only the cursor shows them
    time.sleep(SETTLE)
    assert classify(*pane.capture()) == Classification("unknown")
    pane.keys("-N", "3", "BSpace")
    pane.wait_for(Classification("prompt_empty"))

    pane.seen.clear()
    pane.keys("Run exactly this bash command in the foreground: chmod 600 target.txt", literal=True)
    pane.keys("Enter")
    pane.wait_for(Classification("permission_prompt", "Bash command"))
    assert "busy" in pane.seen, pane.seen
    pane.keys("Escape")
    pane.wait_for(Classification("prompt_empty"))


PLAN_TIMEOUT = 180.0
"""Planning reads the project and writes the plan file before the dialog shows, which takes longer than one answer."""


def test_the_classifier_reads_a_real_plan_approval(plan_pane: Pane) -> None:
    _pass_startup(plan_pane)
    prompt = "Make a short plan to append the line world to target.txt. Do not ask questions; present the plan."
    plan_pane.keys(prompt, literal=True)
    plan_pane.keys("Enter")
    found = plan_pane.wait_for(Classification("plan_approval", "Ready to code?"), PLAN_TIMEOUT)
    assert found.options is not None
    assert [option.key for option in found.options] == [str(n) for n in range(1, len(found.options) + 1)]
    assert found.options[0].selected
    assert "Yes, manually approve edits" in [option.label for option in found.options], found.options
    plan_pane.keys("Escape")  # rejects the plan; the session stays in plan mode at an empty prompt
    plan_pane.wait_for(Classification("prompt_empty"))
