from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

# Variables that point cc-blackbox at the real Claude Code setup, or at the session the tests run under. Tests often run
# inside a Claude Code session, so every test starts without them and with an empty home dir, and reads only the
# projects and sessions dirs it builds.
_REAL_SETUP_ENV = (
    "CLAUDE_CONFIG_DIR",
    "FP_CC_AUTH_SWITCH_PROFILE",
    "CLAUDE_PROJECTS_DIR",
    "CLAUDE_PID",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_MESSAGING_SOCKET",
    # The tmux server and pane the tests may run in.
    "TMUX",
    "TMUX_PANE",
)


@pytest.fixture(autouse=True)
def isolated_home(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if request.node.get_closest_marker("live") is not None:
        return Path.home()  # the live contract test runs against the real Claude Code setup
    for name in _REAL_SETUP_ENV:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def sid(name: str) -> str:
    """A stable session id for a readable fixture name. Claude Code names sessions only by UUID."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"session:{name}"))


STARTED_AT = 1794100000000


def _proc_start(pid: int) -> str:
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    return stat[stat.rfind(")") + 2 :].split(" ")[19]


class Machine:
    """A home dir with three profiles and session records that belong to real, running processes.

    Each record's pid is a sleeping child process, so cc-blackbox's own liveness check keeps it.
    """

    def __init__(self, home: Path) -> None:
        self.home = home
        self.processes: dict[int, subprocess.Popen[bytes]] = {}
        self.records: dict[int, Path] = {}
        (home / ".claude" / "sessions").mkdir(parents=True)
        (home / ".claude" / "auth-switch.yaml").write_text("profiles:\n  work: {}\n  lab: {}\n")
        for name in ("work", "lab"):
            (home / ".claude-profiles" / name / "sessions").mkdir(parents=True)

    def config_dir(self, profile: str) -> Path:
        return self.home / ".claude" if profile == "default" else self.home / ".claude-profiles" / profile

    def session(self, profile: str, name: str | None, **fields: Any) -> int:
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        pid = process.pid
        self.processes[pid] = process
        record: dict[str, Any] = {
            "pid": pid,
            "sessionId": sid(name or str(pid)),
            "cwd": str(self.home / "elsewhere"),
            "startedAt": STARTED_AT + len(self.processes) * 1000,
            "procStart": _proc_start(pid),
            "version": "2.1.319",
            "peerProtocol": 1,
            "peerFeatures": ["notify_idle"],
            "kind": "interactive",
            "entrypoint": "cli",
            "messagingSocketPath": f"/run/user/1000/cc-socks/{pid}.sock",
            "name": name,
            "nameSource": "user",
            "status": "idle",
            "updatedAt": STARTED_AT + 60000,
        }
        record.update(fields)
        path = self.config_dir(profile) / "sessions" / f"{pid}.json"
        self.records[pid] = path
        self._write(path, record)
        return pid

    @staticmethod
    def _write(path: Path, record: dict[str, Any]) -> None:
        record = {k: v for k, v in record.items() if v is not None}
        previous = path.stat().st_mtime_ns if path.exists() else None
        path.write_text(json.dumps(record))
        if previous is not None:
            # Two writes within the file system's timestamp granularity would look unchanged to a watcher.
            os.utime(path, ns=(previous + 1_000_000, previous + 1_000_000))

    def update(self, pid: int, **fields: Any) -> None:
        """Rewrite the record of ``pid`` with ``fields`` changed, as Claude Code does on a status change or rename."""
        path = self.records[pid]
        record = json.loads(path.read_text())
        record.update(fields)
        self._write(path, record)

    def remove_record(self, pid: int) -> None:
        self.records[pid].unlink()

    def kill(self, pid: int) -> None:
        process = self.processes[pid]
        process.kill()
        process.wait()

    def stop(self) -> None:
        for process in self.processes.values():
            process.kill()
            process.wait()


@pytest.fixture
def machine(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Machine]:
    monkeypatch.setenv("COLUMNS", "200")
    m = Machine(isolated_home)
    try:
        yield m
    finally:
        m.stop()


@pytest.fixture
def fake_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A dir first on PATH, for fake commands."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


def stamp(epoch_seconds: float) -> str:
    """A transcript timestamp, as Claude Code writes it."""
    return time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(epoch_seconds))


def write_records(path: Path, records: list[dict[str, Any]]) -> Path:
    """Write transcript records the way Claude Code does: one compact JSON object per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records))
    return path


def response(epoch_seconds: float, *, model: str = "claude-opus-5-5") -> dict[str, Any]:
    """An assistant record: a model response at ``epoch_seconds``."""
    return {
        "type": "assistant",
        "timestamp": stamp(epoch_seconds),
        "message": {"role": "assistant", "model": model, "content": [{"type": "text", "text": "ok"}]},
    }


def cost_state(total: float) -> dict[str, Any]:
    """A ``cost-state`` record, which Claude Code appends when a session process exits."""
    return {
        "type": "cost-state",
        "totalCostUSD": total,
        "hasUnknownModelCost": False,
        "startTime": STARTED_AT,
        "totalDuration": 5_400_000,
        "totalAPIDuration": 600_000,
        "totalAPIDurationWithoutRetries": 590_000,
        "totalToolDuration": 30_000,
        "totalLinesAdded": 12,
        "totalLinesRemoved": 3,
        "modelUsage": {
            "claude-opus-5-5": {
                "inputTokens": 40,
                "outputTokens": 1500,
                "thinkingTokens": 100,
                "cacheReadInputTokens": 90000,
                "cacheCreationInputTokens": 800,
                "webSearchRequests": 0,
                "costUSD": total,
            }
        },
    }


def fake_tmux(bin_dir: Path, panes: list[str] | None, *, exit_code: int = 0) -> None:
    """Install a ``tmux`` that prints ``panes`` (tab-separated list-panes lines) and exits with ``exit_code``.

    Calling it again changes what the installed ``tmux`` prints from then on.
    """
    output = "".join(f"{line}\n" for line in panes or [])
    data = bin_dir / "tmux-panes.txt"
    data.write_text(output)
    script = bin_dir / "tmux"
    script.write_text(f"#!/bin/sh\ncat '{data}'\nexit {exit_code}\n")
    script.chmod(0o755)


class RealTmux:
    """A private tmux server of its own, which every ``tmux`` call of the test reaches through ``$TMUX_TMPDIR``."""

    def run(self, *args: str) -> str:
        result = subprocess.run(
            ["tmux", "-f", os.devnull, *args], capture_output=True, text=True, check=True, timeout=10
        )
        return result.stdout.strip()

    def new_session(self, name: str) -> str:
        """Start a tmux session; return the pane id of its first window."""
        return self.run("new-session", "-d", "-s", name, "-P", "-F", "#{pane_id}", "sleep 600")

    def new_window(self, session: str) -> str:
        return self.run("new-window", "-d", "-t", f"{session}:", "-P", "-F", "#{pane_id}", "sleep 600")

    def move_window(self, pane: str, to_session: str) -> None:
        """Move the window that holds ``pane`` to another tmux session."""
        self.run("move-window", "-s", pane, "-t", f"{to_session}:")

    def recorded(self, pane: str) -> str:
        """The pane as Claude Code records it at startup: ``<session>:@<window>.%<pane>``."""
        return self.run("display-message", "-p", "-t", pane, "#{session_name}:#{window_id}.#{pane_id}")


@pytest.fixture
def real_tmux(fake_bin: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RealTmux]:
    """A real tmux server in a temp dir, never the user's. A fake ``tmux`` another fixture installed is removed."""
    (fake_bin / "tmux").unlink(missing_ok=True)
    if shutil.which("tmux") is None:
        pytest.skip("tmux is not installed")
    # A short dir: the server socket path must fit in a unix socket address.
    socket_dir = tempfile.mkdtemp(prefix="cst-tmux-", dir="/tmp")
    monkeypatch.setenv("TMUX_TMPDIR", socket_dir)
    tmux = RealTmux()
    try:
        yield tmux
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True, check=False, timeout=10)
        shutil.rmtree(socket_dir, ignore_errors=True)
