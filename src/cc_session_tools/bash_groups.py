"""Group Bash commands by what they are for: tests, builds, waiting, git, reading files, and so on.

bash-classify parses a command into its invocations and unwraps launchers such as ``timeout``, ``env``, ``nice``,
``systemd-run``, ``xargs`` and ``uv run``. This module maps each invocation to a group and gives the whole command the
group of its highest-priority invocation, so ``cd x && ./gradlew test 2>&1 | tail`` is a test. An invocation that
waits through a ``--wait`` or ``--watch`` option, a ``wait`` or ``watch`` subcommand, or a program named ``*-watch`` is
a wait, unless its program makes it a test, build, lint, format, git, search or file command.

The built-in rules know widely used public tools only. A user file, ``~/.config/cc-session-tools/bash-groups.toml``,
maps any other program, and any script path, to a group, and overrides a built-in rule::

    [programs]
    my-db-cli = "db"

    [scripts]
    "*/deploy/*.sh" = "cloud"
"""

from __future__ import annotations

import fnmatch
import itertools
import os
import re
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from bash_classify import CommandResult, InnerCommandResult, classify_expression, load_database

GROUPS: tuple[str, ...] = (
    "test",
    "build",
    "lint",
    "format",
    "wait",
    "run",
    "db",
    "cloud",
    "gh-glab",
    "package",
    "git",
    "agent-coord",
    "ext-cli",
    "search",
    "file-edit",
    "file-read",
    "other",
)
"""Every group, highest priority first. A command with invocations of several groups gets the first of them."""

_PRIORITY = {group: index for index, group in enumerate(GROUPS)}

OTHER_BUILD = "other-build"
"""``waits_on`` of a poll on a build or test tool's process, which may belong to another session."""


@dataclass(frozen=True)
class BashClass:
    """What one Bash command was for."""

    group: str
    """The group of the invocation that decides, see :data:`GROUPS`."""
    secondary: tuple[str, ...] = ()
    """The groups of the command's other invocations, in priority order, without :attr:`group`."""
    program: str | None = None
    """The program of the deciding invocation, after its launchers, such as ``gradlew`` or ``pytest``."""
    targeted: bool | None = None
    """For a test: ``True`` when it runs selected tests, ``False`` for a whole suite, ``None`` when it can't tell."""
    waits_on: str | None = None
    """:data:`OTHER_BUILD` for a poll on a build or test tool's process; ``None`` otherwise."""
    error: str | None = None
    """The parser's warning, such as a syntax error, when it had one. A command it can't read at all is ``other``."""


def group_label(found: BashClass) -> str:
    """The group as ``tool-stats`` and the JSON of ``timeline`` name it: a test is ``test (targeted)`` or ``test
    (full)`` when it can tell which."""
    if found.group != "test" or found.targeted is None:
        return found.group
    return "test (targeted)" if found.targeted else "test (full)"


class BashGroupsError(Exception):
    """The user file of Bash groups is invalid. The message names the file and the line."""


@dataclass(frozen=True)
class UserGroups:
    """The user's own program and script groups, from :func:`user_groups_path`."""

    programs: Mapping[str, str] = field(default_factory=dict)
    """Program name, as the basename of the command, to group."""
    scripts: tuple[tuple[str, str], ...] = ()
    """``fnmatch`` pattern of a script path, as written or with the home dir as ``~``, to group; first match wins."""


def user_groups_path() -> Path:
    return Path.home() / ".config" / "cc-session-tools" / "bash-groups.toml"


def load_user_groups(path: Path | None = None) -> UserGroups:
    """Read the user file; no file means no user groups.

    Raises:
        BashGroupsError: the file is not valid TOML, has a key or table other than ``[programs]`` and ``[scripts]``,
            or maps something to a value that is not the name of a group.
    """
    path = path or user_groups_path()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return UserGroups()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise BashGroupsError(f"{path}: {e}") from e
    lines = text.splitlines()
    for name, value in data.items():
        if name not in ("programs", "scripts"):
            kind = f"table [{name}]" if isinstance(value, dict) else f"key {name!r}"
            raise BashGroupsError(
                f"{path}:{_line_of(lines, name)}: unknown {kind}; the tables are [programs] and [scripts]"
            )
    tables: dict[str, dict[str, str]] = {}
    for table in ("programs", "scripts"):
        entries = data.get(table, {})
        if not isinstance(entries, dict):
            raise BashGroupsError(f"{path}:{_line_of(lines, table)}: {table} must be a table")
        for key, group in entries.items():
            where = f"{path}:{_line_of(lines, key, table)}"
            if not isinstance(group, str):
                raise BashGroupsError(f"{where}: the group of {key!r} in [{table}] must be a string, like 'db'")
            if group not in _PRIORITY:
                known = ", ".join(GROUPS)
                raise BashGroupsError(f"{where}: unknown group {group!r} for {key!r}; the groups are {known}")
        tables[table] = entries
    return UserGroups(programs=dict(tables["programs"]), scripts=tuple(tables["scripts"].items()))


def _line_of(lines: Sequence[str], key: str, table: str | None = None) -> int:
    """The 1-based number of the line that defines ``key`` (in ``table``), or 0 when no line does.

    The whole key must match, bare or quoted. It is defined by an assignment, a ``[table.key]`` header, a dotted
    ``table.key =`` assignment, or a ``key =`` inside an inline table. A key of ``table`` is looked for from the line
    that opens ``table`` on.
    """
    forms = [re.escape(f'"{key}"'), re.escape(f"'{key}'")]
    if re.fullmatch(r"[A-Za-z0-9_-]+", key):
        forms.append(re.escape(key))
    name = "(?:" + "|".join(forms) + ")"
    if table is None:
        patterns = [rf"^\s*{name}\s*=", rf"^\s*\[\s*{name}\s*[\].]", rf"^\s*{name}\s*\."]
        start = 1
    else:
        prefix = re.escape(table)
        patterns = [
            rf"^\s*{name}\s*=",
            rf"^\s*\[\s*{prefix}\s*\.\s*{name}\s*[\].]",
            rf"^\s*{prefix}\s*\.\s*{name}\s*[=.]",
            rf"(?<![\w\"'.-]){name}\s*=",
        ]
        start = _line_of(lines, table) or 1
    for pattern in patterns:
        for number in range(start, len(lines) + 1):
            if re.search(pattern, lines[number - 1]):
                return number
    return 0


# The built-in rules.

_TRIVIAL = frozenset(
    {"cd", "pushd", "popd", "echo", "printf", "export", "true", "false", "date", "break", "continue", "exit"}
    | {"return", "set", "unset", "local", "shift", "pwd", ":", "test", "[", "[[", "source", "."}
)
"""Builtins that never decide a command's group."""

_SIMPLE: dict[str, str] = {
    **dict.fromkeys(("pytest", "py.test", "vitest", "jest"), "test"),
    **dict.fromkeys(("mypy", "pyright", "eslint", "shellcheck", "tsc", "ty", "hadolint", "actionlint"), "lint"),
    **dict.fromkeys(("black", "prettier", "isort", "gofmt", "rustfmt", "shfmt"), "format"),
    **dict.fromkeys(("sleep", "ps", "watch"), "wait"),
    **dict.fromkeys(("java", "node", "bun", "deno", "ruby", "perl", "php"), "run"),
    **dict.fromkeys(("psql", "mysql", "sqlite3", "mongosh", "redis-cli", "snowsql", "clickhouse-client"), "db"),
    **dict.fromkeys(("gcloud", "gsutil", "bq", "aws", "kubectl", "terraform", "helm", "az", "podman"), "cloud"),
    **dict.fromkeys(("gh", "glab"), "gh-glab"),
    **dict.fromkeys(("pip", "pip3", "pipx", "poetry", "apt", "apt-get", "brew", "dnf"), "package"),
    **dict.fromkeys(("tmux",), "agent-coord"),
    **dict.fromkeys(("curl", "wget", "http"), "ext-cli"),
    **dict.fromkeys(("grep", "egrep", "fgrep", "rg", "ag", "ack", "fd"), "search"),
    **dict.fromkeys(("cp", "mv", "rm", "rmdir", "mkdir", "touch", "tee", "chmod", "chown", "ln"), "file-edit"),
    **dict.fromkeys(("patch", "tar", "unzip", "zip", "gzip", "gunzip", "truncate"), "file-edit"),
    **dict.fromkeys(
        ("cat", "head", "tail", "less", "ls", "wc", "stat", "file", "tree", "du", "jq", "yq", "diff", "cmp", "awk"),
        "file-read",
    ),
    **dict.fromkeys(
        ("sort", "uniq", "cut", "tr", "column", "nl", "readlink", "realpath", "basename", "dirname", "md5sum"),
        "file-read",
    ),
    **dict.fromkeys(("sha256sum", "xxd", "od", "which", "type", "strings", "comm", "paste"), "file-read"),
}

_NOT_A_PROJECT_CLI = frozenset(
    {"seq", "df", "free", "kill", "killall", "wait", "read", "trap", "id", "whoami", "command"}
)
"""Lowercase programs that are not someone's CLI, so they stay ``other`` instead of ``run``."""

_LOWERCASE_PROGRAM = re.compile(r"[a-z][a-z0-9_.+-]*")
_TIMED_SUBSHELL = re.compile(r"(^|[;&|(]\s*)time\s+(?:-p\s+)?(?=\()")
_LOOP = re.compile(r"\b(?:while|until|for)\b.*?\bdo\b", re.DOTALL)
_BUILD_TOOL = re.compile(r"gradle|pytest|mvn|maven|cargo|npm|tsc|webpack|vite", re.IGNORECASE)
"""A build or test tool in a process name or command line, such as ``GradleDaemon`` or ``GradleWrapperMain``."""
_GREPS = frozenset({"grep", "egrep", "fgrep", "rg"})
_GRADLE_TEST = re.compile(r"(?:^|:)(?:test|check|integrationTest|\w*Test|\w*ITest)$")
_GRADLE_CHECK = re.compile(r"(?:^|:)check$")
_GRADLE_LINT = re.compile(r"(?:^|:)(?:spotlessCheck|detekt|ktlintCheck|lint|checkstyle\w*|pmd\w*)$")
_GRADLE_FORMAT = re.compile(r"(?:^|:)(?:spotlessApply|ktlintFormat)$")
_MVN_SKIP_TESTS = frozenset({"-DskipTests", "-Dmaven.test.skip"})
_INTERPRETERS = re.compile(r"(?:python[0-9.]*|bash|sh|zsh|node|bun|deno|ruby|perl)")
_PYTEST_VALUE_OPTIONS = frozenset(
    {"-n", "-p", "-o", "-c", "-m", "-r", "-W", "--numprocesses", "--maxprocesses", "--dist", "--timeout"}
    | {"--rootdir", "--confcutdir", "--basetemp", "--config-file", "--override-ini", "--pythonwarnings"}
    | {"--deselect", "--ignore", "--ignore-glob", "--maxfail", "--durations", "--durations-min", "--tb"}
    | {"--junitxml", "--junit-xml", "--cov-report", "--cov-config", "--log-level", "--log-cli-level", "--log-file"}
    | {"--import-mode", "--capture", "--color", "--reruns", "--count", "--randomly-seed", "--html"}
)
"""pytest options that take their value as the next argument, so the value is not a test path."""
_UVX_VALUE_OPTIONS = frozenset(
    {"--from", "--with", "-w", "--with-editable", "--with-requirements", "--python", "-p", "--index"}
    | {"--index-url", "--default-index", "--extra-index-url", "--find-links", "-f", "--directory", "--env-file"}
)
"""``uvx`` options that take their value as the next argument. bash-classify does not unwrap ``uvx``."""


@dataclass(frozen=True)
class _Group:
    group: str
    targeted: bool | None = None
    waits_on: str | None = None


_Invocation = CommandResult | InnerCommandResult


class BashGrouper:
    """Groups Bash commands with the built-in rules and ``user``'s, caching each command string's result."""

    def __init__(self, user: UserGroups | None = None) -> None:
        self.user = user or UserGroups()
        self._database = load_database()
        self._cache: dict[str, BashClass] = {}
        self._home = str(Path.home())

    def classify(self, command: str) -> BashClass:
        found = self._cache.get(command)
        if found is None:
            found = self._cache[command] = self._classify(command)
        return found

    def _classify(self, command: str) -> BashClass:
        # bash-classify reads ``time (…)`` as a program named ``(…``; without ``time`` it is a plain subshell.
        command = _TIMED_SUBSHELL.sub(r"\1", command)
        try:
            result = classify_expression(command, self._database, sensitive_rules=[])
        except (ValueError, RecursionError) as e:
            return BashClass("other", error=f"{type(e).__name__}: {e}")
        error = result.parse_warnings[0] if result.parse_warnings else None
        decided: list[tuple[_Group, str]] = []
        trivial: list[str] = []
        grepped: list[str] = []
        for invocation in _leaves(result.commands):
            program = _program(invocation.argv)
            if program in _TRIVIAL:
                trivial.append(program)
                continue
            if program in _GREPS:
                grepped += invocation.argv[1:]
            decided.append(self._group(invocation, program))
        writes_a_file = bool(result.write_paths)
        if not decided:
            group = "file-edit" if writes_a_file else "other"
            return BashClass(group, program=trivial[0] if trivial else None, error=error)

        best, program = min(decided, key=lambda d: _PRIORITY[d[0].group])
        group = best.group
        if _LOOP.search(command) and any(g.group == "wait" for g, _p in decided):
            best, program = next((g, p) for g, p in decided if g.group == "wait")
            group = "wait"
        elif writes_a_file and group in ("file-read", "other"):
            group = "file-edit"  # a reader whose output goes to a file, such as `cat > notes.md <<EOF`
        waits_on = None
        if group == "wait":
            waits_on = next((g.waits_on for g, _p in decided if g.waits_on), None)
            # `ps aux | grep [g]radle`: the brackets only keep grep from finding itself.
            polls_ps = any(p == "ps" for _g, p in decided)
            if waits_on is None and polls_ps and _BUILD_TOOL.search(re.sub(r"[\[\]]", "", " ".join(grepped))):
                waits_on = OTHER_BUILD
        secondary = sorted({g.group for g, _p in decided} - {group}, key=_PRIORITY.__getitem__)
        targeted = best.targeted if group == "test" else None
        return BashClass(group, tuple(secondary), program, targeted, waits_on, error)

    def _group(self, invocation: _Invocation, program: str) -> tuple[_Group, str]:
        """The group of one invocation, and the program that decided it."""
        path = list(invocation.command)
        argv = list(invocation.argv)
        positionals = list(invocation.positionals or [])
        # bash-classify resolves `uv run ruff` and `npx vitest` as one command path; group the program they run.
        if path[:2] == ["uv", "run"] and len(path) > 2 or path[:1] == ["npx"] and len(path) > 1:
            launched = path[2] if path[0] == "uv" else path[1]
            argv = argv[argv.index(launched) :] if launched in argv else [launched]
            path, program = [launched], launched
        elif path == ["uvx"]:
            argv = _uvx_tool(argv)
            program = _program(argv).split("@", 1)[0] if argv else "uvx"
            path, positionals = [program], []
        user = self._user_group(program, argv)
        if user is not None:
            return _Group(user), program
        found = _builtin_group(program, path, argv, positionals)
        if found.group in _LIFTED_TO_WAIT and _waits(program, path, argv):
            return _Group("wait"), program
        return found, program

    def _user_group(self, program: str, argv: list[str]) -> str | None:
        found = self.user.programs.get(program)
        if found is not None or not self.user.scripts or not argv:
            return found
        scripts = [argv[0]]
        if _INTERPRETERS.fullmatch(program):
            scripts += [a for a in argv[1:] if not a.startswith("-")][:1]
        for script in scripts:
            forms = {script, _home_as_tilde(script, self._home)}
            for pattern, group in self.user.scripts:
                if any(fnmatch.fnmatchcase(form, pattern) for form in forms):
                    return group
        return None


def _leaves(invocations: Sequence[_Invocation]) -> Iterator[_Invocation]:
    """The invocations that run something themselves: a launcher is replaced by what it launches, except
    ``command -v``, which only looks a program up."""
    for invocation in invocations:
        if invocation.inner_commands and not _is_lookup(invocation.argv):
            yield from _leaves(invocation.inner_commands)
        else:
            yield invocation


def _is_lookup(argv: Sequence[str]) -> bool:
    return _program(argv) == "command" and any(a in ("-v", "-V") for a in argv[1:2])


def _uvx_tool(argv: list[str]) -> list[str]:
    """The argv of the tool ``uvx`` runs, after its own options."""
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        i += 2 if argv[i] in _UVX_VALUE_OPTIONS else 1
    return argv[i:]


def _program(argv: Sequence[str]) -> str:
    return os.path.basename(argv[0].strip("\"'")) if argv else ""


def _home_as_tilde(path: str, home: str) -> str:
    return "~" + path[len(home) :] if path == home or path.startswith(home + "/") else path


def _builtin_group(program: str, path: list[str], argv: list[str], positionals: list[str]) -> _Group:
    args = argv[1:]
    words = path[1:] + positionals
    sub = path[1] if len(path) > 1 else next((a for a in [*positionals, *args] if not a.startswith("-")), "")
    if program.startswith("$"):
        return _Group("run")  # a program held in a variable, such as "$PY" script.py
    if program in ("gradle", "gradlew"):
        return _gradle(path, argv, positionals)
    if program in ("mvn", "mvnw"):
        return _maven(words, args)
    if program in ("pytest", "py.test"):
        return _Group("test", _pytest_targeted(args))
    if re.fullmatch(r"python[0-9.]*", program):
        return _python(args)
    if program == "uv":
        if sub in ("sync", "add", "remove", "lock", "pip", "tool", "venv", "export"):
            return _Group("package")
        return _Group("build" if sub == "build" else "run")
    if program in ("npm", "pnpm", "yarn"):
        if any(w == "test" or w.startswith("test:") for w in words):
            return _Group("test")
        if any(w == "build" or w.startswith("build:") for w in words):
            return _Group("build")
        if any(w == "lint" for w in words):
            return _Group("lint")
        return _Group("package" if sub in ("install", "i", "ci", "add", "remove") else "run")
    if program in ("cargo", "go"):
        kinds = {"test": "test", "build": "build", "fmt": "format", "clippy": "lint", "vet": "lint"}
        return _Group(kinds.get(sub, "run"))
    if program == "make":
        targets = [a for a in args if not a.startswith("-")]
        if any("test" in t for t in targets):
            return _Group("test")
        return _Group("lint" if any(t in ("lint", "check", "validate") for t in targets) else "build")
    if program == "ruff":
        return _Group("format" if sub == "format" else "lint")
    if program == "git":
        return _Group("search" if sub == "grep" else "git")
    if program in ("docker", "podman"):
        return _Group("build" if "build" in path[1:3] else "cloud")
    if program == "claude":
        return _Group("lint" if args[:2] == ["plugin", "validate"] else "agent-coord")
    if program == "cc-session-tools":
        return _Group("wait" if args[:1] == ["watch"] and not _ASKS_FOR_HELP & set(args) else "agent-coord")
    if program in ("pgrep", "pidwait"):
        return _Group("wait", waits_on=OTHER_BUILD if any(_BUILD_TOOL.search(a) for a in args) else None)
    if program == "tail" and any(_follows(a) for a in args):
        return _Group("wait")  # follows a log, often until a `grep -m1` sees the line it waits for
    if program == "sed":
        return _Group("file-edit" if any(a.startswith(("-i", "--in-place")) for a in args) else "file-read")
    if program == "find":
        edits = any(a in ("-delete", "-exec", "-execdir", "-ok") for a in args)
        return _Group("file-edit" if edits else "search")
    if program in _SIMPLE:
        return _Group(_SIMPLE[program])
    if program in ("bash", "sh", "zsh") or program.endswith((".sh", ".py")):
        return _Group("run")
    if _LOWERCASE_PROGRAM.fullmatch(program) and program not in _NOT_A_PROJECT_CLI:
        return _Group("run")  # an unknown program: most often a project's own CLI
    return _Group("other")


_LIFTED_TO_WAIT = frozenset(GROUPS[_PRIORITY["wait"] + 1 :]) - {"git", "search", "file-edit", "file-read"}
"""The groups an invocation that waits leaves for ``wait``. A test, build or linter with ``--watch`` stays what it is,
and so does ``git log --follow`` or a search for ``--wait``."""
_WAIT_WORDS = frozenset({"wait", "watch"})
_ASKS_FOR_HELP = frozenset({"--help", "-h", "--version"})


def _waits(program: str, path: list[str], argv: list[str]) -> bool:
    """Whether an invocation waits on something: a program named ``*-watch``, a ``wait`` or ``watch`` subcommand, or an
    option ``--wait``, ``--watch``, ``--wait-*`` or ``--watch-*`` before any bare ``--``.

    Not ``--waiting-for`` or ``--no-wait``, and not an invocation that asks for ``--help``, ``-h`` or ``--version``.
    The subcommands are the command path bash-classify knows, or for a program it doesn't know, the words before the
    first option."""
    args = argv[1:]
    if _ASKS_FOR_HELP & set(itertools.takewhile(lambda a: a != "--", args)):
        return False
    if program.endswith("-watch"):
        return True
    words = path[1:] if len(path) > 1 else list(itertools.takewhile(lambda a: not a.startswith("-"), args))
    if _WAIT_WORDS & set(words):
        return True
    for arg in itertools.takewhile(lambda a: a != "--", args):
        name = arg.split("=", 1)[0]
        if name in ("--wait", "--watch") or name.startswith(("--wait-", "--watch-")):
            return True
    return False


def _follows(arg: str) -> bool:
    if arg == "--follow" or arg.startswith("--follow="):
        return True
    return arg.startswith("-") and not arg.startswith("--") and arg[1:].isalpha() and bool(set(arg[1:]) & {"f", "F"})


def _option_values(argv: list[str], *names: str) -> list[str]:
    """The values of the options ``names`` in ``argv``, given as ``--name value`` or ``--name=value``."""
    values = []
    for i, arg in enumerate(argv):
        if arg in names and i + 1 < len(argv):
            values.append(argv[i + 1])
        else:
            values += [arg.split("=", 1)[1] for name in names if name.startswith("--") and arg.startswith(f"{name}=")]
    return values


def _gradle(path: list[str], argv: list[str], positionals: list[str]) -> _Group:
    # bash-classify drops the tasks `-x` excludes, but the values of `--tests` still show up as positionals.
    filters = set(_option_values(argv, "--tests"))
    excluded = set(_option_values(argv, "-x", "--exclude-task"))
    tasks = [t for t in path[1:] + positionals if t not in filters and t not in excluded]
    # Without its tests, `check` runs only the verification tasks: linters and static analysis.
    tests_excluded = any(_GRADLE_TEST.search(t) and not _GRADLE_CHECK.search(t) for t in excluded)
    checks = [t for t in tasks if _GRADLE_CHECK.search(t)] if tests_excluded else []
    tests = [t for t in tasks if _GRADLE_TEST.search(t) and t not in checks]
    if tests:
        return _Group("test", bool(filters) or all(":" in t for t in tests))
    if checks or any(_GRADLE_LINT.search(t) for t in tasks):
        return _Group("lint")
    if any(_GRADLE_FORMAT.search(t) for t in tasks):
        return _Group("format")
    return _Group("build")


def _maven(words: list[str], args: list[str]) -> _Group:
    if not any(w in ("test", "verify", "integration-test") for w in words) or any(_skips_tests(a) for a in args):
        return _Group("build")
    selected = any(a.startswith(("-Dtest=", "-Dit.test=")) for a in args)
    return _Group("test", selected or bool(_option_values(args, "-pl", "--projects")))


def _skips_tests(arg: str) -> bool:
    """``-DskipTests`` or ``-Dmaven.test.skip``, unless set to ``false``."""
    name, _, value = arg.partition("=")
    return name in _MVN_SKIP_TESTS and value != "false"


def _pytest_targeted(args: list[str]) -> bool:
    """Selected tests: ``-k``, a ``::`` node id, a ``.py`` file, or a path other than the ``tests`` dir."""
    paths: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        if arg.startswith("-k") or arg == "--keyword" or arg.startswith("--keyword="):
            return True
        if arg in _PYTEST_VALUE_OPTIONS:
            i += 2
            continue
        if not arg.startswith("-"):
            paths.append(arg)
        i += 1
    if any("::" in p or p.endswith(".py") for p in paths):
        return True
    return any(p.rstrip("/") not in ("tests", "test", ".") for p in paths)


def _python(args: list[str]) -> _Group:
    if args[:1] == ["-c"] and len(args) > 1:
        return _Group("wait" if "sleep(" in args[1] else "run")
    if args[:1] == ["-m"] and len(args) > 1:
        module, rest = args[1], args[2:]
        if module == "pytest":
            return _Group("test", _pytest_targeted(rest))
        return _builtin_group(module, [module], [module, *rest], [])
    return _Group("run")


# Repeated commands.

_CD_PREFIX = re.compile(r"^\s*cd\s+(?:\"[^\"]*\"|'[^']*'|\S+)\s*(?:&&|;)\s*")
_DIGITS = re.compile(r"\d{4,}")
_NUMBER = r"\d+(?:\.\d+)?"
_WAIT_ARGUMENT = re.compile(rf"\b(timeout|sleep)(\s+(?:-[ks]\s+\S+\s+|-\S+\s+)*){_NUMBER}[smhd]?(?![\w.])")
_SLEEP_CALL = re.compile(rf"\bsleep\(\s*{_NUMBER}\s*\)")
_SEQ_ARGUMENTS = re.compile(rf"\bseq((?:\s+-?{_NUMBER})+)(?![\w.])")
_SCRATCHPAD = re.compile(
    r"\S*/claude-\d+/[^/\s]+/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/scratchpad"
)


def normalize(command: str, *, home: str | None = None, scratchpad: str | None = None) -> str:
    """The form of ``command`` that its repeats share.

    It drops leading ``cd X &&`` prefixes, writes the session's scratchpad dir as ``<scratchpad>`` and the home dir as
    ``~``, and replaces every run of 4 or more digits with ``N``, and so the numbers of MRs and issues too. The
    durations of ``timeout`` and ``sleep`` (also ``time.sleep(…)``) and the numbers of ``seq`` become ``N`` whatever
    their length, so polls that only wait longer share a row. ``home`` defaults to the current user's.
    ``scratchpad`` is the session's scratchpad dir; without it any path shaped like a Claude Code scratchpad
    (``…/claude-<uid>/<project>/<session id>/scratchpad``) is replaced.
    """
    text = command.strip()
    while (match := _CD_PREFIX.match(text)) is not None:
        text = text[match.end() :]
    if scratchpad:
        text = text.replace(scratchpad.rstrip("/"), "<scratchpad>")
    text = _SCRATCHPAD.sub("<scratchpad>", text)
    home = (home if home is not None else str(Path.home())).rstrip("/")
    if home:
        text = re.sub(re.escape(home) + r"(?![\w.-])", "~", text)
    text = _WAIT_ARGUMENT.sub(r"\1\2N", text)
    text = _SLEEP_CALL.sub("sleep(N)", text)
    text = _SEQ_ARGUMENTS.sub(lambda m: "seq" + " N" * len(m.group(1).split()), text)
    return _DIGITS.sub("N", text)
