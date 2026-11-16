# claude-code-session-tools

Commands and skills for finding and distilling past Claude Code sessions, and for listing the running ones, with the `cc-session-tools` CLI.

The CLI has five subcommands. `search` lists and greps the sessions that belong to the directory you run it in. `distill` condenses one session transcript into markdown. `list-active` lists the sessions that run on the machine now, in every profile, with their live tmux pane. `inspect` shows every field of one session, running or ended. `watch` reports changes to the running sessions as JSON events, for an agent that waits for other sessions.

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
- The `session-watch` skill, which teaches Claude `watch`: waiting for other sessions to finish a turn, wait for their user or end, with the events arriving as messages, and never watching its own session.
- The `active-sessions` skill, which teaches Claude `list-active` and `inspect`: finding another session, running or ended, its profile, tmux pane, messaging address and resume command, through `--json` and `jq`.

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
cc-session-tools list-active --profile 'rohlik*'      # sessions of every profile whose name matches a glob
cc-session-tools list-active --status busy --tmux     # busy sessions that run in tmux
cc-session-tools list-active --name 'w?-*'            # names matching a glob, case-insensitively
cc-session-tools list-active --json | jq length       # every field of every session, as JSON
```

Claude Code keeps one registry record per running session in `<config dir>/sessions/<pid>.json`. `list-active` reads those of every profile through cc-blackbox, leaves out records whose process has ended, and lists a `sessions/` dir that several profiles share once, under the profile that runs each session.

The table has six columns: **Profile**, **Name** (`pid:<pid>` dimmed for a session without a name), **Status** (`waiting: <reason>` when the record says what a waiting session waits for), **Updated** (time since the record last changed, which Claude Code does on every status change), **Branch or cwd** (the git branch of the session's cwd, or the cwd with `~` when it is not on a branch), and **Tmux pane**. Rows are sorted by profile, then start time.

**The tmux pane is resolved live.** Claude Code records `<session>:@<window>.%<pane>` once, at startup. When you move the window to another tmux session, the record keeps the old session name. Only the pane id stays valid, so the command reads the current session and window of every pane with one `tmux list-panes -a` call. The table shows `%14 <session>:<window>`, `%14 (gone)` when the tmux server has no such pane, and `%14 (tmux unknown)` when tmux is missing or has no server. `tmux capture-pane`, `switch-client` and `send-keys` all take the pane id as a target.

**One session** is the job of [`inspect`](#inspect). `list-active` only lists: given a SESSION argument, it exits with `2` and prints the `inspect` command to run instead.

**Filters** combine with AND. `--profile` takes a glob, matched case-sensitively against the profile names, so a plain name matches only that profile. A glob that matches no profile is an error that lists the known ones. `--here` uses the scope of `search` and matches the session's current cwd.

**`--json`** prints an array of objects. The keys are a stable contract: snake_case, ISO 8601 timestamps, paths as strings, `null` for a missing value. They cover every field of the registry record (`raw` holds the record itself) plus derived ones: `waiting_for`, `tmux_pane`, `tmux_state`, `tmux_live`, `tmux_recorded`, `messaging_address`, `git_branch`, `git_worktree_root`, `git_repo_root` and `transcript_path`. The [active-sessions skill](coding-agent-plugins/claude-code/skills/active-sessions/SKILL.md) documents each key.

Exit codes: `0`, including when no session matches the filters (the notice goes to stderr, and `--json` prints `[]`). `1` when a `--profile` glob matches no profile. `2` for an unknown `--status`, or a SESSION argument.

### inspect

```bash
cc-session-tools inspect w1-order-planner             # every field of one running session
cc-session-tools inspect rohlik-team/w1-order-planner # the same, by address
cc-session-tools inspect 27906ead --json              # an ended session by a session id prefix, as JSON
cc-session-tools inspect old-name                     # a session renamed since, ended or running
cc-session-tools inspect 1199796 --days 30            # an ended session by its pid, from the auth-switch start log
```

`inspect` shows the full current picture of one session, running or ended. A running session shows every field `list-active --json` has, with `state` `running`. An ended session shows what its transcript and the [claude-code-auth-switch](https://github.com/fprochazka/claude-code-auth-switch) start log recorded, with `state` `ended`: the profile that ran it last and how that was decided (`profile_source`), its last name and title, its launch cwd and last cwd, the git branch of its last record, `started_at` and `last_activity_at`, every start in `profile_starts` (`ts`, `pid`, `source`, `profile`), `transcript_path`, and `resume_command`: `claude-<profile> --resume <id>` when an auth-switch profile ran it, else `claude --resume <id>`.

**SESSION** is looked up in this order, and the first step that finds something decides:

1. A running session, as cc-blackbox matches it: session id, name, `<profile>/<name>`, `<profile>/pid:<pid>`, pid, or a unique session id prefix. An all-digit SESSION is a pid when a running session has that pid.
2. An ended session by session id or session id prefix, in every transcript of every profile. Only file names are read.
3. An ended session by its name or any earlier name or title the transcript recorded, compared without case, among the transcripts active in the last `--days` days (14 by default). A renamed session is found under its old names too.
4. An ended session by its pid, from the auth-switch start logs next to the transcripts active in that window.

`<profile>/` in front restricts steps 2 to 4 to sessions that profile ran last. A step that finds several sessions exits with `1` and lists them. A transcript found in steps 2 to 4 whose session still runs is shown as running.

**Cost.** A running session takes about as long as `list-active`. Steps 2 to 4 run only when no running session matches. A session id costs a listing of every project dir. A name lists the transcripts active in the window, reading the head and tail of each, greps them for the name with ripgrep, or a built-in scan without it, and reads the title records of the files that contain it. About 0.8 seconds for 230 transcripts with 1.7 GB in the window. A pid also reads one small start log per transcript in the window.

**`--json`** prints one object whose keys are a stable contract, the same for a running and an ended session: `state`, every key of `list-active --json`, `title`, `title_source`, `last_activity_at`, `profile_starts` and `resume_command`. Fields only a running session has, such as `pid`, `status`, the tmux and messaging fields, `record_path` and `raw`, are `null` for an ended one.

A session that has no name and was not started through an auth-switch wrapper can be found after it ended only by its session id.

Exit codes: `0` when one session is shown. `1` when SESSION matches nothing or more than one session in a step.

### watch

```bash
cc-session-tools watch                                         # every running session except the one this runs under
cc-session-tools watch --profile 'rohlik*' --events=turns      # only turn_finished and waiting, in profiles matching a glob
cc-session-tools watch --name 'w*-order-planner' --until=turn_finished   # stop at the first finished turn of a worker
cc-session-tools watch w1-order-planner w2-order-planner       # these two sessions, until both have ended
cc-session-tools watch --busy-after=30m --waiting-after=5m --idle-after=2h
cc-session-tools watch --here --events-stream=stdout --timeout=10m
```

`watch` reports every change to the running sessions as one JSON event per line. It takes the filters of `list-active` and runs until an end you ask for, until its named sessions have ended, or until it gets SIGTERM or Ctrl-C. The last event is always `watch_ended` with a `reason`.

**Selection.** Without SESSION arguments the filters are evaluated again on every change. A session that starts later and matches joins with `session_started`. A running session that starts to match, for example after a rename or a status change under `--status`, joins with `session_matched`, and one that stops matching leaves with `session_unmatched`. With SESSION arguments, resolved like a running SESSION of `inspect` after the filters, the watch follows exactly those sessions, and ends with reason `sessions_ended` once all of them have ended.

**The own session is left out.** Run under a Claude Code session, `watch` never reports that session: every event it delivers makes the session busy, which would be the next event. `--include-self` watches it anyway. Naming the own session as SESSION without `--include-self` is an error.

**Events.** Each has `kind`, `seq`, `observed_at` and `data`, and is kept small: it names the session and says what changed, nothing more. For every field of a session, run `cc-session-tools inspect <session> --json`; the header says so on its `details:` line.

`data.session` names the session with one string that `inspect` resolves to exactly that session, also after it ended: its address `<profile>/<name>`, or `<profile>/pid:<pid>` when the session has no name or another running session of its profile has the same name, compared without case. The string is computed for each event, so it changes with a rename, and moves between the two forms when a name stops or starts being unique. After the session ended, `inspect` finds `<profile>/<name>` among the transcripts by name, and `<profile>/pid:<pid>` through the auth-switch start log, which a session started without an auth-switch wrapper does not have.

| Kind | Group | `data` |
|------|-------|--------|
| `watch_started` | `lifecycle` | `selection`, and `sessions`: `{session, status}` for every matching session, plus `waiting_for` when it waits |
| `watch_ended` | `lifecycle` | `reason`: `until` (with `event` and `seq`), `timeout`, `idle` (with `idle_after`), `sessions_ended`, `session_gone`, or `signal SIGTERM`/`signal SIGINT` |
| `session_started`, `session_matched` | `membership` | `session`, `status`: a new session that matches, or a running one that starts to |
| `session_ended` | `membership` | `session`, `reason`: `record_removed` (a clean exit) or `process_gone` |
| `session_unmatched` | `membership` | `session`: it no longer matches the filters |
| `status_changed` | `status` | `session`, `from`, `to`, and `waiting_for` when `to` is `waiting` |
| `turn_finished` | `turns` | `session`, `from`: `busy` or `waiting`, the status before `idle` |
| `waiting` | `turns` | `session`, `waiting_for` |
| `busy_long`, `waiting_long` | `thresholds` | `session`, `after` |
| `renamed` | `changes` | `session` with the new name, `from`: the `session` string before the rename |
| `cwd_changed` | `changes` | `session`, `to`: the new cwd |
| `tmux_moved` | `changes` | `session`, `to`: `<tmux session>:<window index>`, or `null` when the tmux server no longer has the pane |

```json
{"kind":"turn_finished","seq":6,"observed_at":"2026-11-16T17:05:03.120Z","data":{"session":"work/w1-order-planner","from":"busy"}}
```

One status transition can yield two events: `busy` to `idle` is `status_changed` and `turn_finished`, anything to `waiting` is `status_changed` and `waiting`. Take `--events=turns` for the derived events alone. A change is reported for a session that matched before it, so a rename out of a `--name` glob is `renamed`, then `session_unmatched`. A session that starts to match is reported once, as `session_matched` with its new state.

`--events` and `--exclude-events` take kinds and groups, comma-separated and repeatable. `lifecycle` is always on. An unknown kind or group, or one this watch never produces, is an error at start: `busy_long` needs `--busy-after`, `waiting_long` needs `--waiting-after`, and a watch of named sessions never produces `session_started`, `session_matched` or `session_unmatched`.

**Ends and thresholds.** `--until=<kind>` stops after the first reported event of that kind. `--timeout` stops that long after the start, `--idle-after` when nothing was reported for that long; `busy_long` and `waiting_long` do not count as activity. `--busy-after` reports `busy_long` once per busy stretch of a session, `--waiting-after` reports `waiting_long` once per waiting stretch. A stretch starts at the record's `statusUpdatedAt`, so a session that was already busy for an hour when the watch started gets its `busy_long` at once. Durations are `90s`, `30m`, `2h`, `1h30m` or plain seconds.

**Startup header.** The header, the `ended: <reason>` line and errors go to stderr, each line prefixed `cc-session-tools watch: ` and never starting with `{`, so in merged output every line that starts with `{` is an event:

```text
cc-session-tools watch: selection: profile=rohlik* (profiles: rohlik-team, rohlik-team2); 4 matching now
cc-session-tools watch: own session: coordinator (pid 4242) is left out; --include-self watches it
cc-session-tools watch: stream: stdout (one JSON event per line)
cc-session-tools watch: changelog: /tmp/claude-1000/…/scratchpad/cc-session-tools/watch-20261116T170501-4343.events.jsonl
cc-session-tools watch: details: cc-session-tools inspect <session> --json
cc-session-tools watch: events: turn_finished,waiting (always: watch_started,watch_ended)
cc-session-tools watch: ignored: session_started,session_ended,session_matched,session_unmatched,status_changed,renamed,cwd_changed,tmux_moved
cc-session-tools watch: ends: until the first turn_finished
```

**Output streams.** `--events-stream=stdout` prints one JSON event per line. `--events-stream=claude-message` sends the events as messages to the Claude Code session the command runs under, through its messaging socket, and fails at start when that session accepts no messages. The default, `auto`, picks `claude-message` under a session that accepts messages and stdout otherwise; a `stream fallback:` header line says why it did not message a session it runs under. Each message is one batch, as `cc-session-tools-watch:<pid>`:

```text
<session-watcher filter="name=w*-order-planner" seq="5-6">
{"kind":"status_changed","seq":5,"data":{"session":"work/w1-order-planner","from":"busy","to":"idle"}}
{"kind":"turn_finished","seq":6,"data":{"session":"work/w1-order-planner","from":"busy"}}
</session-watcher>
```

Events that arrive within 3 seconds go out as one message, and a batch above 64 KB becomes a count per kind. `watch_ended` goes out at once. The first message also carries `changelog="…"` and `details="cc-session-tools inspect <session> --json"`. Message lines leave out `observed_at`. `--message-priority` sets the priority, `next` by default. The watch ends with reason `session_gone` when its session can no longer receive messages. The delivery itself is [cc-blackbox](https://github.com/fprochazka/cc-blackbox)'s event stream, which glab-watch uses too.

**Changelog.** Every reported event is also appended to `watch-<time>-<pid>.events.jsonl`, under `<session scratchpad>/cc-session-tools/` when the command runs under a session, else under `$TMPDIR/cc-session-tools-<uid>/`. The header names it. Nothing deletes it.

**Cost.** No daemon: the watcher stats every registry record of every profile once a second, reading a record again only when its mtime or size moved, and lists the sessions through cc-blackbox only when a new record appears. tmux is asked once every 3 seconds. A status that lasts less than one poll can be missed.

Exit codes: `0` when the watch ends, whatever the reason. `1` when SESSION matches nothing or several sessions, names the own session, a `--profile` glob matches no profile, or `claude-message` is not possible. `2` for a usage error: an unknown event kind or group, a bad duration, or an `--until` kind the watch does not report.

## Requirements

- Python 3.11+
- [ripgrep](https://github.com/BurntSushi/ripgrep) — optional. It makes `search --rg` faster; without it a built-in scan runs.
- git — only for worktree-aware scoping and the branch in `list-active`. Outside a repo every subcommand falls back to the exact current directory.
- tmux — optional. Without it `list-active` and `watch` report every tmux pane as unknown.

## Project structure

```
src/cc_session_tools/
├── __init__.py        # package version
├── cli.py             # root Typer app, --version, the error guard
├── search_cmd.py      # the `search` command
├── distill_cmd.py     # the `distill` command
├── list_active_cmd.py # the `list-active` command
├── inspect_cmd.py     # the `inspect` command
├── inspection.py      # one session, running or ended: the lookup and its fields
├── watch_cmd.py       # the `watch` command: options, header, stream and changelog
├── watcher.py         # the polling loop: registry records in, session events out
├── watch_events.py    # event kinds and groups, the --events filter, durations
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
└── skills/{session-tools,active-sessions,session-watch}/SKILL.md
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
