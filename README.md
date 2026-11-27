# claude-code-session-tools

Commands and skills for finding and distilling past Claude Code sessions, and for listing the running ones, with the `cc-session-tools` CLI.

The CLI has six subcommands. `search` lists and greps the sessions that belong to the directory you run it in. `distill` condenses one session transcript into markdown. `list-active` lists the sessions that run on the machine now, in every profile, with their live tmux pane. `inspect` shows every field of one session, running or ended. `watch` reports changes to the running sessions as JSON events, for an agent that waits for other sessions. `limits` shows the rate limits of every profile's account.

## Why

Claude Code keeps every session as a JSONL transcript, and they pile up. When you come back to a piece of work weeks later, the earlier session holds the decisions you need, buried in a file too large to read and too large to paste into a fresh session.

`search` finds the session. `distill` reduces it to the prompts, the answers and the subagent results, so a follow-up session can read the outcome instead of replaying the history.

## Installation

You need Python 3.12+ and [uv](https://docs.astral.sh/uv/).

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
- The `active-sessions` skill, which teaches Claude `list-active`, `inspect` and `limits`: finding another session, running or ended, its profile, tmux pane, messaging address and resume command, and how much of each profile's rate limits is left, through `--json` and `jq`.

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
cc-session-tools distill latest --stdout             # the main markdown on stdout instead of a file
cc-session-tools distill latest --include-thinking   # keep the assistant's thinking blocks
cc-session-tools distill latest --output-dir ./notes # write somewhere other than the default
```

The target is a `.jsonl` path, a session id, or the literal `latest`.

By default the command writes `<output-dir>/<session-id>.md` and prints that one path on stdout. The output directory defaults to `/tmp/claude-code-session-tools`. The stats table — prompt count, subagent count, size, estimated tokens — goes to stderr in both modes, so stdout stays either a single path or valid markdown.

`latest` uses the same scope as `search`: the newest session of the current repo and its worktrees, by transcript timestamp. Transcripts with no timestamped records — empty stubs — are skipped. There is no "newest session overall" fallback. If the scope is empty the command fails with exit `1` rather than distilling an unrelated session.

A session id is looked up across every project directory, so it works from anywhere. Subagent sidecars under `<session-id>/subagents/` are child transcripts, not sessions, and cannot be reached by id.

**A compacted session is split at its compactions.** A compaction continues in the same transcript file, and replaces the model's context with a summary. The main file holds only the context the model worked with since the last compaction: the compaction summary under `## 📦 Compaction summary`, the stretch of conversation Claude Code kept verbatim next to it, and everything after. Each earlier stretch goes next to it as `<output-dir>/<session-id>.pre-compact-<k>.md`, `k` = 1 for the oldest, with its own summary when a compaction opened it. Every record of the transcript lands in exactly one file. A further compaction adds one file and keeps the existing numbers. A session without compactions gives one file, as before.

The main file and every part after the first start, right under the title, with a line that points back:

```text
> Compacted session: this part starts at compaction 2 of 2 (auto, 2026-11-20 14:03 UTC, 180k tokens before it). The conversation before it is distilled in /tmp/claude-code-session-tools/<session-id>.pre-compact-2.md.
```

The path is absolute, so an agent can open it as it is. Every pre-compact part ends with `> Continued after compaction <k> in <path>.`, pointing at the next part. Each file's header gives its own prompt count and time range. A subagent spawned before a compaction and finished after it stays whole, in the part where it was spawned.

Claude Code from about 2.1.266 keeps the last stretch of the conversation verbatim after a compaction. Those records stay before the boundary in the transcript, so the part after a compaction starts at the first of them. Older transcripts have no such stretch, and the part starts at the compaction.

The stdout contract holds: without `--stdout` it is the path of the main file. With `--stdout` it is the main file's markdown, and the pre-compact parts are still written to the output directory so the pointer line names a file that exists; stderr names the directory. The stats table describes the main file and adds a `Pre-compact parts` row with the count, total size and estimated tokens of the other files.

**A prompt's heading names who sent it:** `👤 User` for you, `📨 Peer session` for a message another Claude Code session sent, `🔔 Task notification` for a background task that finished. Prompts you typed while Claude was working are included.

**The rendered markdown is not reliably parseable by heading level.** Prompts and assistant replies keep their own markdown, so a distilled file's `## ` headings are not only `👤 User` and `🤖 Assistant` — any heading someone wrote inside the conversation shows up at the same level.

Exit code `1` when the target does not resolve or the file cannot be read.

### timeline

```bash
cc-session-tools timeline latest                          # where the newest session of this repo spent its time
cc-session-tools timeline <session-id>                    # by session id, from anywhere
cc-session-tools timeline <session-id> --max-lines 80     # collapse more
cc-session-tools timeline <session-id> --gap 5m           # print idle gaps from 5 minutes, merge shorter ones
cc-session-tools timeline <session-id> --from 18:30 --to 19:00   # one stretch, every turn and gap
cc-session-tools timeline <session-id> --subagent <agent-id>     # one subagent: its bursts, calls and children; agent-<id> works too
cc-session-tools timeline <session-id> --json             # everything, nothing merged
```

The target is a `.jsonl` path, a session id, or `latest`, as for `distill`. The markdown goes to stdout, errors to stderr; exit code `1` when the target, a time or the Bash groups file can't be read. A subagent's own `agent-<id>.jsonl` is no target: pass its session and `--subagent`.

The header gives the session's totals:

- **wall** runs from the first to the last prompt, response, tool result or subagent record. Notices that Claude Code writes days later don't count.
- **active** is the time the main agent was in a turn or a subagent was working, overlaps counted once. **away** is the idle gaps over 2 h that a human or cron prompt ended.
- **main agent busy** is the sum of its turns, split into model time (from the `api-request` records, `n/a` before Claude Code wrote them), tool time, foreground subagents, waiting on the user (permission prompts the user answered, AskUserQuestion, ExitPlanMode) and compactions that blocked the session. The parts overlap: a call runs while the model streams the next one.
- **idle between turns** is split by what ended the gap: `human`, `peer` (another session), `handback` (one of the session's own subagents reporting), `task-notification`, `cron`, `sdk`, `other` (model activity no recorded prompt opened). The time subagents were active during the gaps follows.
- **subagents**: how many, top-level (foreground and background) and nested. **subagent active** is the union of their activity, as the clock ran; **lane time**, shown when it differs, adds up each subagent's own active time, in which a parent counts its wait on its nested subagents. Their model time and tokens follow.
- **⌛ suspected waits**: a tool call over 1 h usually waited on something the transcript doesn't show, an unanswered prompt or a machine that slept. Its time still counts as busy and active; the header says how much of both lies inside such calls, so it can be discounted.

Below the header, one line per turn: `HH:MM-HH:MM T <trigger> <prompt or command> <busy>: <tool mix>`, with Bash split by purpose (`Bash:test×3`, `Bash:wait`, `Bash:git`); `~/.config/cc-session-tools/bash-groups.toml` maps your own programs to a group. Under a turn: `⏸` a wait on the user, `⌛` a suspected wait, `!` an interruption, and `└` each subagent it launched: shape (`fg` or `bg`), type, model, role (`[validate]` from a description prefix such as `validate: C4`, `[validate?]` guessed from the agent type, a word naming the agent such as `implementer` or `actor`, or the description's first verb), span, active time, bursts, rounds, tokens and description. Nested subagents fold into their parent's line, and a turn that launched more than four groups them by shape, type and role. `…` lines are idle gaps, `≡` compactions with the segment they open (the numbering of `distill`'s parts), `!` API errors.

Short turns without subagents or waits, and short idle gaps, merge into `⋯ N turns (…) busy …, idle …` lines. When the output is still longer than `--max-lines`, the thresholds grow; past a point compact mode starts again from the base thresholds and grows them until the output fills `--max-lines`. In compact mode short turns are left out, and a short turn prints only the subagents it launched that were active at least half the turn threshold, as `HH:MM trigger→subagent` (grouped when there are more than three). Each day header carries the day's busy time and turns, the count of compactions that ran in the background (under 1 s) with their segments, and a line with the top-level subagents launched that day: their active time by role and the most repeated descriptions, digits written as `N`. Nested subagents are only counted there, because a parent's active time already covers its wait on them. The thresholds stop growing at 6 h for turns and 24 h for gaps; a session that still doesn't fit says so in one line and suggests `--from`/`--to`. `--from`/`--to` print a stretch with nothing merged. They take `HH:MM` on the session's first day, in local time, or an ISO time.

Prompt excerpts are cut to 50 characters. The session's cwd and, for a worktree, its repo print as `<repo>`, the home dir as `~`, also in the dashed form of a project dir name (`-home-user-…`), a scratchpad dir as `<scratchpad>`, other files of Claude Code's per-session temp dir (`…/claude-<uid>/<project>/<session>/`) as `<claude-tmp>/…`, host names, in URLs or not, as `<host>` (lowercase names outside a path: `example.com` with a common public domain, longer names or names after `@`, `//`, `-h`, `ssh` or before `:<port>` also with a private or newer one, so `compose.dev` or `server.app` stay), `gs://` and `s3://` buckets as `<bucket>`, long hex ids as `<hex>`, and values that look like credentials as `<redacted>`. That is a safety net for pasting the output somewhere, not a guarantee.

`--json` prints one object, durations in seconds, times in ISO 8601:

| Key | Holds |
|---|---|
| `session_id`, `kind`, `kind_source`, `start`, `end`, `has_permission_decisions`, `window` | the session; `kind` is the auth-switch start log's role (`kind_source: start-log`) or `loop`/`interactive` guessed from what opened the turns (`origins`); `window` is `--from`/`--to` or `null` |
| `totals` | `wall`, `active`, `away`, `main_busy`, `main_idle`, `model` (`null` without api-request records), `tools`, `foreground_subagents`, `user_waits`, `compaction_waits`, `idle_by_trigger`, `idle_background`, `batched_calls`, `interruptions`, `api_errors`, `suspect_waits`, `suspect_wait_main_busy`, `suspect_wait_subagent_active` (a union over the subagents), `subagent_active` (the union of the subagents' bursts), `subagent_lane_time` (the sum over their lanes) |
| `main` | the main agent's `model`, `model_seconds`, `requests`, `tokens_total` (with cache reads), `tokens_new` (input, cache creation, output), `first_context` |
| `turns[]` | `index`, `start`, `end`, `seconds`, `trigger`, `prompt` (50 characters), `segment`, `ended_by_record`, `interrupted`, `subagents` |
| `gaps[]` | `start`, `end`, `seconds`, `ended_by`, `background_seconds`, `away` |
| `calls[]` | every tool call of the main agent and the subagents: `id`, `name`, `lane` (`main` or an agent id), `turn`, `streamed_at`, `start`, `end`, `wall`, `request_ref`, `permission_source`, `permission_decision`, `denial`, `is_error`, `batched`, `background`, `user_wait`, `suspect_wait`, `bash_group`, `command` (200 characters), `description` |
| `subagents[]` | `agent_id`, `agent_type`, `description`, `shape`, `parent_id`, `depth`, `tool_use_id`, `turn`, `role`, `role_source`, `step`, `start`, `end`, `active`, `rounds`, `bursts`, `model`, `model_seconds`, `requests`, `tokens_total`, `tokens_new`, `first_context`, `api_errors` |
| `compactions[]` | `time`, `lane`, `trigger`, `duration_ms`, `wait_seconds` (0 for one that ran in the background), `pre_tokens`, `post_tokens`, `segment` |

With `--subagent`, the JSON is that subagent's fields plus `window`, `tools`, its `calls` and its `children`. `--from`/`--to` limit the lists to records inside the window; the totals stay the session's.

How the numbers are made, and where they can mislead:

- A tool call starts when its `tool_use` block streams and ends when its result is written. Calls of one response overlap, so a call's `wall` is elapsed time and the totals are unions. Bash, Edit and Write calls of one response run one after another, and each starts when the previous one ended.
- A result written together with a long Agent call's result of the same response was likely held back until the subagent returned. Its own runtime is unknown, its `wall` is 0 and it is counted in `batched_calls`.
- A subagent is active while its records come in less than 5 minutes apart, and while one of its own tool calls runs. A parent waiting on a nested subagent counts as active too.
- Turns are found from the prompts, queued prompts and cron fires that open them, and end at Claude Code's turn-end record. A turn without one ends at its last activity once something new comes over 15 minutes later with no call running. A call that runs for days keeps its turn open; the `⌛` flag marks it.

### tool-stats

```bash
cc-session-tools tool-stats latest                 # where the newest session of this repo spent its tool time
cc-session-tools tool-stats <session-id> --top 20  # list 20 slowest calls and repeated commands
cc-session-tools tool-stats <session-id> --json    # every section as data, unrounded
cc-session-tools tool-stats --sessions-from ids.txt  # added up over many sessions, one id or .jsonl path per line
```

The target is resolved as for `timeline`. The markdown goes to stdout, errors to stderr; exit code `1` when the target or the Bash groups file can't be read, `2` for a usage error such as `--top 0`. Commands are redacted as in `timeline`.

A call's wall runs from when it could start to when its result was written, as in `timeline`. Calls of one response, and of different subagents, overlap, so a column's total is a sum of elapsed times, not a share of the session's clock. A call over 1 h is marked `⌛` and counted in the header, because it probably waited on something the transcript doesn't show. A call the user answered a permission prompt or a dialog for waited on the user for an unknown part of its wall, because the transcript doesn't say when the answer came; such calls are left out of the tool sections, and the header gives their count and wall. The sections:

- **Per tool**: count, total, median and max wall, the share of all non-Agent tool wall, and the calls of the main agent and of each subagent type.
- **Bash by group**: each command counts once, for its highest-priority group: test (split into targeted and full), build, lint, format, wait, run, db, cloud, gh-glab, package, git, agent-coord, ext-cli, search, file-edit, file-read, other. A line follows with the polls on a build or test tool's process (`pgrep gradle` in a loop), which often wait on another session's job. Background Bash calls show only their launch.
- **Slowest calls**, `--top` of them: one per tool and normalized command, with the count of similar calls. Results held back behind a subagent (wall 0) don't show.
- **Repeated commands**, `--top` of them, by total wall: commands with the `cd X &&` prefix dropped, the scratchpad written as `<scratchpad>`, the durations of `timeout` and `sleep` and the numbers of `seq` as `N`, and every other run of 4 or more digits as `N`, with the lanes that ran them. So commands on different MRs or issues with numbers of 4 or more digits share a row. The markdown cuts a command at 100 characters; `--json` has the whole normalized command, up to 200.
- **Permission decisions** of every lane by source (`config`, `hook`, `user_temporary`, `user_permanent`, `user_reject`, a rejection with its reason type when that differs from the source, or `none`), then the prompts and dialogs the user answered with their wait, and the denials.
- **Subagent cost by role**: per role, type and whether it was nested: count, active time, median and max, rounds, tokens with cache reads and new ones, the median context of the first request, and the models. `active` is each lane's own time, and a parent's includes its wait on its nested subagents, so the column adds up to more than the subagents' elapsed time, which the section's first line gives as the union of their bursts. The nested subagents follow, grouped by the role of the top-level subagent above them.

**Your own tools.** The built-in Bash rules know widely used public tools only. Map your own programs and scripts to a group in `~/.config/cc-session-tools/bash-groups.toml`; an entry overrides a built-in rule, and a bad file is an error that names the file and line:

```toml
[programs]
my-db-cli = "db"
my-deploy = "cloud"

[scripts]
"*/scripts/smoke-*.sh" = "test"
```

`--json` prints one object, durations in seconds, times in ISO 8601:

| Key | Holds |
|---|---|
| `session_id` | the session |
| `totals` | `calls`, `main_calls`, `subagent_calls`, `tool_wall` (non-Agent calls without a user wait), `user_wait_calls`, `user_wait_wall`, `main_busy`, `batched_calls`, `background_bash`, `suspect_waits`, `suspect_wait_wall`, `subagents`, `subagents_elapsed` |
| `tools[]` | `tool`, `n`, `total`, `median`, `max`, `share` (`null` for Agent), `main`, `subagents_by_type` |
| `bash_groups[]` | `group`, `n`, `total`, `median`, `max`, `share` (of all Bash wall), `main`, `subagents`, `programs`, `waits_on_other_builds`, `waits_on_other_builds_wall` |
| `slowest[]` | `session_id`, `tool`, `lane`, `lane_type`, `start`, `wall`, `suspect_wait`, `bash_group`, `command` (200 characters), `similar` |
| `repeated[]` | `command` (normalized, 200 characters), `n`, `total`, `median`, `max`, `sessions` (how many ran it), `bash_group`, `lanes_by_type` |
| `permissions` | `recorded` (every session recorded decisions), `sessions_without_decisions`, `decisions`, `user_answered`, `user_wait`, `user_waits[]` (`session_id`, `tool`, `lane`, `start`, `wall`, `decision`), `denials` |
| `subagent_roles[]` | `role`, `guessed`, `type`, `nested`, `n`, `active`, `median`, `max`, `rounds`, `tokens_total`, `tokens_new`, `first_context_median`, `models` |
| `nested_by_parent_role[]` | `parent_role`, `n`, `active` (lane time), `elapsed` (union), `tokens_total`, `by_type` |

**Many sessions.** `--sessions-from FILE` adds the sections up over the sessions FILE lists, one session id or `.jsonl` path per line; blank lines and `#` comments are skipped, and `-` reads the list from stdin. It takes no TARGET. Each session is analysed in a child process of its own, one after another, and only its sums come back, so the run needs the memory of the largest session, not of all of them: a large session can take a few GB while it is analysed. A session that can't be resolved or read is reported on stderr, listed under `skipped` and left out; the run goes on. Each session's text is redacted with its own repo. The markdown starts with a row per session, by active time (kind, active, main busy, tool wall, the Bash group with the most wall, suspected waits, user waits), and the totals per session kind, then the sections as for one session: repeated commands give the number of sessions that ran them, slowest calls name their session, and the subagents' elapsed time is each session's union, added up. The JSON adds three keys to the single-session shape and drops `session_id`:

| Key | Holds |
|---|---|
| `sessions[]` | `session_id`, `title` (60 characters), `kind`, `start`, `wall`, `active`, `main_busy`, `tool_wall`, `top_group`, `top_group_wall`, `suspect_wait_wall`, `user_wait_wall`, `subagents` |
| `by_kind` | per kind: `sessions`, `wall`, `active`, `main_busy`, `tool_wall`, `suspect_wait_wall`, `user_wait_wall`, `subagents`, `bash_groups` (group → wall) |
| `skipped[]` | `target`, `error` |

### list-active

```bash
cc-session-tools list-active                          # every running session, one row each
cc-session-tools list-active --profile work --here    # sessions of one profile in this repo and its worktrees
cc-session-tools list-active --profile 'work*'      # sessions of every profile whose name matches a glob
cc-session-tools list-active --status busy --tmux     # busy sessions that run in tmux
cc-session-tools list-active --name 'w?-*'            # names matching a glob, case-insensitively
cc-session-tools list-active --tmux-session current   # sessions whose pane is in this tmux session now
cc-session-tools list-active --role 'worker-*'        # sessions started with a matching auth-switch role
cc-session-tools list-active --active-within 1h       # sessions that got a model response in the last hour
cc-session-tools list-active --json | jq length       # every field of every session, as JSON
```

Claude Code keeps one registry record per running session in `<config dir>/sessions/<pid>.json`. `list-active` reads those of every profile through cc-blackbox, leaves out records whose process has ended, and lists a `sessions/` dir that several profiles share once, under the profile that runs each session.

The table has seven or eight columns: **Profile**, **Name** (`pid:<pid>` dimmed for a session without a name), **Role** (only when a listed session has one; empty for the others), **Status** (`waiting: <reason>` when the record says what a waiting session waits for), **Active** (time since the session's last model response, in its transcript or any subagent's; `started …` dimmed for a session without a response yet), **Ctx** (how full the context is, such as `312k/1M 31%`; see `context_window` below), **Branch or cwd** (the git branch of the session's cwd, or the cwd with `~` when it is not on a branch), and **Tmux pane**. Rows are sorted by profile, then start time.

**The tmux pane is resolved live.** Claude Code records `<session>:@<window>.%<pane>` once, at startup. When you move the window to another tmux session, the record keeps the old session name. Only the pane id stays valid, so the command reads the current session and window of every pane with one `tmux list-panes -a` call per tmux server. The table shows `%14 <session>:<window>`, `%14 (gone)` when the tmux server has no such pane, and `%14 (tmux unknown)` when tmux is missing or has no server. `tmux capture-pane`, `switch-client` and `send-keys` all take the pane id as a target. Act on a pane only when `tmux_state` is `present`, and always with `-S <tmux_socket>`, which reaches the session's server. When `tmux_socket` is `null`, there is no pane to act on: the session is outside tmux, or its pane was not found and owned. `switch-client` cannot move a client to another tmux server.

**Each pane is looked up on the tmux server the session runs on.** Pane ids restart at `%0` with every tmux server, so a session started on its own server, such as `tmux -L <name>`, records a pane id that the default server may have too. The session's server is the socket in `TMUX` of its process environment, which cc-blackbox reads on Linux. The command lists the server bare `tmux` reaches, the caller's, and once each other server a session runs on, with `tmux -S <socket>`. A session whose socket is unknown, because the environment cannot be read, has no `TMUX` or names a relative socket, is looked up on the caller's server. A server that does not answer makes only its own sessions `unknown`. The table adds `(server <socket name>)`, dimmed, to a pane on a server other than the caller's.

**A pane counts only when the session runs in it.** Even on the right server, the id a session recorded can name another program's pane, for example after the server restarted. A server whose pid differs from the one in the session's `TMUX` was restarted, and its pane is `gone`. The listing also gives each pane's process, and a pane is the session's own only when that process is the session's process or one of its ancestors. A pane that fails either check is `gone`: it has no `tmux_live`, never matches `--tmux-session`, and is never captured. The check reads `/proc` a few times and makes no extra tmux call. A pane that passes is remembered for the life of the process, so a watch checks each session's pane once, not on every poll.

**One session** is the job of [`inspect`](#inspect). `list-active` only lists: given a SESSION argument, it exits with `2` and prints the `inspect` command to run instead.

**The role comes from the auth-switch start log.** [claude-code-auth-switch](https://github.com/fprochazka/claude-code-auth-switch) logs every start of a session next to its transcript, with the role the run was started with. `role` is the role of the session's running process, from the newest start that process logged, so a session resumed under another role shows the new one, and two processes that run the same session each show their own. `/clear` and an in-process `/resume` keep the process and its role: the start hook logs it again under the new session id. The hook writes the log when the session starts, before the first prompt creates the transcript, so a session that waits for its first prompt already has its role. A session started without an auth-switch role, or before auth-switch had roles, has none.

**Activity comes from the transcript, not from the registry record.** The record's `updatedAt` changes with the status, but a local command such as `/login` moves it without the model, so a session idle for days can look fresh. It can also lag behind: a session can answer well after its last `updatedAt`. Its transcript's last record has the same problem: a `/login` or a rename notice is a record. The newest `assistant` record is a model response, so `list-active` uses that, from the main transcript or any subagent's, since a background subagent writes only to its own. The table reads the end of each transcript, which adds about 0.05 seconds for 35 sessions.

**Filters** combine with AND. `--profile` takes a glob, matched case-sensitively against the profile names, so a plain name matches only that profile. A glob that matches no profile is an error that lists the known ones. `--here` uses the scope of `search` and matches the session's current cwd. `--tmux-session <name>` keeps the sessions whose own pane is in that tmux session now, by the tmux server, so a window moved there with `move-window` counts; `current` is the tmux session of `$TMUX_PANE`, the pane the command runs in. The name is resolved on the caller's tmux server, and sessions on other tmux servers never match. A name no tmux session has, `current` outside tmux, or a tmux server that does not answer is an error that lists the tmux sessions. `--role` takes a glob, matched case-sensitively against the session's role, and is repeatable; a session without a role never matches it. `--active-within` takes a duration like `watch` does (`90s`, `30m`, `2h`, `1h30m`) and keeps the sessions whose last model response, or start when they have no response yet, is within it.

**`--json`** prints an array of objects. The keys are a stable contract: snake_case, ISO 8601 timestamps, paths as strings, `null` for a missing value. They cover every field of the registry record (`raw` holds the record itself) plus derived ones: `role`, `waiting_for` (see below), `tmux_pane`, `tmux_socket` (the socket of the session's tmux server: the one its environment names, else the caller's server when the pane was found and owned there, else `null`), `tmux_state`, `tmux_live`, `tmux_recorded`, `messaging_address`, `git_branch`, `git_worktree_root`, `git_repo_root` and `transcript_path`, and from the transcript `last_activity_at` (the last model response), `last_record_at` (the newest record of any kind), `cost` and `context_window`. `updated_at` is the last change of the registry record, not activity. The [active-sessions skill](coding-agent-plugins/claude-code/skills/active-sessions/SKILL.md) documents each key.

**`waiting_for`** is what a `waiting` session waits for: the record's `waitingFor`, such as `permission prompt`, and `null` in every other status. The table shows it as `waiting: <reason>`. Claude Code's record says `permission prompt` for the plan approval dialog of plan mode too, so for a session in that state that runs in tmux, `list-active` captures its pane and reports `plan approval` when the pane shows that dialog, as `inspect --screen` classifies it as `plan_approval`. That costs one `tmux capture-pane` per such session. A session outside tmux, or a pane that cannot be captured, keeps `permission prompt`. `raw.waitingFor` keeps the record's value.

**`context_window`** is how full the session's context is: the same numbers Claude Code gives its status line. The object has `used` (the input and cache tokens of the newest response of the main conversation), `size` (the window in tokens), `percent`, `compacted`, `model`, `size_source` and `measured_at` (the time of that response, or of the compaction after it). Output tokens do not count, and neither do subagents, which have contexts of their own. It is `null` for a session without a response, as right after a `/clear`.

After a compaction the fill is unknown until the session answers again: `used` is 0 and `compacted` is `true`, and the table shows `0k/1M compacted`. The status line shows 0% in the same gap.

No transcript record holds the window size, so it comes from the model id, through a table of models in [cc-blackbox](https://github.com/fprochazka/cc-blackbox). `size_source` says how: `model_tag` for a `[1m]` suffix, `model_table` for the table, `used_exceeds_table` when the fill was above the table's size and so proves the 1M window, `env_disable_1m` when `CLAUDE_CODE_DISABLE_1M_CONTEXT` in the session's settings or environment caps it at 200k, and `default` for an unknown model, which Claude Code assumes to be 200k; the table dims that size. Only a running session shows `env_disable_1m`. A provider that refuses the 1M window, or a fallback to a smaller model, does not show in the transcript, so `size` can be too large for those.

**`cost`** is what Claude Code records when a process of the session exits, never the current cost of a running session. For an ended session it is the session's total. For a running session that was resumed, it is the total as of its last exit and leaves out the current run. A running session that never exited has `null`. The object has `total_cost_usd`, `has_unknown_model_cost`, `started_at`, the durations in seconds (`duration_seconds`, `api_duration_seconds`, `api_duration_without_retries_seconds`, `tool_duration_seconds`), `lines_added`, `lines_removed`, and `model_usage` with the tokens and `cost_usd` of each model. Reading it costs a read of the whole transcript of a session that never exited, so `--json` takes about 0.4 seconds for 35 sessions, against 0.2 for the table. The table has no cost column, because a running session's cost would read as its current cost.

Exit codes: `0`, including when no session matches the filters (the notice goes to stderr, and `--json` prints `[]`). `1` when a `--profile` glob matches no profile, or `--tmux-session` matches no tmux session. `2` for an unknown `--status`, a duration `--active-within` cannot read, `--tmux-session` with `--no-tmux`, or a SESSION argument.

### inspect

```bash
cc-session-tools inspect w1-order-planner             # every field of one running session
cc-session-tools inspect work-team/w1-order-planner # the same, by address
cc-session-tools inspect 27906ead --json              # an ended session by a session id prefix, as JSON
cc-session-tools inspect old-name                     # a session renamed since, ended or running
cc-session-tools inspect 1199796 --days 30            # an ended session by its pid, from the auth-switch start log
cc-session-tools inspect w1-order-planner --screen    # also what its tmux pane shows: empty prompt, dialog, busy …
```

`inspect` shows the full current picture of one session, running or ended. A running session shows every field `list-active --json` has, with `state` `running`. An ended session shows what its transcript and the [claude-code-auth-switch](https://github.com/fprochazka/claude-code-auth-switch) start log recorded, with `state` `ended`: the profile that ran it last and how that was decided (`profile_source`), its last name and title, its launch cwd and last cwd, the git branch of its last record, `started_at`, `last_activity_at` (its last model response), `last_record_at`, its `cost` and `context_window`, its `role`, every start in `profile_starts` (`ts`, `pid`, `source`, `profile`, `role`, `role_prompt`, `role_prompt_mode`), `transcript_path`, and `resume_command`: `claude-<profile> --resume <id>` when an auth-switch profile ran it, else `claude --resume <id>`.

**SESSION** is looked up in this order, and the first step that finds something decides:

1. A running session, as cc-blackbox matches it: session id, name, `<profile>/<name>`, `<profile>/pid:<pid>`, pid, or a unique session id prefix. An all-digit SESSION is a pid when a running session has that pid.
2. An ended session by session id or session id prefix, in every transcript of every profile. Only file names are read.
3. An ended session by its name or any earlier name or title the transcript recorded, compared without case, among the transcripts active in the last `--days` days (14 by default). A renamed session is found under its old names too.
4. An ended session by its pid, from the auth-switch start logs next to the transcripts active in that window.

`<profile>/` in front restricts steps 2 to 4 to sessions that profile ran last. A step that finds several sessions exits with `1` and lists them. A transcript found in steps 2 to 4 whose session still runs is shown as running.

**Cost.** A running session takes about as long as `list-active`. Steps 2 to 4 run only when no running session matches. A session id costs a listing of every project dir. A name lists the transcripts active in the window, reading the head and tail of each, greps them for the name with ripgrep, or a built-in scan without it, and reads the title records of the files that contain it. About 0.8 seconds for 230 transcripts with 1.7 GB in the window. A pid also reads one small start log per transcript in the window.

The text output prints the cost as one line of totals and one line per model, and says whether the current run is left out, so a running session's cost does not read as its current cost.

**`--json`** prints one object whose keys are a stable contract, the same for a running and an ended session: `state`, every key of `list-active --json`, `title`, `title_source`, `profile_starts`, `resume_command` and `plan`. Fields only a running session has, such as `pid`, `status`, the tmux and messaging fields, `record_path` and `raw`, are `null` for an ended one.

**`plan`** is the plan file of a session that entered plan mode, the same for a running and an ended session, and `null` for a session that never did. In plan mode the model writes its plan into a file, and the plan approval dialog shows that file; the file's name is random and its dir depends on the `plansDirectory` setting. Claude Code records the path in the transcript, in the `plan_mode` attachment when the session enters plan mode and in the `plan_mode_exit` attachment after an approval, and `plan` comes from the newest of them. The object has `file` (the absolute path), `slug` (the file's base name), `exists`, `sha256` of the content and `modified_at`; the last two are `null` when the file does not exist, as before the model writes it. A rejected plan is revised in the same file, so compare `sha256` to tell whether it changed. Finding it reads the whole transcript, about 0.3 seconds for 170 MB.

A session that has no name and was not started through an auth-switch wrapper can be found after it ended only by its session id.

**`--screen`** captures the session's tmux pane with `tmux capture-pane -p -e`, on the session's tmux server, and says what it shows, for a coordinating session that types into other panes (`/compact`, a brief as `@<file>`, the answer to a startup dialog) and must know when that is safe. The registry status cannot tell: startup dialogs come before the session has a usable record, and text someone has half typed is in no record. `--json` gets one more key, `screen`, after all the others:

- `state`: `prompt_empty` (nothing typed, or only the dimmed `Try "…"` placeholder or a dimmed suggestion, which typing replaces, and the cursor right after `❯`), `prompt_has_input` (typed text, or a `[Pasted text #1 +40 lines]` or `[Image #1]` marker), `dialog` (such as workspace trust, MCP server approval or external `CLAUDE.md` imports), `permission_prompt`, `plan_approval` (the `Ready to code?` dialog of plan mode), `busy` (the spinner, or the greyed `❯` Claude Code draws while it works), or `unknown`.
- `title`: the title line of a `dialog` or `permission_prompt`, `Ready to code?` for `plan_approval`, else `null`.
- `options`: the choices of a `dialog`, `permission_prompt` or `plan_approval`, top to bottom, each as `{"key": "1", "label": "Yes, and use auto mode", "selected": true}`, else `null`. `key` is the number to press, or `null` for a dialog whose choices have no numbers, such as workspace trust, where the arrow keys move `selected` and Enter picks it. The labels differ between sessions: the first choice of a plan approval reads `Yes, auto-accept edits` when auto mode is not available, and a setting can add a choice above it, which moves every number. Pick a choice by its label, not by a fixed number. Only the choices after the plan's question count, since the plan itself can hold numbered lines.
- `reason`: why nothing was captured, else `null`: `ended`, `not_in_tmux`, `tmux_unavailable`, `pane_gone`, `pane_not_owned` (the pane id belongs to another process, as after a tmux server restart; `tmux_state` is `gone` for it), `pane_in_mode` (the pane is in copy mode or another tmux mode, which would take the keys instead of Claude Code) or `capture_failed`. Then `state` is `unknown` and `lines` is `null`.
- `pane`, `captured_at`, and `lines`: the last 20 lines of the pane as plain text, with runs of empty lines collapsed to one.

The text output adds a `screen` row and prints the lines under the table.

The classification is a heuristic over Claude Code's screen text, box drawing and colors, checked against captures of Claude Code 2.1.325, and it breaks when the UI changes. It answers `unknown` whenever the match is not certain, because a wrong `prompt_empty` makes a coordinator type into a dialog or over someone's half-typed text. The placeholder and typed text differ only in the dim attribute, so a capture without escape sequences never yields a prompt state. A capture also drops trailing spaces, so typed spaces look like an empty prompt; only the cursor shows them, and `prompt_empty` needs it right after `❯`. Known gaps: in a pane of about 12 lines or fewer, the plan approval dialog loses its `Ready to code?` title and all choices but the selected one, and reads `unknown`; a longer plan does not matter, because the dialog clips the plan and keeps the title; busy detection without a spinner relies on the dark theme's grey prompt glyph, and any other glyph color gives `unknown`; there are no light-theme captures; Edit and WebFetch permission prompts were not captured; a session in vim mode's NORMAL mode reads as `prompt_empty`, although it takes typed keys as commands, because its screen looks like that of a session without vim mode. Run the opt-in live contract test, `tests/test_live_screen.py`, after a Claude Code update.

Exit codes: `0` when one session is shown. `1` when SESSION matches nothing or more than one session in a step.

### watch

```bash
cc-session-tools watch                                         # every running session except the one this runs under
cc-session-tools watch --profile 'work*' --events=turns      # only turn_finished and waiting, in profiles matching a glob
cc-session-tools watch --name 'w*-order-planner' --until=turn_finished   # stop at the first finished turn of a worker
cc-session-tools watch w1-order-planner w2-order-planner       # these two sessions, until both have ended
cc-session-tools watch --busy-after=30m --waiting-after=5m --stop-after-quiet=2h
cc-session-tools watch --tmux-session current --idle-after=10m --idle-every=50m   # idle workers next to this session
cc-session-tools watch --context-above 75% --context-above 90% --events=context_high   # sessions whose context fills up
cc-session-tools watch --here --events-stream=stdout --timeout=10m
```

`watch` reports every change to the running sessions as one JSON event per line. It takes the filters of `list-active` and runs until an end you ask for, until its named sessions have ended, or until it gets SIGTERM or Ctrl-C. The last event is always `watch_ended` with a `reason`.

**Selection.** Without SESSION arguments the filters are evaluated again on every change. A session that starts later and matches joins with `session_started`. A running session that starts to match, for example after a rename or a status change under `--status`, joins with `session_matched`, and one that stops matching leaves with `session_unmatched`. Under `--tmux-session`, a window moved into that tmux session joins and one moved out leaves, with `tmux_moved` then `session_unmatched`, within the 3 seconds between two tmux calls. A session on another tmux server never joins, and a pane the session does not run in counts as gone, as in `list-active`. `--tmux-session current` is resolved once, at the start. The tmux session is matched by its id, not its name, so renaming it during the watch keeps its sessions in the watch; each of them reports `tmux_moved` with the new name. Under `--role`, a session whose role is not known when it starts joins with `session_matched` once its role is read: the watcher looks for it again every 3 seconds in the first minute after the session's start, and after that at every change of its registry record, such as its first turn. With SESSION arguments, resolved like a running SESSION of `inspect` after the filters, the watch follows exactly those sessions, and ends with reason `sessions_ended` once all of them have ended.

**The own session is left out.** Run under a Claude Code session, `watch` never reports that session: every event it delivers makes the session busy, which would be the next event. `--include-self` watches it anyway. Naming the own session as SESSION without `--include-self` is an error.

**Events.** Each has `kind`, `seq`, `observed_at` and `data`, and is kept small: it names the session and says what changed, nothing more. For every field of a session, run `cc-session-tools inspect <session> --json`; the header says so on its `details:` line.

`data.session` names the session with one string that `inspect` resolves to exactly that session, also after it ended: its address `<profile>/<name>`, or `<profile>/pid:<pid>` when the session has no name or another running session of its profile has the same name, compared without case. The string is computed for each event, so it changes with a rename, and moves between the two forms when a name stops or starts being unique. After the session ended, `inspect` finds `<profile>/<name>` among the transcripts by name, and `<profile>/pid:<pid>` through the auth-switch start log, which a session started without an auth-switch wrapper does not have.

| Kind | Group | `data` |
|------|-------|--------|
| `watch_started` | `lifecycle` | `selection`, and `sessions`: `{session, status}` for every matching session, plus `waiting_for` when it waits, `plan approval` included |
| `watch_ended` | `lifecycle` | `reason`: `until` (with `event` and `seq`), `timeout`, `quiet` (with `stop_after_quiet`), `sessions_ended`, `session_gone`, or `signal SIGTERM`/`signal SIGINT` |
| `session_started`, `session_matched` | `membership` | `session`, `status`: a new session that matches, or a running one that starts to |
| `session_ended` | `membership` | `session`, `reason`: `record_removed` (a clean exit) or `process_gone` |
| `session_unmatched` | `membership` | `session`: it no longer matches the filters |
| `status_changed` | `status` | `session`, `from`, `to`, and `waiting_for` when `to` is `waiting` |
| `turn_finished` | `turns` | `session`, `from`: `busy` or `waiting`, the status before `idle` |
| `waiting` | `turns` | `session`, `waiting_for`, and `plan_file` when `waiting_for` is `plan approval`: the path of `inspect`'s `plan.file`, or `null` when the transcript names none |
| `busy_long`, `waiting_long` | `thresholds` | `session`, `after` |
| `idle_long` | `thresholds` | `session`, `threshold`: the idle duration reached, such as `50m` |
| `context_high` | `thresholds` | `session`, `used`, `size`, `percent`, `mark`: the mark reached as `75%` or as the token count given, `mark_tokens`: the mark in tokens for this window, `model` |
| `renamed` | `changes` | `session` with the new name, `from`: the `session` string before the rename |
| `cwd_changed` | `changes` | `session`, `to`: the new cwd |
| `tmux_moved` | `changes` | `session`, `to`: `<tmux session>:<window index>`, or `null` when the session's tmux server no longer has the pane, or the pane no longer runs the session |

```json
{"kind":"turn_finished","seq":6,"observed_at":"2026-11-16T17:05:03.120Z","data":{"session":"work/w1-order-planner","from":"busy"}}
```

`waiting_for` is `plan approval` when the session's pane shows the plan approval dialog, as in `list-active`. Claude Code writes the record before it draws the dialog, so a pane that reads `unknown` is captured once more after 1 second, which delays the poll by that second. `plan_file` costs a read of the transcript, once per plan approval.

One status transition can yield two events: `busy` to `idle` is `status_changed` and `turn_finished`, anything to `waiting` is `status_changed` and `waiting`. Take `--events=turns` for the derived events alone. A change is reported for a session that matched before it, so a rename out of a `--name` glob is `renamed`, then `session_unmatched`. A session that starts to match is reported once, as `session_matched` with its new state.

`--events` and `--exclude-events` take kinds and groups, comma-separated and repeatable. `lifecycle` is always on. An unknown kind or group, or one this watch never produces, is an error at start: `busy_long` needs `--busy-after`, `waiting_long` needs `--waiting-after`, `idle_long` needs `--idle-after` or `--idle-every`, `context_high` needs `--context-above`, and a watch of named sessions never produces `session_started`, `session_matched` or `session_unmatched`.

**Ends and thresholds.** `--until=<kind>` stops after the first reported event of that kind. `--timeout` stops that long after the start, `--stop-after-quiet` when nothing was reported for that long; `busy_long`, `waiting_long`, `idle_long` and `context_high` do not count as activity. `--busy-after` reports `busy_long` once per busy stretch of a session, `--waiting-after` reports `waiting_long` once per waiting stretch. A stretch starts at the record's `statusUpdatedAt`, so a session that was already busy for an hour when the watch started gets its `busy_long` at once.

**Idle thresholds.** `--idle-after` reports `idle_long` once per idle stretch, when a session has had the status `idle` for that long since its last model response, in its transcript or any subagent's. It is repeatable: `--idle-after 10m --idle-after 50m` gives two events per stretch. `--idle-every` reports `idle_long` again at every multiple of its duration within the stretch, for example to decide whether to keep a session's prompt cache warm. A stretch ends at the next model response, so a message that makes the session answer starts the count again. A turn without a response, such as a local command, does not. A session without any response counts from its start. A resumed session counts from the start of its process, not from a response of an earlier run, and a session whose id changes by `/clear` or an in-process `/resume` counts from that change: during the watch the record shows it, before the watch the auth-switch start log does. When a watch starts, or polls late, past several thresholds of one stretch, it reports the longest one once, so a session idle for three hours when the watch starts gets one `idle_long`. The watcher reads the end of the transcript when a session becomes idle and again before it reports `idle_long`, because a background subagent can answer while its session stays idle.

**Context thresholds.** `--context-above` reports `context_high` when a session's context fill reaches a mark, so a coordinator can compact a worker before its context runs out. The fill is `context_window.used` of `list-active --json`: the input and cache tokens of the newest response of the main conversation. The option is repeatable: `--context-above 75% --context-above 90%` gives one event at each mark. A mark is a percentage of the session's context window, such as `75%`, from `1%` to `99%`. A bare number below 100 is a percentage too, but write the `%`: the number alone is easy to misread as tokens. A number from 100 on is a token count, such as `150000`, and `k` and `M` stand for thousands and millions, as in `150k` or `1M`. A token mark is the same number of tokens on every window: `150k` is 75% of a 200k window and 15% of a 1M one, and a mark above a session's window never fires for it. `75%` needs no quotes in bash or zsh, which treat `%` as special only in job control commands such as `fg %1`.

Each mark is reported once, and again only after the fill dropped below it: after a compaction, after a `/clear`, or when a smaller response follows. A session that passes several marks between two reads, or that is past them when the watch starts, gets one event, for the highest mark. The watcher looks at a session's transcript every 3 seconds and reads the fill only when the transcript changed since the last read. A read takes 1 to 2 milliseconds, also for a transcript of 430 MB, because it reads backwards from the end to the newest response. A session that passes a mark gets its event within about 3 seconds of the response that passed it.

Durations are `90s`, `30m`, `2h`, `1h30m` or plain seconds.

**Startup header.** The header, the `ended: <reason>` line and errors go to stderr, each line prefixed `cc-session-tools watch: ` and never starting with `{`, so in merged output every line that starts with `{` is an event:

```text
cc-session-tools watch: selection: profile=work* (profiles: work-team, work-team2); 4 matching now
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

**Cost.** No daemon: the watcher stats every registry record of every profile once a second, reading a record again only when its mtime or size moved, and lists the sessions through cc-blackbox only when a new record appears. tmux is asked once every 3 seconds, once per tmux server the sessions run on. With `--context-above`, the transcript of every matching session is stat'ed every 3 seconds and its context fill read when it changed. A status that lasts less than one poll can be missed.

Exit codes: `0` when the watch ends, whatever the reason. `1` when SESSION matches nothing or several sessions, names the own session, a `--profile` glob matches no profile, `--tmux-session` matches no tmux session, or `claude-message` is not possible. `2` for a usage error: an unknown event kind or group, a bad duration, a `--context-above` mark that is not a percentage from 1% to 99% or a token count above zero, an `--until` kind the watch does not report, or `--tmux-session` with `--no-tmux`.

#### bypassPermissions sessions

Claude Code holds a message for its user's approval when the receiving session runs in `bypassPermissions` mode and cannot verify the sender. The events then show up as "Held peer message … The sender did not attest its permission mode" instead of reaching the model. Sessions in other modes, auto included, deliver them.

Start `cc-session-tools watch` from the receiving session's own Bash tool. Claude Code delivers messages from its own child processes even in bypass mode: on Linux it checks the process tree, and on macOS and in containers where `claude` runs as PID 1 the watcher also proves it with the session's `CLAUDE_CODE_MESSAGING_TOKEN`, which cc-blackbox 0.14.1 and later sends. A watcher started from another terminal or another session stays held.

If the events are still held, avoid setting `"crossSessionInbound": "accept"` in your user settings. It lets every bypass session take messages from any local sender, other Claude Code sessions included, so a session that read untrusted input, such as a web page or a tool result, could drive one that runs commands without asking. Limit it to one run with `claude --settings '{"crossSessionInbound":"accept"}'`, or run the watching session in auto mode. See [cross-session messaging](https://code.claude.com/docs/en/cross-session-messaging) and [`crossSessionInbound`](https://code.claude.com/docs/en/settings-reference#crosssessioninbound).

### limits

```bash
cc-session-tools limits                       # every profile and its rate limits
cc-session-tools limits --profile 'work*'     # profiles whose name matches a glob
cc-session-tools limits --json | jq …         # every field, as JSON
```

Claude Code passes an account's rate limits only to its [status line](https://code.claude.com/docs/en/statusline) command. The launchers of [claude-code-auth-switch](https://github.com/fprochazka/claude-code-auth-switch) put a status line in front of yours that records them in `auth-switch-rate-limits.json` in the profile dir, merged from every session of the profile, and [cc-blackbox](https://github.com/fprochazka/cc-blackbox) reads that file. A profile has data once a session started through such a launcher got a response.

The table has a row per profile and the columns **Profile**, **5h** and **7d** (the five-hour and weekly windows of a claude.ai subscription), **Spend** (the spend limit of a Claude apps gateway, only when a profile has one) and **Measured** (how long ago a session last reported the values). A window shows as `23% · resets in 1h12m (16:40)`, green below 65%, yellow below 85% and red from there. A window whose reset time has passed shows dimmed as `reset 14:00 · was 87%`. A profile without data shows `no data`: no session of it was started through a launcher yet, or its account has no rate limits, as with an API key or Vertex AI.

**The values are a lower bound with no expiry.** They are the last ones a session reported and stay valid until their window resets, whether a session still runs or not; Measured says how old they are. Usage on another machine or on claude.ai shows only with the next response of a session here. After a reset the current usage is unknown until a session reports the new window, so the table shows the usage from before the reset instead of 0.

**`--json`** prints an array with one object per profile: `profile`, `config_dir`, `path` (the file), `measured_at`, `written_at`, `session_id`, `model` and `claude_version` of the session that wrote the file last, and `windows`, keyed by window name (`five_hour`, `seven_day`, `spend_limit`, and any window a newer Claude Code adds), each with `used_percentage`, `resets_at`, `reset` (whether `resets_at` had passed when the command ran; never `true` without `resets_at`), `measured_at`, `changed_at`, `session_id` and `raw` (the window as the file holds it, with `used_usd`, `limit_usd` and `period` for a spend limit). Every key but `profile` and `config_dir` is `null` for a profile without data.

Exit codes: `0`, also when no profile has data. `1` when a `--profile` glob matches no profile.

## Requirements

- Python 3.12+
- [ripgrep](https://github.com/BurntSushi/ripgrep) — optional. It makes `search --rg` faster; without it a built-in scan runs.
- git — only for worktree-aware scoping and the branch in `list-active`. Outside a repo every subcommand falls back to the exact current directory.
- tmux — optional. Without it `list-active` and `watch` report every tmux pane as unknown, and `inspect --screen` captures nothing. The tmux server of a session that runs on its own server is known only on Linux, from its process environment; elsewhere its pane is looked up on the caller's server and is `gone` unless the session runs in it.

## Project structure

```
src/cc_session_tools/
├── __init__.py        # package version
├── cli.py             # root Typer app, --version, the error guard
├── search_cmd.py      # the `search` command
├── distill_cmd.py     # the `distill` command
├── timeline_cmd.py    # the `timeline` command: the markdown and JSON of a session's time
├── tool_stats_cmd.py  # the `tool-stats` command: tool time per tool, Bash group, command, permission and subagent role
├── redact.py          # the cleanup of quoted text and the short duration and token formats of timeline and tool-stats
├── list_active_cmd.py # the `list-active` command
├── inspect_cmd.py     # the `inspect` command
├── inspection.py      # one session, running or ended: the lookup and its fields
├── plan.py            # the plan file of a session in plan mode, from its transcript
├── screen.py          # what a captured tmux pane shows: empty prompt, input, dialog, permission prompt, plan approval, busy
├── watch_cmd.py       # the `watch` command: options, header, stream and changelog
├── watcher.py         # the polling loop: registry records in, session events out
├── watch_events.py    # event kinds and groups, the --events filter, durations
├── limits_cmd.py      # the `limits` command
├── active.py          # running sessions with the live tmux pane and git location, the JSON shape
├── console.py         # the console every table prints on
├── gitrepo.py         # git worktree resolution and the branch of a dir
├── discovery.py       # which sessions belong to a cwd, distill target resolution
├── titles.py          # display title derived from the first user message
├── search.py          # ripgrep shell-out with the built-in fallback
├── timefmt.py         # relative "time ago" formatting
├── segments.py        # a transcript split at its compactions
├── session_time.py    # where a session's time went: turns, tool calls, subagents, idle gaps, user waits, compactions
├── bash_groups.py     # Bash commands grouped by purpose (test, build, wait, git, …), with the user's own groups file
├── render.py          # condensed conversation -> markdown, one file per segment
└── stats.py           # size and token estimates

tests/                # unit tests; test_live_screen.py is the opt-in live contract test (pytest -m live)
└── fixtures/screen/  # tmux captures of real Claude Code panes, one per screen state

.claude-plugin/marketplace.json          # Claude Code plugin marketplace
coding-agent-plugins/claude-code/        # the plugin
├── .claude-plugin/plugin.json
├── commands/{search,distill}.md
└── skills/{session-tools,active-sessions,session-watch}/SKILL.md
```

## Developing

You need Python 3.12+ and uv. Set up the environment, then run the checks CI runs:

```bash
uv sync --dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

`uv run ruff format .` fixes the formatting. Run the CLI from the clone without installing it with `uv run cc-session-tools …`.

**Work in a git worktree, never in the main checkout.** The installed `cc-session-tools` is an editable install of the main checkout, so every Claude Code session on the machine runs its code the moment it changes. Never edit, check out or pull there; create a worktree under `.worktrees/`, which git ignores, and test it with `uv run`. The editable install picks up code changes by itself, but not dependency changes: those need a reinstall, see [Releasing](#releasing).

**The tests never read your real Claude Code setup.** An autouse fixture removes `CLAUDE_CONFIG_DIR`, `CLAUDE_PROJECTS_DIR` and the variables of the session the tests run under, and points `HOME` at a temporary dir, because the tests often run inside a Claude Code session. The `list-active`, `inspect` and `watch` tests need Linux; the `--rg` CLI tests skip without ripgrep.

**To test against an unreleased cc-blackbox**, point `[tool.uv.sources]` at its worktree with `cc-blackbox = { path = "<cc-blackbox worktree>", editable = true }` and widen the pin. Before landing, release cc-blackbox, restore the PyPI pin and run `uv lock --refresh-package cc-blackbox`. CI installs with `uv sync --locked`, so a path source cannot land on master.

### Commit hooks

The tools show what runs on your machine: profile names, session names, branches, ticket keys, paths and claude.ai remote-session links. All of that ends up in terminals, captured panes and test fixtures, and from there it can slip into a commit. `scripts/hooks` holds a pre-commit and a commit-msg hook that refuse a commit when a staged line or the commit message matches a private identifier. The identifiers live in a file outside the repository, because listing them here would publish the very strings the hooks exist to keep out. Set it up once per clone:

```bash
git config core.hooksPath scripts/hooks
git config hooks.forbiddenPatternsFile ~/.config/git/forbidden-patterns.txt  # any path outside the repo
```

The file holds one extended regex per line, matched case-insensitively; blank lines and `#` comments are ignored. Until `hooks.forbiddenPatternsFile` points at a file with at least one pattern, both hooks refuse every commit and print how to fix it. The hooks check only the lines a commit adds, so a captured pane or other fixture needs its private names replaced with made-up ones before you commit it.

### The live contract test

`tests/test_live_screen.py` checks the `inspect --screen` classifier against a real Claude Code session. It is excluded from the default run and from CI. Run it after a Claude Code update:

```bash
uv run pytest -m live -s
```

It starts the session in a temporary tmux session and drives it through its startup dialogs, typed input, a busy turn and a permission prompt. A second session runs in plan mode, writes a short plan and shows the plan approval dialog, which the test rejects with Esc. It runs against a real, logged-in profile and spends a few model calls. These variables change it:

- `CC_SESSION_TOOLS_LIVE_CLAUDE`: the command that starts Claude Code, `claude` by default. An auth-switch wrapper such as `claude-personal` picks the profile.
- `CC_SESSION_TOOLS_LIVE_MODEL`: the model, `haiku` by default.
- `CC_SESSION_TOOLS_LIVE_FRESH_DIR=1`: run in a new temporary directory and require the workspace trust, MCP server and external imports dialogs.

By default the session runs in `~/.cache/cc-session-tools/live-test`. Claude Code stores the trust answer for that directory as a project entry in the profile's config, so the test adds at most one entry, ever. It also remembers the MCP and imports answers there, so after the first run the dialogs do not show; the test answers the ones that do, always the same way. With `CC_SESSION_TOOLS_LIVE_FRESH_DIR=1`, every run adds one more project entry to the profile's config. Every run, in either mode, also leaves the session's transcript in the profile: a new `<session-id>.jsonl` under `<config dir>/projects/<encoded dir>/`, which the test does not delete. Those sessions show up in `search --all`. The unit tests cover the dialogs through captured fixtures either way.

## Releasing

A release is an annotated tag on master and a GitHub release with notes. There is no PyPI package: users install from a clone, so a change reaches them when they pull master. The tag marks which commit is which version, and the release notes say what changed.

The version lives in five places, and one release bumps all five to the same number:

- `pyproject.toml` (`version`)
- `src/cc_session_tools/__init__.py` (`__version__`, what `--version` prints)
- `uv.lock` (the project's own entry; `uv lock` updates it)
- `.claude-plugin/marketplace.json` (`plugins[0].version`)
- `coding-agent-plugins/claude-code/.claude-plugin/plugin.json` (`version`)

Bump the minor version for features and for breaking changes, and the patch version for fixes. Say what breaks in the release notes.

The skills embed the `--help` output of the subcommands they cover. When a subcommand's options or help text change, regenerate those blocks with `COLUMNS=80`, for example `COLUMNS=80 uv run cc-session-tools inspect --help`, so the wrapping stays stable.

CI runs on every push to master and on pull requests: ruff check, ruff format check and mypy, and the tests on Python 3.12, 3.13, 3.14 and 3.15. It does not run on tags.

To release version `X.Y.Z`:

1. Bump the version in the five places. Run `uv lock` to update `uv.lock`.
2. Commit the five files as `Release X.Y.Z`, the last commit of the change.
3. Fast-forward master to the commit with `git push origin HEAD:master`, without a pull request. Wait until the CI run of that commit is green: find its id with `gh run list -w ci.yml -L 1`, then run `gh run watch <run id> --exit-status`.
4. Tag the commit and push only the tag:

   ```shell
   git tag -a vX.Y.Z -m "claude-code-session-tools X.Y.Z"
   git push origin vX.Y.Z
   ```

5. Write the release notes to a file and create the GitHub release:

   ```shell
   gh release create vX.Y.Z --verify-tag --latest --title vX.Y.Z --notes-file <notes.md>
   ```

6. Check that `gh release list` shows `vX.Y.Z` as `Latest`. A release is not done until it does.

The release title is the tag name, without the project name. The notes have this format:

- One sentence that says what the release does for the user, followed by "Unofficial, not affiliated with or endorsed by Anthropic."
- A code block with the install commands from [Installation](#installation).
- `## Added`, `## Changed`, `## Fixed` and `## Breaking` sections, only those that apply. Each bullet names the command or option and says what it does or what to change.

Users pick up a release by pulling their clone and reinstalling the tool, then updating the plugin:

```bash
git pull
uv tool install --editable . --reinstall
claude plugin marketplace update fprochazka-claude-code-session-tools
claude plugin update session-tools@fprochazka-claude-code-session-tools
```

After a release that raises the cc-blackbox pin, reinstall with `uv tool install --editable . --force` instead: a plain `--reinstall` has kept the old cc-blackbox once. A running Claude Code session sees a new skill only after a restart.

## License

MIT
