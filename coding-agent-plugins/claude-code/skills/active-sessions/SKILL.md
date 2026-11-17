---
name: active-sessions
description: CLI for listing the Claude Code sessions that run on this machine right now, across every Claude Code profile — their profile, name, status, branch, live tmux pane and messaging address — and for inspecting one session, running or ended. Use when you need to find another running session, check which sessions are busy, find the tmux pane or profile a session runs in, get a session's messaging address before sending it a message, peek at what another session shows, check whether its prompt is empty or it shows a startup dialog or permission prompt before typing into its pane, or find out what became of a session that ended and how to resume it. Triggered by requests involving running sessions, other agents, "which sessions are busy", "where is session X", "what is the pane of", "is X still running", "which sessions worked in the last hour", "what did session X cost", "is it safe to type into", or tmux panes of Claude sessions.
trigger-keywords: running sessions, active sessions, live sessions, other sessions, tmux pane, session status, busy sessions, messaging address, ended session, resume session, recently active sessions, session cost, pane screen, empty prompt, startup dialog, permission prompt, list-active, inspect
allowed-tools: Bash(cc-session-tools --help), Bash(cc-session-tools list-active:*), Bash(cc-session-tools inspect:*)
---

# cc-session-tools list-active and inspect

`list-active` lists the running sessions; `inspect` shows one session, running or ended.

## list-active

Claude Code writes one registry record per running session into `<config dir>/sessions/<pid>.json`. `list-active` reads the records of every profile (`~/.claude` and `~/.claude-profiles/*`), drops records whose process has ended, and resolves two things live: the git branch of the session's cwd, and where its tmux pane is now. It never writes anything and never contacts a session.

```
Usage: cc-session-tools list-active [OPTIONS]

  List the running Claude Code sessions of every profile.

  One row per session, by profile and then start time. Role is the auth-switch
  role of the session's newest start, a column only when a session has one.
  Active is the time since the session's last model response, in its
  transcript or a subagent's; a session without one shows its start, dimmed.
  The tmux pane is the pane id with the tmux session and window that pane is
  in now, read from the tmux server. The inspect command shows one session.

Options:
  -p, --profile <str>             Only sessions of the profiles whose name
                                  matches this glob, for example 'rohlik*'.
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
                                  'worker-*'. The role is the one of the
                                  session's newest start. A session without a
                                  role never matches. Repeatable.
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
- **`--profile` and `--name` take globs.** `--profile 'rohlik*'` covers every profile whose name starts with `rohlik`; a plain name matches only that profile. A `--profile` glob that matches no profile exits with `1` and lists the known profiles.
- **Filters combine with AND.**
- **`--role <glob>`** keeps the sessions whose role matches, case-sensitively; repeatable. A session without a role never matches. The table shows a Role column only when a listed session has a role.
- **`--tmux-session <name>`** keeps the sessions whose pane is in that tmux session now, by the tmux server, so a window moved there with `move-window` counts. `--tmux-session current` is the tmux session this command runs in, from `$TMUX_PANE`: the sessions next to you. A name that no tmux session has, or `current` outside tmux, exits with `1` and lists the tmux sessions.
- **`list-active` only lists.** For one session run `inspect`; `list-active <SESSION>` exits with `2` and prints the `inspect` command.
- **"Active in the last hour" is `--active-within 1h`**, never a filter on `updated_at`. The record's `updated_at` changes with the status, but a local command such as `/login` moves it without the model, so a session idle for days can have a fresh `updated_at`. It can also lag behind a session that answered later. `--active-within` reads the transcript: the newest model response in it or in any subagent's, else the session's start when it has no response yet.
- **`--here`** uses the scope of `search`: the main checkout, every worktree, and the legacy `<repo>-worktrees/` sibling dir. It matches the session's current `cwd`, which changes when the session moves into a worktree.
- The command takes about 0.2 seconds for 35 sessions. The Active column reads the end of each transcript. `--json` also reads the cost, which costs a read of the whole transcript of a session that never exited: about 0.4 seconds for 35 sessions.

## The JSON fields

`list-active --json` prints an array of objects; `inspect SESSION --json` prints one object with the same keys and a few more. Timestamps are ISO 8601 in UTC, paths are strings, and a missing value is `null`, never an absent key.

- `profile`, `profile_config_dir`: the profile name and its config dir.
- `profile_source`: how sure the profile is. `log`, `environ`, `claude_json` and `dir` are reliable. `inferred` is a guess between profiles that share one `sessions/` dir.
- `address`: `<profile>/<name>`, or `<profile>/pid:<pid>` for a session without a name.
- `pid`, `session_id`, `name`, `name_source` (`user`, `peer`, `derived`, `collision`, `auto`, `hook`).
- `role`: the claude-code-auth-switch role of the session's newest start, from the start log next to its transcript. `null` for a session started without a role, and for one that has not written its transcript yet.
- `status`: `busy`, `idle`, `shell` or `waiting`. `waiting_for`: what a `waiting` session waits for, from the record's `waitingFor`, and `null` in every other status. The table shows it as `waiting: <reason>`. `kind` (`interactive`, `bg`, `daemon`, `daemon-worker`), `entrypoint`, `version`.
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
- `alive`: always `true`. The command leaves out records whose process has ended.
- `raw`: the whole registry record, for fields the list above does not model.

## jq recipes

```bash
# Names and status of the busy sessions
cc-session-tools list-active --status busy --json | jq -r '.[] | "\(.address)\t\(.status)"'

# Pane id of a session by name
cc-session-tools inspect w1-order-planner --json | jq -r '.tmux_pane'

# Sessions of one profile working in this repo, with their branch and pane
cc-session-tools list-active --here --profile rohlik-team --json | jq -r '.[] | "\(.name)\t\(.git_branch)\t\(.tmux_pane)"'

# Messaging address of a session
cc-session-tools inspect cc-auth-switch --json | jq -r '.messaging_address'

# Sessions that worked in the last hour, newest response first
cc-session-tools list-active --active-within 1h --json | jq -r 'sort_by(.last_activity_at) | reverse | .[] | "\(.last_activity_at)\t\(.address)"'

# What an ended session cost
cc-session-tools inspect old-worker --json | jq '.cost | {total_cost_usd, duration_seconds}'

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
                      permission_prompt, busy or unknown, with its last lines.
                      Typing into the pane is safe only at prompt_empty.
  --json              Print every field as one JSON object.
  --help              Show this message and exit.
```

- **Running first.** SESSION is looked up among the running sessions: session id, name, `<profile>/<name>`, `<profile>/pid:<pid>`, pid, or a unique session id prefix. The `session` strings of `cc-session-tools watch` events are of these forms.
- **Then ended sessions**, in this order: by session id or id prefix in every transcript; by the session's name or any earlier name or title, compared without case, among transcripts active in the last `--days` days (14 by default); by its pid from the auth-switch start log in the same window. `<profile>/` in front keeps sessions that profile ran last. A renamed session is found under an old name, and shown as running if it still runs.
- **Several matches** in a step exit with `1` and list the candidates. Narrow with `<profile>/`, a longer id prefix, or the full session id.
- **`--json` keys:** `state` (`running` or `ended`), every key above, `title`, `title_source`, `profile_starts` (`ts`, `pid`, `source`, `profile`, `role`, `role_prompt`, `role_prompt_mode` per start or resume), and `resume_command`. For an ended session the live-only fields (`pid`, `status`, tmux, messaging, `record_path`, `raw` …) are `null`, `cwd` is the last cwd its transcript recorded, `launch_cwd` the first, and `git_branch` the last.
- **Resume an ended session** with its `resume_command` — tell the user the command rather than run it; it starts an interactive session.
- A name lookup among ended sessions reads every transcript of the window; expect about a second.

### --screen

`--screen` captures the session's tmux pane and adds a `screen` object as the last key of `--json`, and a `screen` row plus the last lines in text:

- `state`: `prompt_empty`, `prompt_has_input`, `dialog`, `permission_prompt`, `busy` or `unknown`.
- `title`: the title line of a `dialog` or `permission_prompt`, such as `Accessing workspace:` (workspace trust), `New MCP server found in this project: <name>`, `Allow external CLAUDE.md file imports?` or `Bash command`. `null` for the other states.
- `reason`: why nothing was captured, else `null`: `ended`, `not_in_tmux`, `tmux_unavailable`, `pane_gone`, `pane_not_owned` (the pane id now belongs to another process, as after a tmux server restart), `pane_in_mode` (the pane is in copy mode or another tmux mode, which would take the keys instead of Claude Code) or `capture_failed`. With a reason, `state` is `unknown` and `lines` is `null`.
- `pane`, `captured_at`, and `lines`: the last 20 lines of the pane as plain text, with runs of empty lines collapsed to one.

The classification reads Claude Code's screen layout and answers `unknown` whenever it is not certain, so it breaks safely when the UI changes:

- **Type only at `prompt_empty`.** `prompt_has_input` means someone, often the user, has half-typed text there; typing would append to it. A dimmed `Try "…"` placeholder or a dimmed suggestion counts as empty.
- **`busy`** is the spinner above the prompt box or the greyed `❯` that Claude Code draws while it works, whatever the box holds. Text typed now is queued; wait for the turn to end.
- **`unknown`** covers everything else: shell mode (`!`), the transcript view, an unfamiliar overlay. Do not type; capture the pane again later or look at `lines`.
- The state is a snapshot. Check it right before you type, and check `title` before you answer a dialog.

## Acting on a pane

Every tmux command accepts the pane id as a target, wherever the window has moved:

- `tmux capture-pane -p -t %14 | tail -40` shows what the session shows now, without touching it. Use it to peek.
- `tmux switch-client -t %14` moves the user's tmux client to the session. Run it only when the user asks to go there.
- `tmux send-keys -t %14 …` types into the session. Never do this without the user's explicit request; to talk to another session, send it a message at its `messaging_address` instead. When you do type, run `cc-session-tools inspect <session> --screen --json` first and type only when `.screen.state` is `prompt_empty`.

When `tmux_state` is `gone`, the tmux server has no pane with that id, so the session no longer runs in the pane it started in. When it is `unknown`, this process cannot reach the tmux server the session runs in.
