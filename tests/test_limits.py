from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from cc_session_tools.cli import app
from cc_session_tools.limits_cmd import duration_text

runner = CliRunner()
SESSION = "0b6f2c1e-1111-4222-8333-444455556666"


@pytest.fixture
def home(isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("COLUMNS", "200")
    (isolated_home / ".claude").mkdir()
    (isolated_home / ".claude" / "auth-switch.yaml").write_text("profiles:\n  work: {}\n  lab: {}\n  vertex: {}\n")
    for name in ("work", "lab", "vertex"):
        (isolated_home / ".claude-profiles" / name).mkdir(parents=True)
    return isolated_home


def write_limits(home: Path, profile: str, **windows: dict[str, Any]) -> Path:
    now_ms = int(time.time() * 1000)
    data = {
        "v": 1,
        "profile": profile,
        "written_at": now_ms - 120_000,
        "session_id": SESSION,
        "model": "claude-opus-5-5",
        "claude_version": "2.1.325",
        "windows": {
            name: {**window, "observed_at": now_ms - 120_000, "changed_at": now_ms - 300_000, "session_id": SESSION}
            for name, window in windows.items()
        },
    }
    path = home / ".claude-profiles" / profile / "auth-switch-rate-limits.json"
    path.write_text(json.dumps(data))
    return path


def window(used: float, resets_in_s: float, **extra: Any) -> dict[str, Any]:
    return {"used_percentage": used, "resets_at": int(time.time() + resets_in_s), **extra}


def invoke(*args: str) -> Any:
    return runner.invoke(app, ["limits", *args])


def test_json_lists_every_profile_with_its_windows(home: Path) -> None:
    path = write_limits(home, "work", five_hour=window(23.5, 3600), seven_day=window(41, 3 * 86400))
    write_limits(home, "lab", five_hour=window(87, -600), seven_day=window(12, 86400))

    result = invoke("--json")

    assert result.exit_code == 0, result.output
    rows = {row["profile"]: row for row in json.loads(result.stdout)}
    assert list(rows) == ["default", "work", "lab", "vertex"]
    work = rows["work"]
    assert work["path"] == str(path)
    assert (work["session_id"], work["model"], work["claude_version"]) == (SESSION, "claude-opus-5-5", "2.1.325")
    assert work["windows"]["five_hour"]["used_percentage"] == 23.5
    assert work["windows"]["five_hour"]["reset"] is False
    assert work["windows"]["five_hour"]["resets_at"].endswith("+00:00")
    assert rows["lab"]["windows"]["five_hour"]["reset"] is True
    assert rows["vertex"] == {
        "profile": "vertex",
        "config_dir": str(home / ".claude-profiles" / "vertex"),
        "path": None,
        "measured_at": None,
        "written_at": None,
        "session_id": None,
        "model": None,
        "claude_version": None,
        "windows": None,
    }


def test_the_table_shows_each_window_its_reset_and_profiles_without_data(home: Path) -> None:
    write_limits(home, "work", five_hour=window(23.5, 3600 + 12 * 60 + 30), seven_day=window(91, 3 * 86400))
    write_limits(home, "lab", five_hour=window(87, -600), seven_day=window(12, 86400))

    result = invoke()

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].split() == ["Profile", "5h", "7d", "Measured"]
    work = next(line for line in lines if line.startswith("work"))
    assert "24% · resets in 1h12m (" in work
    assert "91% · resets in 3d (" in work
    assert "2m ago" in work
    lab = next(line for line in lines if line.startswith("lab"))
    assert "reset " in lab and "· was 87%" in lab
    assert "no data" in next(line for line in lines if line.startswith("vertex"))
    assert "Spend" not in result.stdout


def test_a_spend_limit_gets_its_own_column(home: Path) -> None:
    write_limits(home, "work", spend_limit=window(62.8, 10 * 86400, used_usd=314.12, limit_usd=500, period="monthly"))

    result = invoke("--profile", "work")

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[0].split() == ["Profile", "5h", "7d", "Spend", "Measured"]
    assert "63% · resets in 10d (" in result.stdout
    assert "$314.12 of $500.00" in result.stdout


def test_profile_filter_takes_globs_and_fails_when_nothing_matches(home: Path) -> None:
    write_limits(home, "work", five_hour=window(1, 3600))

    assert [row["profile"] for row in json.loads(invoke("--profile", "w*", "--json").stdout)] == ["work"]
    result = invoke("--profile", "acme*")
    assert result.exit_code == 1
    assert "No Claude Code profile matches 'acme*'" in result.stderr


@pytest.mark.parametrize(
    ("seconds", "text"),
    [
        (0, "0m"),
        (40 * 60, "40m"),
        (3600, "1h"),
        (3600 + 12 * 60, "1h12m"),
        (3 * 86400, "3d"),
        (2 * 86400 + 4 * 3600, "2d4h"),
    ],
)
def test_durations_are_short(seconds: float, text: str) -> None:
    assert duration_text(seconds) == text
