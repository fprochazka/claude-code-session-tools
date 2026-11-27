from __future__ import annotations

import pytest

from cc_session_tools.redact import redact, redacting_session, session_paths, tokens

HOME = "/home/user"
SESSION = "0b1c2d3e-0000-4000-8000-000000000000"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            f"cat /tmp/claude-1000/-home-user-projects-example/{SESSION}/scratchpad/notes.md",
            "cat <scratchpad>/notes.md",
        ),
        (
            f"tail /tmp/claude-1000/-home-user-projects-example/{SESSION}/tasks/b1x.output",
            "tail <claude-tmp>/tasks/b1x.output",
        ),
        (f"ls {HOME}/.cache/claude-tmp/claude-1000/-home-user-x/{SESSION}/scratchpad", "ls <scratchpad>"),
        ("ls ~/.claude/projects/-home-user-other/", "ls ~/.claude/projects/~-other/"),
        ("a -home-username-x b", "a -home-username-x b"),
        (f"cat {HOME}/.bashrc {HOME}name/x", "cat ~/.bashrc /home/username/x"),
        ("use claude-3 models", "use claude-3 models"),
    ],
)
def test_home_and_the_claude_temp_dir_are_redacted(text: str, expected: str) -> None:
    assert redact(text, home=HOME, paths=()) == expected


def test_the_session_cwd_and_its_repo_print_as_repo() -> None:
    paths = session_paths(f"{HOME}/projects/example/.worktrees/feature", home=HOME)
    assert paths == (f"{HOME}/projects/example/.worktrees/feature", f"{HOME}/projects/example")
    text = (
        f"cd {HOME}/projects/example/.worktrees/feature && cat {HOME}/projects/example/README.md"
        f" ~/.claude/projects/-home-user-projects-example--worktrees-feature/x {HOME}/projects/example-other"
    )
    assert redact(text, home=HOME, paths=paths) == (
        "cd <repo> && cat <repo>/README.md ~/.claude/projects/<repo>/x ~/projects/example-other"
    )


def test_a_legacy_worktree_dir_names_its_repo() -> None:
    assert session_paths(f"{HOME}/projects/example-worktrees/feature", home=HOME)[-1] == f"{HOME}/projects/example"


def test_the_home_dir_and_its_parents_are_never_the_repo() -> None:
    assert session_paths(HOME, home=HOME) == ()
    assert session_paths("/home", home=HOME) == ()
    assert session_paths(None) == ()


def test_the_session_paths_apply_inside_the_block_only() -> None:
    with redacting_session(f"{HOME}/projects/example"):
        assert redact(f"{HOME}/projects/example/a.py", home=HOME) == "<repo>/a.py"
    assert redact(f"{HOME}/projects/example/a.py", home=HOME) == "~/projects/example/a.py"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ssh db1.example.internal uptime", "ssh <host> uptime"),
        ("psql -h pg.prod.example.com -U app", "psql -h <host> -U app"),
        ("curl api.example.io:8080/health", "curl <host>:8080/health"),
        ("git clone git@example.org:team/repo.git", "git clone git@<host>:team/repo.git"),
        ("see example.com.", "see <host>."),
        ("cat pyproject.toml a.py README.md setup.cfg", "cat pyproject.toml a.py README.md setup.cfg"),
        ("docker build -f Dockerfile.dev docker/app.dev", "docker build -f Dockerfile.dev docker/app.dev"),
        ("java org.example.app.Main", "java org.example.app.Main"),
        ("ls ~/src/example.com/team/repo", "ls ~/src/example.com/team/repo"),
        ("psql -h db.dev -U app", "psql -h <host> -U app"),
        ("curl api.example.dev:8080/health", "curl <host>:8080/health"),
        ("git push git@code.dev:team/repo.git", "git push git@<host>:team/repo.git"),
        ("ssh://deploy@build.example.internal/x", "ssh://deploy@<host>/x"),
        ("docker compose -f compose.dev up", "docker compose -f compose.dev up"),
        ("make build.dev", "make build.dev"),
        ("uvicorn server.app:app", "uvicorn server.app:app"),
        ("open hello.app notes.ai settings.local", "open hello.app notes.ai settings.local"),
        ("gsutil cp gs://my-bucket/a/b.txt .", "gsutil cp gs://<bucket>/a/b.txt ."),
        ("aws s3 cp s3://data-bucket/x.csv y.csv", "aws s3 cp s3://<bucket>/x.csv y.csv"),
    ],
)
def test_host_names_outside_urls_are_redacted(text: str, expected: str) -> None:
    assert redact(text, home=HOME, paths=()) == expected


@pytest.mark.parametrize(("count", "expected"), [(0, "0"), (812, "812"), (1500, "2k"), (23_200_000, "23.2M")])
def test_token_counts(count: int, expected: str) -> None:
    assert tokens(count) == expected
