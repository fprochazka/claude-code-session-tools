import json
import subprocess
import sys

from cc_blackbox import ReadDiagnostics, iter_entries

from cc_session_tools.render import distill_entries
from cc_session_tools.stats import compute_stats, estimate_tokens, human_bytes


def _jsonl(*entries: dict) -> str:
    return "\n".join(json.dumps(e) for e in entries)


def test_estimate_tokens_uses_four_chars_per_token():
    assert estimate_tokens("") == 0
    assert estimate_tokens("a" * 4) == 1
    assert estimate_tokens("a" * 10) == round(10 / 4)


def test_human_bytes_scales_units():
    assert human_bytes(512) == "512 B"
    assert human_bytes(1536) == "1.5 KB"
    assert human_bytes(5 * 1024 * 1024) == "5.0 MB"


def test_compute_stats_on_small_fixture(tmp_path):
    text = _jsonl(
        {"type": "user", "cwd": "/repo", "timestamp": "t1", "message": {"content": "do the thing"}},
    )
    path = tmp_path / "sess.jsonl"
    path.write_text(text, encoding="utf-8")
    diagnostics = ReadDiagnostics()
    session = distill_entries("sess", list(iter_entries(path, diagnostics=diagnostics)), diagnostics)
    markdown = "# Session sess\nbody with a few words\n"

    stats = compute_stats(session, markdown)

    assert stats.prompt_count == 1
    assert stats.subagent_count == 0
    assert stats.char_count == len(markdown)
    assert stats.byte_size == len(markdown.encode("utf-8"))
    assert stats.estimated_tokens == round(len(markdown) / 4)


def test_stdout_mode_keeps_stats_off_stdout(tmp_path):
    entry = {"type": "user", "cwd": "/repo", "timestamp": "t1", "message": {"content": "hello world"}}
    session_file = tmp_path / "abc123.jsonl"
    session_file.write_text(_jsonl(entry), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from cc_session_tools.cli import main; main()",
            "distill",
            str(session_file),
            "--stdout",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    # stdout must be clean markdown; the stats block belongs only on stderr.
    assert result.stdout.startswith("# Session abc123")
    assert "Transcript stats" not in result.stdout
    assert "Transcript stats" in result.stderr
    assert "tokens" in result.stderr.lower()
