from __future__ import annotations

import uuid
from pathlib import Path

import pytest

# Variables that point cc-blackbox at the real Claude Code setup. Tests often run inside a Claude Code session, so
# every test starts without them and with an empty home dir, and reads only the projects dir it builds.
_REAL_SETUP_ENV = ("CLAUDE_CONFIG_DIR", "FP_CC_AUTH_SWITCH_PROFILE", "CLAUDE_PROJECTS_DIR")


@pytest.fixture(autouse=True)
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in _REAL_SETUP_ENV:
        monkeypatch.delenv(name, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    return home


def sid(name: str) -> str:
    """A stable session id for a readable fixture name. Claude Code names sessions only by UUID."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"session:{name}"))
