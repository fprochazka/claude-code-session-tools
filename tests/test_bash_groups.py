from __future__ import annotations

from pathlib import Path

import pytest

from cc_session_tools.bash_groups import (
    GROUPS,
    OTHER_BUILD,
    BashClass,
    BashGrouper,
    BashGroupsError,
    UserGroups,
    load_user_groups,
    normalize,
)


@pytest.fixture
def grouper(isolated_home: Path) -> BashGrouper:
    """A grouper made once the home dir is isolated, so the user's own bash-classify commands are never read."""
    return BashGrouper()


# (command, group, program, targeted)
RULES = [
    # test, with full vs targeted
    ("./gradlew test", "test", "gradlew", False),
    ("./gradlew check", "test", "gradlew", False),
    ("./gradlew :app:test", "test", "gradlew", True),
    ("gradle :app:test :lib:integrationTest", "test", "gradle", True),
    ("./gradlew :app:test test", "test", "gradlew", False),
    ("./gradlew clean test --tests 'com.example.FooTest'", "test", "gradlew", True),
    ("mvn -q verify", "test", "mvn", False),
    ("mvn -Dtest=FooTest test", "test", "mvn", True),
    ("pytest", "test", "pytest", False),
    ("pytest tests/", "test", "pytest", False),
    ("pytest -q tests/test_a.py", "test", "pytest", True),
    ("pytest tests/test_a.py::test_b", "test", "pytest", True),
    ("pytest -k parse", "test", "pytest", True),
    ("pytest tests/unit", "test", "pytest", True),
    ("python3 -m pytest tests", "test", "python3", False),
    ("npm test", "test", "npm", None),
    ("npm run test:unit", "test", "npm", None),
    ("npx vitest run", "test", "vitest", None),
    ("cargo test", "test", "cargo", None),
    ("go test ./...", "test", "go", None),
    ("make test", "test", "make", None),
    # build
    ("./gradlew build -x test", "build", "gradlew", None),
    ("./gradlew assemble", "build", "gradlew", None),
    ("mvn package", "build", "mvn", None),
    ("npm run build", "build", "npm", None),
    ("uv build", "build", "uv", None),
    ("cargo build --release", "build", "cargo", None),
    ("docker build -t example .", "build", "docker", None),
    ("make", "build", "make", None),
    # lint
    ("ruff check .", "lint", "ruff", None),
    ("mypy src", "lint", "mypy", None),
    ("python -m mypy src", "lint", "python", None),
    ("shellcheck scripts/a.sh", "lint", "shellcheck", None),
    ("./gradlew spotlessCheck", "lint", "gradlew", None),
    ("make lint", "lint", "make", None),
    ("claude plugin validate .", "lint", "claude", None),
    # format
    ("ruff format .", "format", "ruff", None),
    ("prettier --write src", "format", "prettier", None),
    ("./gradlew spotlessApply", "format", "gradlew", None),
    # wait
    ("sleep 60", "wait", "sleep", None),
    ("python3 -c 'import time; time.sleep(90)'", "wait", "python3", None),
    ("cc-session-tools watch --until turn_finished", "wait", "cc-session-tools", None),
    # run
    ("python3 scripts/report.py", "run", "python3", None),
    ("python3 - <<'EOF'\nprint(1)\nEOF", "run", "python3", None),
    ('"$PY" script.py', "run", "$PY", None),
    ("./run.sh --fast", "run", "run.sh", None),
    ("bash scripts/setup.sh", "run", "bash", None),
    ("node server.js", "run", "node", None),
    ("my-project-cli export --all", "run", "my-project-cli", None),
    # db, cloud, gh-glab, package
    ("psql -c 'select 1'", "db", "psql", None),
    ("kubectl get pods", "cloud", "kubectl", None),
    ("docker compose up -d", "cloud", "docker", None),
    ("gh pr view 12", "gh-glab", "gh", None),
    ("glab mr view 12", "gh-glab", "glab", None),
    ("uv sync --dev", "package", "uv", None),
    ("npm install", "package", "npm", None),
    ("pip install requests", "package", "pip", None),
    # git, agent-coord, ext-cli
    ("git status", "git", "git", None),
    ("git -C /home/user/projects/example log --oneline -5", "git", "git", None),
    ("tmux capture-pane -p -t %1", "agent-coord", "tmux", None),
    ("cc-session-tools search", "agent-coord", "cc-session-tools", None),
    ("curl -s https://example.com/api", "ext-cli", "curl", None),
    # search
    ("rg -n pattern src", "search", "rg", None),
    ("git grep -n pattern", "search", "git", None),
    ("find . -name '*.py'", "search", "find", None),
    # file-edit
    ("sed -i 's/a/b/' src/a.py", "file-edit", "sed", None),
    ("rm -rf build", "file-edit", "rm", None),
    ("mkdir -p out", "file-edit", "mkdir", None),
    ("find . -name '*.pyc' -delete", "file-edit", "find", None),
    ("cat > notes.md <<'EOF'\nhello\nEOF", "file-edit", "cat", None),
    # file-read
    ("cat README.md", "file-read", "cat", None),
    ("sed -n '1,20p' src/a.py", "file-read", "sed", None),
    ("ls -la", "file-read", "ls", None),
    ("jq '.a' data.json", "file-read", "jq", None),
    # other
    ("seq 1 10", "other", "seq", None),
    ("cd src", "other", "cd", None),
    # what the review found
    ("./gradlew test --tests=com.example.FooTest", "test", "gradlew", True),
    ("./gradlew check -x test", "lint", "gradlew", None),
    ("./gradlew check --exclude-task test", "lint", "gradlew", None),
    ("./gradlew check -x :app:test", "lint", "gradlew", None),
    ("./gradlew build -x test", "build", "gradlew", None),
    ("./gradlew testClasses", "build", "gradlew", None),
    ("mvn verify -DskipTests", "build", "mvn", None),
    ("mvn test -Dmaven.test.skip=true", "build", "mvn", None),
    ("mvn verify -DskipTests=false", "test", "mvn", False),
    ("mvn -pl core,api -am test", "test", "mvn", True),
    ("pytest -n 4 tests", "test", "pytest", False),
    ("pytest -p no:cacheprovider -o addopts= tests/", "test", "pytest", False),
    ("pytest --timeout 60 -m slow", "test", "pytest", False),
    ("pytest -c pytest.ini --rootdir . tests", "test", "pytest", False),
    ("pytest --deselect tests/test_a.py::test_b", "test", "pytest", False),
    ("pytest -kparse", "test", "pytest", True),
    ("pytest -k=parse tests", "test", "pytest", True),
    ("python -m ruff format .", "format", "python", None),
    ("python -m ruff check src", "lint", "python", None),
    ("uvx ruff check .", "lint", "ruff", None),
    ("uvx --from ruff==0.9 ruff format .", "format", "ruff", None),
    ("uvx pytest@8 tests/test_a.py", "test", "pytest", True),
    ("command -v pytest", "other", "command", None),
    ("tail -f build.log | grep -m1 'BUILD SUCCESSFUL'", "wait", "tail", None),
    ("tail -F -n 50 app.log", "wait", "tail", None),
    ("tail -n 50 app.log", "file-read", "tail", None),
]


@pytest.mark.parametrize(("command", "group", "program", "targeted"), RULES, ids=[r[0] for r in RULES])
def test_a_command_gets_the_group_of_its_rule(
    grouper: BashGrouper, command: str, group: str, program: str, targeted: bool | None
) -> None:
    found = grouper.classify(command)
    assert (found.group, found.program, found.targeted, found.error) == (group, program, targeted, None)


# (command, group, program): bash-classify unwraps the launcher, and the command it launches decides.
WRAPPERS = [
    ("timeout 600 ./gradlew test", "test", "gradlew"),
    ("env CI=1 make lint", "lint", "make"),
    ("nice -n 10 pytest", "test", "pytest"),
    ("stdbuf -oL pytest -q", "test", "pytest"),
    ("nohup ./run.sh &", "run", "run.sh"),
    ("sudo apt-get install jq", "package", "apt-get"),
    ("xargs -n1 rm < list.txt", "file-edit", "rm"),
    ("bash -c 'make test'", "test", "make"),
    ("rtk grep pattern src", "search", "grep"),
    ("uv run pytest -q", "test", "pytest"),
    ("uv run --frozen pytest -q", "test", "pytest"),
    ("uv run --only-group test pytest tests/test_a.py", "test", "pytest"),
    ("uv run --with-editable ../lib pytest tests", "test", "pytest"),
    ("uv run ruff check .", "lint", "ruff"),
    ("uv run mypy", "lint", "mypy"),
    ("uv run python scripts/report.py", "run", "python"),
    ("systemd-run --user --scope -p MemoryMax=4G ./gradlew :app:test", "test", "gradlew"),
    ("setsid ./gradlew build", "build", "gradlew"),
    ("ionice -c 3 pytest", "test", "pytest"),
    ("time ./gradlew test", "test", "gradlew"),
    ("cd x && time (PYTHONPATH=src ./.venv/bin/pytest -q)", "test", "pytest"),
    ("time -p (make build)", "build", "make"),
    ("systemd-run --user --scope -q ionice -c 3 nice -n 10 ./gradlew check", "test", "gradlew"),
]


@pytest.mark.parametrize(("command", "group", "program"), WRAPPERS, ids=[w[0] for w in WRAPPERS])
def test_a_launcher_is_grouped_by_what_it_launches(
    grouper: BashGrouper, command: str, group: str, program: str
) -> None:
    found = grouper.classify(command)
    assert (found.group, found.program) == (group, program)


def test_a_compound_command_gets_its_highest_priority_group_and_keeps_the_others(grouper: BashGrouper) -> None:
    found = grouper.classify("cd /home/user/projects/example && ./gradlew :app:test 2>&1 | grep FAIL | tail -20")
    assert found == BashClass("test", ("search", "file-read"), "gradlew", True)


def test_a_compound_command_of_lower_groups(grouper: BashGrouper) -> None:
    assert grouper.classify("git status && ls -la | head").group == "git"
    assert grouper.classify("git diff | grep -c '+' && sleep 5").group == "wait"


def test_a_poll_loop_waits_even_with_a_higher_group_inside(grouper: BashGrouper) -> None:
    found = grouper.classify("until ./gradlew :app:test -q; do sleep 30; done")
    assert (found.group, found.secondary) == ("wait", ("test",))


def test_a_loop_without_a_wait_keeps_the_priority_of_its_commands(grouper: BashGrouper) -> None:
    assert grouper.classify("for f in a b; do ruff check $f; done").group == "lint"


@pytest.mark.parametrize(
    "command",
    [
        "while pgrep -f gradle >/dev/null; do sleep 30; done",
        "pgrep -af pytest",
        "pidwait -f 'mvn verify'",
        "pgrep -f GradleDaemon",
        "while pgrep -f GradleWrapperMain; do sleep 10; done",
        "ps aux | grep '[g]radle'",
        "while ps -ef | grep -q '[p]ytest'; do sleep 5; done",
    ],
)
def test_a_poll_on_a_build_tool_waits_on_other_builds(grouper: BashGrouper, command: str) -> None:
    found = grouper.classify(command)
    assert (found.group, found.waits_on) == ("wait", OTHER_BUILD)


def test_a_poll_on_anything_else_waits_on_nothing_known(grouper: BashGrouper) -> None:
    found = grouper.classify("while ! test -f done.txt; do sleep 5; done")
    assert (found.group, found.waits_on) == ("wait", None)


@pytest.mark.parametrize("command", ["((", "'unterminated", "if [ -f x ]; then"])
def test_an_unparsable_command_is_other_with_the_reason(grouper: BashGrouper, command: str) -> None:
    found = grouper.classify(command)
    assert found.group == "other"
    assert found.error is not None and "syntax error" in found.error


def test_a_partly_parsed_command_keeps_its_group_and_the_parse_warning(grouper: BashGrouper) -> None:
    found = grouper.classify("pytest tests/test_a.py; if [ -f x ]; then")
    assert (found.group, found.program) == ("test", "pytest")
    assert found.error is not None and "syntax error" in found.error


def test_the_result_is_cached_per_command(grouper: BashGrouper) -> None:
    assert grouper.classify("ls") is grouper.classify("ls")


def test_every_group_has_a_priority() -> None:
    assert GROUPS[0] == "test" and GROUPS[-1] == "other" and len(set(GROUPS)) == len(GROUPS)


# The user file.


def _write(home: Path, text: str) -> Path:
    path = home / ".config" / "cc-session-tools" / "bash-groups.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_without_a_user_file_there_are_no_user_groups(isolated_home: Path) -> None:
    assert load_user_groups() == UserGroups()


def test_the_user_file_maps_programs_and_overrides_a_built_in_rule(isolated_home: Path) -> None:
    _write(isolated_home, '[programs]\nmy-db-cli = "db"\ncurl = "other"\n')
    grouper = BashGrouper(load_user_groups())
    assert grouper.classify("my-db-cli query 'select 1' | head").group == "db"
    assert grouper.classify("setsid my-db-cli query 'select 1'").group == "db"
    assert grouper.classify("curl -s https://example.com").group == "other"
    assert grouper.classify("uv run my-db-cli tables").group == "db"


def test_the_user_file_maps_script_paths(isolated_home: Path) -> None:
    _write(isolated_home, '[scripts]\n"~/tools/deploy/*.sh" = "cloud"\n"*/checks/*.py" = "lint"\n')
    grouper = BashGrouper(load_user_groups())
    assert grouper.classify(f"{isolated_home}/tools/deploy/push.sh --dry-run").group == "cloud"
    assert grouper.classify("python3 scripts/checks/style.py").group == "lint"
    assert grouper.classify("bash ~/tools/deploy/push.sh").group == "cloud"
    assert grouper.classify("python3 scripts/report.py").group == "run"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('[programs]\nmy-db-cli = "db"\nmy-cli = "databases"\n', ":3: unknown group 'databases' for 'my-cli'"),
        ('[programs]\nmy-cli = "db"\n\n[commands]\nx = "db"\n', ":4: unknown table [commands]"),
        ('[programs]\nmy-cli = "db\n', "line 2"),
        ('programs = "db"\n', ":1: programs must be a table"),
        ('[programs]\nmy-cli = "db"\nx = ["db"]\n', ":3: the group of 'x' in [programs] must be a string"),
        (
            '[programs]\nmy-cli = "db"\n\n[programs.sub]\na = "db"\n',
            ":4: the group of 'sub' in [programs] must be a string",
        ),
        ('programs = { my-cli = "db", other = 1 }\n', ":1: the group of 'other' in [programs] must be a string"),
        ('[scripts]\n"*/a.sh" = "db"\n\n[programs]\nx-y = "db"\nx = "nope"\n', ":6: unknown group 'nope' for 'x'"),
        ('[programs]\nfoo = "db"\n\n[scripts]\nfoo = "bad"\n', ":5: unknown group 'bad' for 'foo'"),
        ("extra = 1\n", ":1: unknown key 'extra'"),
    ],
)
def test_a_bad_user_file_is_an_error_that_names_the_file_and_line(isolated_home: Path, text: str, message: str) -> None:
    path = _write(isolated_home, text)
    with pytest.raises(BashGroupsError) as raised:
        load_user_groups()
    assert str(raised.value).startswith(str(path))
    assert message in str(raised.value)


# Repeated commands.


@pytest.mark.parametrize(
    ("command", "normalized"),
    [
        ("cd /home/user/projects/example && ./gradlew test", "./gradlew test"),
        ("cd 'a b'; cd c && ls", "ls"),
        ("cat /home/user/notes.md", "cat ~/notes.md"),
        ("ls /home/username", "ls /home/username"),
        ("sleep 3600 && tail -n 20000 log", "sleep N && tail -n N log"),
        (
            "cat /tmp/claude-1000/-home-user-projects-example/5f0c3a52-8d4e-4c1b-9a7e-2b6d1e0f4a11/scratchpad/a.txt",
            "cat <scratchpad>/a.txt",
        ),
        ("cat /home/user/.cache/tmp/sp/a.txt", "cat <scratchpad>/a.txt"),
        ("timeout 900 ./gradlew test", "timeout N ./gradlew test"),
        ("timeout -k 5 1800s ./gradlew test", "timeout -k 5 N ./gradlew test"),
        ("sleep 30; pgrep -af gradle", "sleep N; pgrep -af gradle"),
        ("python3 -c 'import time; time.sleep(90)'", "python3 -c 'import time; time.sleep(N)'"),
        ("for i in $(seq 1 19); do sleep 0.5; done", "for i in $(seq N N); do sleep N; done"),
        ("glab mr view 12345", "glab mr view N"),
        ("pytest -k test_15", "pytest -k test_15"),
    ],
)
def test_normalize_makes_repeats_alike(command: str, normalized: str) -> None:
    assert normalize(command, home="/home/user", scratchpad="/home/user/.cache/tmp/sp") == normalized
