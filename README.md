# claude-code-session-tools

Commands and skills for finding and distilling past Claude Code sessions, and for listing the running ones, with the `cc-session-tools` CLI.

The CLI has three subcommands. `search` lists and greps the sessions that belong to the directory you run it in. `distill` condenses one session transcript into markdown. `list-active` lists the sessions that run on the machine now, in every profile, with their live tmux pane.

## Why

Claude Code keeps every session as a JSONL transcript, and they pile up. When you come back to a piece of work weeks later, the earlier session holds the decisions you need, buried in a file too large to read and too large to paste into a fresh session.

`search` finds the session. `distill` reduces it to the prompts, the answers and the subagent results, so a follow-up session can read the outcome instead of replaying the history.

## Installation

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/).

There is no PyPI release, so install from a clone as an editable uv tool. The `cc-session-tools` command lands on your PATH, and edits in the clone take effect with no reinstall.

1. Clone the repository and enter it:

   ```bash
   git clone https://github.com/fprochazka/claude-code-session-tools.git
   cd claude-code-session-tools
   ```

2. Install the tool:

   ```bash
   uv tool install --editable .
   ```

3. Check it:

   ```bash
   cc-session-tools --version
   ```

To upgrade, pull the clone and reinstall:

```bash
git pull
uv tool install --editable . --reinstall
```

To remove it, run `uv tool uninstall claude-code-session-tools`.

## Claude Code plugin

This repository is also a [Claude Code](https://docs.anthropic.com/en/docs/claude-code) plugin marketplace. The plugin ships:

- `/session-tools:search <keywords or topic>` — mine past sessions for decisions and context, writing the findings to `./.claude/plans/past-sessions-<slug>.md`.
- `/session-tools:distill <session-id | latest>` — condense one session into a recap at `./.claude/plans/session-distill-<slug>.md`.
- The `session-tools` skill, which teaches Claude `search` and `distill` and how they scope their results.
- The `active-sessions` skill, which teaches Claude `list-active`: finding another running session, its profile, tmux pane and messaging address, through `--json` and `jq`.

Install the CLI first, then add the marketplace and the plugin:

```bash
claude plugin marketplace add fprochazka/claude-code-session-tools --scope user
claude plugin install session-tools@fprochazka-claude-code-session-tools --scope user
```

To upgrade after new commits land:

```bash
claude plugin marketplace update fprochazka-claude-code-session-tools
claude plugin update session-tools@fprochazka-claude-code-session-tools
```

## Usage

`search` and `distill` read transcripts. Claude Code stores each session at `<config dir>/projects/<encoded-cwd>/<session-id>.jsonl`, where `<encoded-cwd>` is the absolute launch directory with every non-alphanumeric character replaced by `-`. Both read the projects dir of every Claude Code profile on the machine and never write to it. Profiles that share a projects dir through a symlink are read once. Set `CLAUDE_PROJECTS_DIR` to read only that dir instead. Finding and parsing transcripts comes from the [cc-blackbox](https://github.com/fprochazka/cc-blackbox) library.

### search

```bash
cc-session-tools search                              # newest 20 sessions for this directory and its worktrees
cc-session-tools search -n 50                        # show up to 50
cc-session-tools search -n 0                         # no limit
cc-session-tools search --rg "N+1"                   # only sessions whose transcript contains this literal text
cc-session-tools search --rg worktree --rg deleted   # only sessions containing BOTH keywords
cc-session-tools search --rg worktree --rg deleted --rg-any   # sessions containing EITHER keyword
cc-session-tools search --all                        # every project, not just this directory
```

The table has four columns: **When** (time since the last interaction), **Session** (the full session id, ready to paste into `claude --resume`), **Name**, and **Worktree** (the session's path relative to the repo root, shown only inside a git repo). Under `--all` the last column becomes **Project** and shows each session's own directory. A dimmed name is a title derived from the first human message, not a name the session actually has. When the rows come from more than one projects dir, a **Profile** column before the last one names the profile each session belongs to.

**Which sessions you get.** Inside a git repo you get the main checkout and every worktree of it, including worktrees you have since deleted — their transcripts still exist, and membership never depends on the directory still being there. Outside a git repo you get only the sessions launched from the exact current directory.

**Ordering** comes from the last interaction timestamp inside the transcript, not from the file mtime. Claude Code appends titles and summaries to a transcript long after you stopped working in it, which bumps the mtime.

**`--rg` matches literal text**, not a regex, case-insensitively. It uses [ripgrep](https://github.com/BurntSushi/ripgrep) when it is on your PATH, and a built-in scan that is several times slower otherwise. A match inside a subagent transcript or a tool-result sidecar counts, and folds up into the parent session.

**Repeating `--rg` is AND at the level of the session.** Every keyword must appear somewhere in it, but they may sit in different messages, or one in the main transcript and another in a subagent it spawned. Each keyword costs its own ripgrep pass, because ripgrep is line-oriented and one JSONL line is one message record, so a single combined pattern could only find keywords sharing a record. `--rg-any` flips the same keywords to OR and costs one pass no matter how many you give it.

**Known limitation: a session belongs to the directory it started in.** The first `cwd` record in the transcript decides which repo the session belongs to. If you launched Claude Code one directory up and moved into the repo afterwards, a scoped `search` will not list that session, and `distill latest` will not consider it. `search --all` still finds it.

**`--all` reads every project.** It takes about 2 seconds for ~3,000 sessions with a warm disk cache, and adds one ripgrep pass per `--rg` keyword on top. Use the scoped default when you can.

Exit codes: `0`, including when nothing matched (the notice goes to stderr). `2` when ripgrep fails.

### distill

```bash
cc-session-tools distill latest                      # newest session of this repo and its worktrees
cc-session-tools distill <session-id>                # by session id, from anywhere
cc-session-tools distill <path-to>.jsonl             # by path to the transcript
cc-session-tools distill latest --stdout             # markdown on stdout instead of a file
cc-session-tools distill latest --include-thinking   # keep the assistant's thinking blocks
cc-session-tools distill latest --output-dir ./notes # write somewhere other than the default
```

The target is a `.jsonl` path, a session id, or the literal `latest`.

By default the command writes `<output-dir>/<session-id>.md` and prints that one path on stdout. The output directory defaults to `/tmp/claude-code-session-tools`. The stats table — prompt count, subagent count, size, estimated tokens — goes to stderr in both modes, so stdout stays either a single path or valid markdown.

`latest` uses the same scope as `search`: the newest session of the current repo and its worktrees, by transcript timestamp. Transcripts with no timestamped records — empty stubs — are skipped. There is no "newest session overall" fallback. If the scope is empty the command fails with exit `1` rather than distilling an unrelated session.

A session id is looked up across every project directory, so it works from anywhere. Subagent sidecars under `<session-id>/subagents/` are child transcripts, not sessions, and cannot be reached by id.

**A prompt's heading names who sent it:** `👤 User` for you, `📨 Peer session` for a message another Claude Code session sent, `🔔 Task notification` for a background task that finished. Prompts you typed while Claude was working are included.

**The rendered markdown is not reliably parseable by heading level.** Prompts and assistant replies keep their own markdown, so a distilled file's `## ` headings are not only `👤 User` and `🤖 Assistant` — any heading someone wrote inside the conversation shows up at the same level.

Exit code `1` when the target does not resolve or the file cannot be read.

### list-active

```bash
cc-session-tools list-active                          # every running session, one row each
cc-session-tools list-active --profile work --here    # sessions of one profile in this repo and its worktrees
cc-session-tools list-active --status busy --tmux     # busy sessions that run in tmux
cc-session-tools list-active --name 'w?-*'            # names matching a glob, case-insensitively
cc-session-tools list-active w1-order-planner         # every field of one session
cc-session-tools list-active --json | jq length       # every field of every session, as JSON
```

Claude Code keeps one registry record per running session in `<config dir>/sessions/<pid>.json`. `list-active` reads those of every profile through cc-blackbox, leaves out records whose process has ended, and lists a `sessions/` dir that several profiles share once, under the profile that runs each session.

The table has six columns: **Profile**, **Name** (`pid:<pid>` dimmed for a session without a name), **Status**, **Updated** (time since the record last changed, which Claude Code does on every status change), **Branch or cwd** (the git branch of the session's cwd, or the cwd with `~` when it is not on a branch), and **Tmux pane**. Rows are sorted by profile, then start time.

**The tmux pane is resolved live.** Claude Code records `<session>:@<window>.%<pane>` once, at startup. When you move the window to another tmux session, the record keeps the old session name. Only the pane id stays valid, so the command reads the current session and window of every pane with one `tmux list-panes -a` call. The table shows `%14 <session>:<window>`, `%14 (gone)` when the tmux server has no such pane, and `%14 (tmux unknown)` when tmux is missing or has no server. `tmux capture-pane`, `switch-client` and `send-keys` all take the pane id as a target.

**SESSION** matches an exact session id, an exact name, a pid, or a unique session id prefix, in that order. An all-digit SESSION is a pid when a live session has that pid. The filters apply before the lookup. No match or several matches exit with `1` and list the candidates on stderr.

**Filters** combine with AND. `--here` uses the scope of `search` and matches the session's current cwd.

**`--json`** prints an array, or one object with SESSION, with the same keys either way. The keys are a stable contract: snake_case, ISO 8601 timestamps, paths as strings, `null` for a missing value. They cover every field of the registry record (`raw` holds the record itself) plus derived ones: `tmux_pane`, `tmux_state`, `tmux_live`, `tmux_recorded`, `messaging_address`, `git_branch`, `git_worktree_root`, `git_repo_root` and `transcript_path`. The [active-sessions skill](coding-agent-plugins/claude-code/skills/active-sessions/SKILL.md) documents each key.

Exit codes: `0`, including when no session matches the filters (the notice goes to stderr, and `--json` prints `[]`). `1` when SESSION matches nothing or more than one session, or `--profile` names no profile. `2` for an unknown `--status`.

## Requirements

- Python 3.11+
- [ripgrep](https://github.com/BurntSushi/ripgrep) — optional. It makes `search --rg` faster; without it a built-in scan runs.
- git — only for worktree-aware scoping and the branch in `list-active`. Outside a repo every subcommand falls back to the exact current directory.
- tmux — optional. Without it `list-active` reports every tmux pane as unknown.

## Project structure

```
src/cc_session_tools/
├── __init__.py        # package version
├── cli.py             # root Typer app, --version, the error guard
├── search_cmd.py      # the `search` command
├── distill_cmd.py     # the `distill` command
├── list_active_cmd.py # the `list-active` command
├── active.py          # running sessions with the live tmux pane and git location, the JSON shape
├── console.py         # the console every table prints on
├── gitrepo.py         # git worktree resolution and the branch of a dir
├── discovery.py       # which sessions belong to a cwd, distill target resolution
├── titles.py          # display title derived from the first user message
├── search.py          # ripgrep shell-out with the built-in fallback
├── timefmt.py         # relative "time ago" formatting
├── render.py          # condensed conversation -> markdown
└── stats.py           # size and token estimates

tests/                # unit tests

.claude-plugin/marketplace.json          # Claude Code plugin marketplace
coding-agent-plugins/claude-code/        # the plugin
├── .claude-plugin/plugin.json
├── commands/{search,distill}.md
└── skills/{session-tools,active-sessions}/SKILL.md
```

## Development

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run ruff format .
```

Run the CLI from the clone without installing it with `uv run cc-session-tools …`.

## Releasing the plugin

The plugin version lives in two files and must stay in lockstep. Bump both when you change the plugin:

- `.claude-plugin/marketplace.json` (`plugins[0].version`)
- `coding-agent-plugins/claude-code/.claude-plugin/plugin.json` (`version`)

This version is independent of the Python package version in `pyproject.toml`.

The skills embed the `--help` output of the subcommands they cover. Regenerate those blocks with `COLUMNS=80` so the wrapping stays stable.

## License

MIT
