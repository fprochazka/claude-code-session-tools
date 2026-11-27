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
from cc_blackbox import project_slug

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


# A git call that inherits GIT_DIR, GIT_WORK_TREE or GIT_INDEX_FILE from a hook or a `git rebase --exec` acts on that
# repo instead of the one in its cwd: a `git init` in tmp_path once made the real checkout bare. Every test starts
# without any GIT_* variable but these.
_KEPT_GIT_ENV = ("GIT_CONFIG_NOSYSTEM",)


def _inherited_git_env(environ: dict[str, str] | os._Environ[str]) -> list[str]:
    return [name for name in environ if name.startswith("GIT_") and name not in _KEPT_GIT_ENV]


@pytest.fixture(autouse=True)
def no_inherited_git_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _inherited_git_env(os.environ):
        monkeypatch.delenv(name)


# A test identity, and no automatic maintenance: a commit would start it in the background, and it would write to the
# repo after the test step that caused it.
_GIT_CONFIG = ("-c", "user.name=t", "-c", "user.email=t@example.com", "-c", "maintenance.auto=false")


def git(*args: str, cwd: Path) -> None:
    """Run git in ``cwd`` with a test identity, and without the GIT_* variables a test may have set since it started."""
    env = dict(os.environ)
    for name in _inherited_git_env(env):
        del env[name]
    subprocess.run(["git", *_GIT_CONFIG, *args], cwd=cwd, check=True, env=env)


@pytest.fixture(autouse=True)
def isolated_home(request: pytest.FixtureRequest, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    if request.node.get_closest_marker("live") is not None:
        return Path.home()  # the live contract test runs against the real Claude Code setup
    for name in _REAL_SETUP_ENV:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    # Bare tmux then reaches a server in the test's own dir, never the user's default one.
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
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

    def session(self, profile: str, name: str | None, *, env: dict[str, str] | None = None, **fields: Any) -> int:
        """Start a sleeping process and write the record of a session it runs. ``env`` is its whole environment."""
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], env=env)
        self.processes[process.pid] = process
        return self.session_of(process.pid, profile, name, **fields)

    def session_of(self, pid: int, profile: str, name: str | None, **fields: Any) -> int:
        """Write the record of a session whose process already runs, such as the process of a tmux pane. The test that
        started the process stops it."""
        record: dict[str, Any] = {
            "pid": pid,
            "sessionId": sid(name or str(pid)),
            "cwd": str(self.home / "elsewhere"),
            "startedAt": STARTED_AT + (len(self.records) + 1) * 1000,
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

    def project_dir(self, profile: str) -> Path:
        """The project dir of the sessions' default ``elsewhere`` cwd, which holds their transcripts."""
        return self.config_dir(profile) / "projects" / project_slug(self.home / "elsewhere")

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


def response(epoch_seconds: float, *, model: str = "claude-opus-5-5", used: int | None = None) -> dict[str, Any]:
    """An assistant record: a model response at ``epoch_seconds``, with ``used`` context tokens when given."""
    message: dict[str, Any] = {"role": "assistant", "model": model, "content": [{"type": "text", "text": "ok"}]}
    if used is not None:
        message["usage"] = {"input_tokens": 2, "cache_read_input_tokens": used - 2, "output_tokens": 300}
    return {"type": "assistant", "timestamp": stamp(epoch_seconds), "message": message}


def compact_boundary(epoch_seconds: float) -> dict[str, Any]:
    """The record Claude Code writes when a session compacts its context."""
    return {"type": "system", "subtype": "compact_boundary", "timestamp": stamp(epoch_seconds)}


def compaction_records(
    boundary_uuid: str,
    *,
    timestamp: str,
    logical_parent: str | None = None,
    trigger: str = "auto",
    pre_tokens: int = 180_000,
    preserved_head: str | None = None,
    summary: str | None = "This session is being continued from a previous conversation. Summary: the plan so far.",
) -> list[dict[str, Any]]:
    """The records Claude Code writes when it compacts a session: the boundary, then the summary.

    ``preserved_head`` gives the boundary the ``preservedSegment`` that Claude Code from about 2.1.266 writes; without
    it the boundary has the older shape, with ``trigger`` and ``preTokens`` only. ``summary=None`` leaves the summary
    record out.
    """
    metadata: dict[str, Any] = {"trigger": trigger, "preTokens": pre_tokens}
    if preserved_head is not None:
        metadata["postTokens"] = pre_tokens // 10
        metadata["preservedSegment"] = {
            "headUuid": preserved_head,
            "anchorUuid": f"{boundary_uuid}-summary",
            "tailUuid": logical_parent,
        }
    records: list[dict[str, Any]] = [
        {
            "type": "system",
            "subtype": "compact_boundary",
            "uuid": boundary_uuid,
            "parentUuid": None,
            "logicalParentUuid": logical_parent,
            "timestamp": timestamp,
            "content": "Conversation compacted",
            "compactMetadata": metadata,
        }
    ]
    if summary is not None:
        records.append(
            {
                "type": "user",
                "uuid": f"{boundary_uuid}-summary",
                "parentUuid": boundary_uuid,
                "timestamp": timestamp,
                "isCompactSummary": True,
                "isVisibleInTranscriptOnly": True,
                "message": {"role": "user", "content": summary},
            }
        )
    return records


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


FAKE_TMUX_SERVER_PID = 4242
FAKE_TMUX_SOCKET = "/tmp/tmux-1000/default"


def pane_line(line: str, *, pane_pid: int | None = None) -> str:
    """A full ``list-panes`` line from a short one.

    A line of five fields, ``pane, session name, window index, window id, title``, gets the tmux session id ``$<name>``
    inserted before the title; a line of six gives it itself. Then ``pane_pid``, the server pid and the socket go in
    before the title. ``pane_pid`` defaults to the test process, which every session process of :class:`Machine`
    descends from, so the pane counts as the session's own.
    """
    fields = line.split("\t")
    if len(fields) == 5:
        fields = [*fields[:4], f"${fields[1]}", fields[4]]
    if len(fields) == 6:
        pid = os.getpid() if pane_pid is None else pane_pid
        fields = [*fields[:5], str(pid), str(FAKE_TMUX_SERVER_PID), FAKE_TMUX_SOCKET, fields[5]]
    return "\t".join(fields)


def fake_tmux(bin_dir: Path, panes: list[str] | None, *, exit_code: int = 0, pane_pid: int | None = None) -> None:
    """Install a ``tmux`` that prints ``panes`` (list-panes lines, see :func:`pane_line`) and exits with ``exit_code``.

    Calling it again changes what the installed ``tmux`` prints from then on.
    """
    output = "".join(f"{pane_line(line, pane_pid=pane_pid)}\n" for line in panes or [])
    data = bin_dir / "tmux-panes.txt"
    data.write_text(output)
    script = bin_dir / "tmux"
    script.write_text(f"#!/bin/sh\ncat '{data}'\nexit {exit_code}\n")
    script.chmod(0o755)


class RealTmux:
    """A private tmux server of its own, which every ``tmux`` call of the test reaches through ``$TMUX_TMPDIR``.

    Without ``label`` it is the server bare ``tmux`` reaches, the caller's server; with it, a second server started with
    ``tmux -L <label>`` in the same private dir.
    """

    def __init__(self, label: str | None = None) -> None:
        self.label = label

    def run(self, *args: str) -> str:
        server = ["-L", self.label] if self.label is not None else []
        result = subprocess.run(
            ["tmux", *server, "-f", os.devnull, *args], capture_output=True, text=True, check=True, timeout=10
        )
        return result.stdout.strip()

    @property
    def socket(self) -> str:
        """The server's socket, as ``#{socket_path}`` gives it; the server must run."""
        return self.run("display-message", "-p", "#{socket_path}")

    def new_session(self, name: str, command: str = "sleep 600") -> str:
        """Start a tmux session; return the pane id of its first window. ``command`` runs in the pane, under the
        ``TMUX`` and ``TMUX_PANE`` tmux sets for it."""
        return self.run("new-session", "-d", "-s", name, "-P", "-F", "#{pane_id}", command)

    def new_window(self, session: str) -> str:
        return self.run("new-window", "-d", "-t", f"{session}:", "-P", "-F", "#{pane_id}", "sleep 600")

    def move_window(self, pane: str, to_session: str) -> None:
        """Move the window that holds ``pane`` to another tmux session."""
        self.run("move-window", "-s", pane, "-t", f"{to_session}:")

    def rename_session(self, old: str, new: str) -> None:
        self.run("rename-session", "-t", old, new)

    def pane_pid(self, pane: str, program: str | None = None) -> int:
        """The process the pane runs.

        With ``program``, wait until that process runs ``program``: the environment a pane command such as
        ``exec env TMUX=… sleep 600`` sets shows in ``/proc/<pid>/environ`` only after the last exec.
        """
        pid = int(self.run("display-message", "-p", "-t", pane, "#{pane_pid}"))
        if program is None:
            return pid
        comm = Path(f"/proc/{pid}/comm")
        deadline = time.monotonic() + 5
        while (current := comm.read_text(encoding="utf-8").strip()) != program:
            if time.monotonic() > deadline:
                raise AssertionError(f"the process {pid} of pane {pane} runs {current!r}, not {program!r}")
            time.sleep(0.02)
        return pid

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
        for server in ([], ["-L", OTHER_TMUX_SERVER]):
            subprocess.run(["tmux", *server, "kill-server"], capture_output=True, check=False, timeout=10)
        shutil.rmtree(socket_dir, ignore_errors=True)


OTHER_TMUX_SERVER = "other"
"""The label of the second private server :func:`other_tmux` starts, next to :func:`real_tmux`'s."""


@pytest.fixture
def other_tmux(real_tmux: RealTmux) -> RealTmux:
    """A second private tmux server, ``tmux -L other`` in the same temp dir; :func:`real_tmux` kills it too."""
    return RealTmux(OTHER_TMUX_SERVER)


def log_start(project_dir: Path, session_id: str, ts_ms: int, **fields: Any) -> Path:
    """Append one start to the session's auth-switch start log, as claude-code-auth-switch's SessionStart hook does.

    ``fields`` add to or override the line, for example ``role``, ``role_prompt`` and ``role_prompt_mode``.
    """
    log = project_dir / session_id / "auth-switch-profile-starts.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    line = {"v": 1, "ts": ts_ms, "session_id": session_id, "source": "startup", **fields}
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({k: v for k, v in line.items() if v is not None}) + "\n")
    return log


def screen_tmux(
    bin_dir: Path,
    *,
    pane_pid: int,
    capture: str | None,
    panes: list[str],
    mode: str = "0",
    vanish: bool = False,
    cursor: str = "0 0",
    cursor_after: str | None = None,
) -> None:
    """Install a ``tmux`` that lists ``panes``, says the pane runs ``pane_pid``, and captures ``capture``.

    ``cursor`` is the pane's ``#{cursor_x} #{cursor_y}``.

    ``cursor_after`` is the cursor once the pane was captured, for a cursor that moves during the capture.

    ``mode`` is the pane's ``pane_in_mode``, or ``after-capture`` for a pane that enters copy mode while it is captured.
    ``vanish`` closes the pane when ``display-message`` asks about it, as if it closed after the listing.
    """
    panes_file = bin_dir / "tmux-panes.txt"
    panes_file.write_text("".join(f"{pane_line(line, pane_pid=pane_pid)}\n" for line in panes))
    capture_file = bin_dir / "tmux-capture.txt"
    capture_file.write_text(capture or "")
    captured = bin_dir / "tmux-captured"
    capture_exit = 1 if capture is None else 0
    before = f"{pane_pid} {'0' if mode == 'after-capture' else mode} {cursor}"
    after = f"{pane_pid} {'1' if mode == 'after-capture' else mode} {cursor_after or cursor}"
    if vanish:
        display = f": > '{panes_file}'; exit 1"
    else:
        display = f"if [ -e '{captured}' ]; then echo '{after}'; else echo '{before}'; fi"
    script = bin_dir / "tmux"
    script.write_text(
        "#!/bin/sh\n"
        # Every tmux server answers the same; -S and -L pick one before the command.
        'case "$1" in -S|-L) shift 2 ;; esac\n'
        'case "$1" in\n'
        f"  list-panes) cat '{panes_file}' ;;\n"
        f"  display-message) {display} ;;\n"
        f"  capture-pane) touch '{captured}'; cat '{capture_file}'; exit {capture_exit} ;;\n"
        "esac\n"
    )
    script.chmod(0o755)
