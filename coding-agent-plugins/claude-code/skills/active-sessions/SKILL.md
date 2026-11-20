---
name: active-sessions
description: CLI for listing the Claude Code sessions that run on this machine right now, across every Claude Code profile — their profile, name, status, context fill, branch, live tmux pane and messaging address — and for inspecting one session, running or ended. Use when you need to find another running session, check which sessions are busy, find the tmux pane or profile a session runs in, get a session's messaging address before sending it a message, peek at what another session shows, check whether its prompt is empty or it shows a startup dialog or permission prompt before typing into its pane, or find out what became of a session that ended and how to resume it, or see how much of each profile's rate limits (5-hour and weekly) is used. Triggered by requests involving running sessions, other agents, "which sessions are busy", "where is session X", "what is the pane of", "is X still running", "which sessions worked in the last hour", "what did session X cost", "how full is the context of", "is it safe to type into", "rate limits", "usage limits", "which profile has quota left", or tmux panes of Claude sessions.
trigger-keywords: running sessions, active sessions, live sessions, other sessions, tmux pane, session status, busy sessions, messaging address, ended session, resume session, recently active sessions, session cost, context window, context fill, pane screen, empty prompt, startup dialog, permission prompt, plan approval, plan file, rate limits, usage limits, quota, list-active, inspect, limits
allowed-tools: Bash(cc-session-tools --help), Bash(cc-session-tools list-active:*), Bash(cc-session-tools inspect:*), Bash(cc-session-tools limits:*)
---

# cc-session-tools list-active, inspect and limits

`list-active` lists the running sessions; `inspect` shows one session, running or ended; `limits` shows the rate limits of every profile's account.

## list-active

Claude Code writes one registry record per running session into `<config dir>/sessions/<pid>.json`. `list-active` reads the records of every profile (`~/.claude` and `~/.claude-profiles/*`), drops records whose process has ended, and resolves two things live: the git branch of the session's cwd, and where its tmux pane is now. It never writes anything and never contacts a session.

```
Usage: cc-session-tools list-active [OPTIONS]

  List the running Claude Code sessions of every profile.

  One row per session, by profile and then start time. Role is the auth-switch
  role its process was started with, a column only when a session has one.
  Active is the time since the session's last model response, in its
  transcript or a subagent's; a session without one shows its start, dimmed.
  Ctx is how full the context is, as the session's status line shows it; after
  a compaction it is 0 until the next response. The tmux pane is the pane id
  with the tmux session and window that pane is in now, read from the tmux
  server. The inspect command shows one session.

Options:
  -p, --profile <str>             Only sessions of the profiles whose name
                                  matches this glob, for example 'work*'.
                                  Repeatable.
  --status <busy|shell|idle|waiting>
                                  Only sessions with this status. Repeatable.
  --here                          Only sessions whose cwd is in the current
                                  git repo or any of its worktrees; outside a
                                  repo, the exact current directory.
  --tmux / --no-tmux              Only sessions that run in tmux, or only
                                  those that do not.
  --name <str>                    Only sessions whose name matches this glob,
                                  case-insensitively.
  --tmux-session <name|current>   Only sessions whose tmux pane is now in this
                                  tmux session, by the running tmux server, so
                                  a window moved with move-window counts.
                                  'current' is the tmux session this command
                                  runs in.
  --role <str>                    Only sessions whose auth-switch role matches
                                  this glob, case-sensitively, for example
                                  'worker-*'. The role is the one its running
                                  process was started with. A session without
                                  a role never matches. Repeatable.
  --active-within <duration>      Only sessions that got a model response
                                  within this long, for example 30m, 2h or
                                  1h30m. A session without a response yet
                                  counts from its start.
  --json                          Print every field of every session as a JSON
                                  array.
  --help                          Show this message and exit.
```

## How to use it

- **Prefer `--json` and `jq`.** The table is for people and truncates long branches and tmux session names. The JSON has every field and is the stable interface.
- **`--profile` and `--name` take globs.** `--profile 'work*'` covers every profile whose name starts with `work`; a plain name matches only that profile. A `--profile` glob that matches no profile exits with `1` and lists the known profiles.
- **Filters combine with AND.**
- **`--role <glob>`** keeps the sessions whose role matches, case-sensitively; repeatable. A session without a role never matches. The table shows a Role column only when a listed session has a role.
- **`--tmux-session <name>`** keeps the sessions whose pane is in that tmux session now, by the tmux server, so a window moved there with `move-window` counts. `--tmux-session current` is the tmux session this command runs in, from `$TMUX_PANE`: the sessions next to you. A name that no tmux session has, or `current` outside tmux, exits with `1` and lists the tmux sessions.
- **`list-active` only lists.** For one session run `inspect`; `list-active <SESSION>` exits with `2` and prints the `inspect` command.
- **"Active in the last hour" is `--active-within 1h`**, never a filter on `updated_at`. The record's `updated_at` changes with the status, but a local command such as `/login` moves it without the model, so a session idle for days can have a fresh `updated_at`. It can also lag behind a session that answered later. `--active-within` reads the transcript: the newest model response in it or in any subagent's, else the session's start when it has no response yet.
- **`--here`** uses the scope of `search`: the main checkout, every worktree, and the legacy `<repo>-worktrees/` sibling dir. It matches the session's current `cwd`, which changes when the session moves into a worktree.
- The command takes about 0.2 seconds for 35 sessions. The Active and Ctx columns read the end of each transcript. `--json` also reads the cost, which costs a read of the whole transcript of a session that never exited: about 0.4 seconds for 35 sessions.

## The JSON fields

`list-active --json` prints an array of objects; `inspect SESSION --json` prints one object with the same keys and a few more. Timestamps are ISO 8601 in UTC, paths are strings, and a missing value is `null`, never an absent key.

- `profile`, `profile_config_dir`: the profile name and its config dir.
- `profile_source`: how sure the profile is. `log`, `environ`, `claude_json` and `dir` are reliable. `inferred` is a guess between profiles that share one `sessions/` dir.
- `address`: `<profile>/<name>`, or `<profile>/pid:<pid>` for a session without a name.
- `pid`, `session_id`, `name`, `name_source` (`user`, `peer`, `derived`, `collision`, `auto`, `hook`).
- `role`: the claude-code-auth-switch role that the session's running process was started with, from the start log next to its transcript. It is there before the first prompt, and stays after `/clear`. `null` for a session started without a role.
- `status`: `busy`, `idle`, `shell` or `waiting`. `waiting_for`: what a `waiting` session waits for, from the record's `waitingFor`, such as `permission prompt`, and `null` in every other status. The record says `permission prompt` for the plan approval dialog too; for a session in tmux whose pane shows that dialog, `waiting_for` is `plan approval`. `raw.waitingFor` keeps the record's value. The table shows it as `waiting: <reason>`. `kind` (`interactive`, `bg`, `daemon`, `daemon-worker`), `entrypoint`, `version`.
- `started_at`: when this process started; a resumed session started again.
- `updated_at`: the last change of the registry record. It changes with the status, and a local command such as `/login` moves it too. It is not activity: a fresh value does not mean the session worked, and a session can answer well after it.
- `last_activity_at`: the newest model response, in the main transcript or any subagent's. This is when the session last worked. `null` before its first response.
- `last_record_at`: the newest timestamp of any transcript record. Records without the model, such as `/login`, attachments and rename notices, count too, so it can be days after `last_activity_at`.
- `cwd`: the session's current dir. `launch_cwd`: the dir its process started in, or `null` before the first transcript record.
- `git_branch`: the short branch of `cwd`, `null` when detached, unborn or outside git. `git_worktree_root`: the checkout that holds `cwd`. `git_repo_root`: the main checkout, the same for every worktree of a repo.
- `tmux_pane`: the pane id such as `%14`, or `null` outside tmux. It is the address to use with tmux.
- `tmux_state`: `present`, `gone` (the tmux server has no such pane), `unknown` (tmux is missing or has no server), or `null` outside tmux.
- `tmux_live`: `{session, window_index, window_id, pane_title}` from the running tmux server, or `null` when the pane is not `present`.
- `tmux_recorded`: the record's own `<session>:@<window>.%<pane>`. Claude Code writes it once at startup, so its session part goes stale when a window moves to another tmux session. **Trust `tmux_live`, never the session part of `tmux_recorded`.**
- `messaging_socket`, `messaging_address` (`uds:<socket path>`, the address for messaging the session), `can_receive_messages`, `peer_protocol`, `peer_features`.
- `bridge_session_id`, `scratchpad_dir`, `scratchpad_dir_inferred`, `transcript_path` (`null` before the session writes one), `record_path`.
- `cost`: the totals Claude Code records when a process of the session exits, or `null`. **It is never the current cost of a running session.** For an ended session it is the session's total. For a running session that was resumed it is the total as of its last exit and leaves out the current run; a running session that never exited has `null`. Keys: `total_cost_usd`, `has_unknown_model_cost` (`true` when a model without a known price was used, so the total is too low), `started_at`, `duration_seconds` (wall-clock time the processes ran, idle time included), `api_duration_seconds`, `api_duration_without_retries_seconds`, `tool_duration_seconds`, `lines_added`, `lines_removed`, and `model_usage`: per model id, `input_tokens`, `output_tokens`, `thinking_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, `web_search_requests`, `cost_usd`.
- `context_window`: how full the session's context is, the same numbers Claude Code gives its status line, or `null` when the session has no response yet, as right after a `/clear`. Keys: `used` (the input and cache tokens of the newest response of the main conversation; output tokens do not count), `size` (the window in tokens), `percent`, `compacted`, `model`, `size_source` and `measured_at` (the time of the response, or of the compaction). **After a compaction `used` is 0 and `compacted` is `true` until the session answers again**: the real fill is unknown until then, and the compaction summary alone is far below it. Subagents do not count; each has its own context. No transcript record holds the window size, so it comes from the model id; `size_source` says how: `model_tag` (a `[1m]` suffix), `model_table` (cc-blackbox's table of models), `used_exceeds_table` (the fill was above the table's size, so the window is the 1M one), `env_disable_1m` (`CLAUDE_CODE_DISABLE_1M_CONTEXT` caps it at 200k; only a running session's settings and environment show it), or `default` (an unknown model, which Claude Code assumes to be 200k; the table dims that size). A provider that refuses the 1M window does not show in the transcript, so `size` can be too large there. The table shows the column as `312k/1M 31%`, or `0k/1M compacted`.
- `alive`: always `true`. The command leaves out records whose process has ended.
- `raw`: the whole registry record, for fields the list above does not model.

## jq recipes

```bash
# Names and status of the busy sessions
cc-session-tools list-active --status busy --json | jq -r '.[] | "\(.address)\t\(.status)"'

# Pane id of a session by name
cc-session-tools inspect w1-order-planner --json | jq -r '.tmux_pane'

# Sessions of one profile working in this repo, with their branch and pane
cc-session-tools list-active --here --profile work-team --json | jq -r '.[] | "\(.name)\t\(.git_branch)\t\(.tmux_pane)"'

# Messaging address of a session
cc-session-tools inspect cc-auth-switch --json | jq -r '.messaging_address'

# Sessions that worked in the last hour, newest response first
cc-session-tools list-active --active-within 1h --json | jq -r 'sort_by(.last_activity_at) | reverse | .[] | "\(.last_activity_at)\t\(.address)"'

# What an ended session cost
cc-session-tools inspect old-worker --json | jq '.cost | {total_cost_usd, duration_seconds}'

# Sessions whose context is over 15% full, fullest first
cc-session-tools list-active --json | jq -r 'map(select(.context_window.percent > 15)) | sort_by(-.context_window.percent) | .[] | "\(.context_window.percent)%\t\(.address)"'

# Is a session still running, and how to resume it
cc-session-tools inspect old-worker --json | jq -r '"\(.state)\t\(.resume_command)"'

# Workers by role, with their pane
cc-session-tools list-active --role 'worker-*' --json | jq -r '.[] | "\(.role)\t\(.address)\t\(.tmux_pane)"'

# Sessions in the tmux session this command runs in
cc-session-tools list-active --tmux-session current --json | jq -r '.[] | "\(.tmux_pane)\t\(.address)"'

# Where each pane really is now
cc-session-tools list-active --tmux --json | jq -r '.[] | "\(.tmux_pane)\t\(.tmux_live.session // "?"):\(.tmux_live.window_index // "?")\t\(.name)"'

# What a session's pane shows, and whether its prompt is empty
cc-session-tools inspect w1-order-planner --screen --json | jq '.screen | {state, title, reason}'

# A plan approval: the choices on screen and the plan file
cc-session-tools inspect w1-order-planner --screen --json | jq '{options: .screen.options, plan: .plan.file, sha256: .plan.sha256}'
```

## inspect

```
Usage: cc-session-tools inspect [OPTIONS] {SESSION}

  Show every field of one Claude Code session, running or ended.

  A running session shows what list-active knows about it, with state running.
  An ended session shows what its transcript and the auth-switch start log
  recorded, with state ended: the profile that ran it last, its last name,
  title and cwd, when it started and last got a model response, every start,
  and the command that resumes it. Both show the cost Claude Code recorded
  when the session last exited; for a running session it leaves out the
  current run.

  --screen reads the pane the way it looks now. The classification matches
  Claude Code's screen layout and answers unknown whenever it is not certain,
  so treat anything but prompt_empty as unsafe to type into.

Arguments:
  SESSION  A running session by session id, id prefix, name, <profile>/<name>,
           <profile>/pid:<pid> or pid; else an ended session by session id or
           id prefix, by its name or any earlier title, or by its pid from the
           auth-switch start log.  [required]

Options:
  --days <int range>  How far back to look for an ended session by name, title
                      or pid: transcripts active in the last this many days. A
                      session id or id prefix is looked up in every
                      transcript.  [default: 14; x>=1]
  --screen            Also capture the session's tmux pane and say what it
                      shows: prompt_empty, prompt_has_input, dialog,
                      permission_prompt, plan_approval, busy or unknown, with
                      the choices of a dialog and the last lines. Typing into
                      the pane is safe only at prompt_empty.
  --json              Print every field as one JSON object.
  --help              Show this message and exit.
```

- **Running first.** SESSION is looked up among the running sessions: session id, name, `<profile>/<name>`, `<profile>/pid:<pid>`, pid, or a unique session id prefix. The `session` strings of `cc-session-tools watch` events are of these forms.
- **Then ended sessions**, in this order: by session id or id prefix in every transcript; by the session's name or any earlier name or title, compared without case, among transcripts active in the last `--days` days (14 by default); by its pid from the auth-switch start log in the same window. `<profile>/` in front keeps sessions that profile ran last. A renamed session is found under an old name, and shown as running if it still runs.
- **Several matches** in a step exit with `1` and list the candidates. Narrow with `<profile>/`, a longer id prefix, or the full session id.
- **`--json` keys:** `state` (`running` or `ended`), every key above, `title`, `title_source`, `profile_starts` (`ts`, `pid`, `source`, `profile`, `role`, `role_prompt`, `role_prompt_mode` per start or resume), `resume_command`, and `plan`: the plan file of a session that entered plan mode, as `file`, `slug`, `exists`, `sha256` and `modified_at`, from the newest `plan_mode` or `plan_mode_exit` attachment of its transcript, or `null` when it never did. A rejected plan is revised in the same file; compare `sha256` to tell whether it changed. For an ended session the live-only fields (`pid`, `status`, tmux, messaging, `record_path`, `raw` …) are `null`, `cwd` is the last cwd its transcript recorded, `launch_cwd` the first, and `git_branch` the last.
- **Resume an ended session** with its `resume_command` — tell the user the command rather than run it; it starts an interactive session.
- A name lookup among ended sessions reads every transcript of the window; expect about a second.

### --screen

`--screen` captures the session's tmux pane and adds a `screen` object as the last key of `--json`, and a `screen` row plus the last lines in text:

- `state`: `prompt_empty`, `prompt_has_input`, `dialog`, `permission_prompt`, `plan_approval` (the `Ready to code?` dialog of plan mode), `busy` or `unknown`.
- `title`: the title line of a `dialog` or `permission_prompt`, such as `Accessing workspace:` (workspace trust), `New MCP server found in this project: <name>`, `Allow external CLAUDE.md file imports?` or `Bash command`, and `Ready to code?` for `plan_approval`. `null` for the other states.
- `options`: the choices of a `dialog`, `permission_prompt` or `plan_approval`, top to bottom, as `{key, label, selected}`; `null` for the other states. `key` is the number to press, or `null` when the choices have no numbers and the arrow keys and Enter pick one. Labels and numbers vary between sessions (`Yes, and use auto mode` or `Yes, auto-accept edits`, an extra first choice from a setting), so find a choice by its exact label and press its `key`, never a fixed number.
- `reason`: why nothing was captured, else `null`: `ended`, `not_in_tmux`, `tmux_unavailable`, `pane_gone`, `pane_not_owned` (the pane id now belongs to another process, as after a tmux server restart), `pane_in_mode` (the pane is in copy mode or another tmux mode, which would take the keys instead of Claude Code) or `capture_failed`. With a reason, `state` is `unknown` and `lines` is `null`.
- `pane`, `captured_at`, and `lines`: the last 20 lines of the pane as plain text, with runs of empty lines collapsed to one.

The classification reads Claude Code's screen layout and answers `unknown` whenever it is not certain, so it breaks safely when the UI changes:

- **Type only at `prompt_empty`.** `prompt_has_input` means someone, often the user, has half-typed text there; typing would append to it. A dimmed `Try "…"` placeholder or a dimmed suggestion counts as empty, because typing replaces it, but only with the cursor right after `❯`: typed spaces do not show in a capture, and they give `unknown`. A `[Pasted text #1 +40 lines]` or `[Image #1]` marker is input.
- **Vim mode is invisible.** A session with vim mode on, in NORMAL mode, reads as `prompt_empty`, but takes typed keys as vim commands. Its screen looks like that of a session without vim mode. If the session's footer shows `-- INSERT --`, it uses vim mode and is in INSERT mode now; if you know a session uses vim mode, send `Escape` then `i` before you type.
- **`busy`** is the spinner above the prompt box or the greyed `❯` that Claude Code draws while it works, whatever the box holds. Text typed now is queued; wait for the turn to end.
- **`unknown`** covers everything else: shell mode (`!`), the transcript view, an unfamiliar overlay. Do not type; capture the pane again later or look at `lines`.
- The state is a snapshot. Check it right before you type, and check `title` before you answer a dialog.

## Acting on a pane

Every tmux command accepts the pane id as a target, wherever the window has moved:

- `tmux capture-pane -p -t %14 | tail -40` shows what the session shows now, without touching it. Use it to peek.
- `tmux switch-client -t %14` moves the user's tmux client to the session. Run it only when the user asks to go there.
- `tmux send-keys -t %14 …` types into the session. Never do this without the user's explicit request; to talk to another session, send it a message at its `messaging_address` instead. When you do type, run `cc-session-tools inspect <session> --screen --json` first and type only when `.screen.state` is `prompt_empty`.

When `tmux_state` is `gone`, the tmux server has no pane with that id, so the session no longer runs in the pane it started in. When it is `unknown`, this process cannot reach the tmux server the session runs in.

## limits

```text
Usage: cc-session-tools limits [OPTIONS]

  Show the rate limits of every profile's account: the 5-hour and weekly
  windows, and a gateway's spend limit.

  claude-code-auth-switch's launchers record what Claude Code passes to the
  status line, so a profile has data once a session started through a launcher
  got a response. The values are the last ones a session reported, as old as
  Measured says, and stay valid until their window resets; usage on other
  machines or claude.ai shows with the next response here. A window whose
  reset time has passed shows as reset, with the usage from before the reset.

Options:
  -p, --profile <str>  Only the profiles whose name matches this glob, for
                       example 'work*'. Repeatable.
  --json               Print every profile's limits as a JSON array.
  --help               Show this message and exit.
```

The values come from the status line of claude-code-auth-switch's launchers, which records what Claude Code passes it; a profile without such a session shows `no data` (`windows` is `null` in `--json`). Read them as a lower bound with no expiry:

- **`measured_at`** is when a session last reported the window. The value stays valid until `resets_at`, however old, but usage on another machine or claude.ai is not in it until a session here gets its next response.
- **`reset: true`** means `resets_at` had passed when the command ran: `used_percentage` is the usage from before the reset, and the current usage is unknown, not 0.
- Windows: `five_hour` and `seven_day` for a claude.ai subscription, `spend_limit` for a Claude apps gateway (its `raw` holds `used_usd`, `limit_usd` and `period`), and any window a newer Claude Code adds.

```bash
# Weekly usage of each profile, highest first
cc-session-tools limits --json | jq -r 'map(select(.windows.seven_day)) | sort_by(-.windows.seven_day.used_percentage) | .[] | "\(.windows.seven_day.used_percentage)%\t\(.profile)"'

# Profiles with five-hour headroom left, current windows only
cc-session-tools limits --json | jq -r '.[] | select(.windows.five_hour and (.windows.five_hour.reset or .windows.five_hour.used_percentage < 80)) | .profile'
```
