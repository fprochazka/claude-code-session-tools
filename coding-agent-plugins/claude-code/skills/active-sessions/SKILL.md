---
name: active-sessions
description: CLI for listing the Claude Code sessions that run on this machine right now, across every Claude Code profile — their profile, name, status, branch, live tmux pane and messaging address — and for inspecting one session, running or ended. Use when you need to find another running session, check which sessions are busy, find the tmux pane or profile a session runs in, get a session's messaging address before sending it a message, peek at what another session shows, or find out what became of a session that ended and how to resume it. Triggered by requests involving running sessions, other agents, "which sessions are busy", "where is session X", "what is the pane of", "is X still running", or tmux panes of Claude sessions.
trigger-keywords: running sessions, active sessions, live sessions, other sessions, tmux pane, session status, busy sessions, messaging address, ended session, resume session, list-active, inspect
allowed-tools: Bash(cc-session-tools --help), Bash(cc-session-tools list-active:*), Bash(cc-session-tools inspect:*)
---

# cc-session-tools list-active and inspect

`list-active` lists the running sessions; `inspect` shows one session, running or ended.

## list-active

Claude Code writes one registry record per running session into `<config dir>/sessions/<pid>.json`. `list-active` reads the records of every profile (`~/.claude` and `~/.claude-profiles/*`), drops records whose process has ended, and resolves two things live: the git branch of the session's cwd, and where its tmux pane is now. It never writes anything and never contacts a session.

```
Usage: cc-session-tools list-active [OPTIONS]

  List the running Claude Code sessions of every profile.

  One row per session, by profile and then start time. Updated is the time
  since the session's registry record last changed, which happens on a status
  change. The tmux pane is the pane id with the tmux session and window that
  pane is in now, read from the tmux server. The inspect command shows one
  session.

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
  --json                          Print every field of every session as a JSON
                                  array.
  --help                          Show this message and exit.
```

## How to use it

- **Prefer `--json` and `jq`.** The table is for people and truncates long branches and tmux session names. The JSON has every field and is the stable interface.
- **`--profile` and `--name` take globs.** `--profile 'rohlik*'` covers every profile whose name starts with `rohlik`; a plain name matches only that profile. A `--profile` glob that matches no profile exits with `1` and lists the known profiles.
- **Filters combine with AND.**
- **`list-active` only lists.** For one session run `inspect`; `list-active <SESSION>` exits with `2` and prints the `inspect` command.
- **`--here`** uses the scope of `search`: the main checkout, every worktree, and the legacy `<repo>-worktrees/` sibling dir. It matches the session's current `cwd`, which changes when the session moves into a worktree.
- The command takes about 0.2 seconds for 30 sessions. `--json` also looks up each transcript path, which costs a scan of the profile's project dirs for a session that has no transcript yet.

## The JSON fields

`list-active --json` prints an array of objects; `inspect SESSION --json` prints one object with the same keys and a few more. Timestamps are ISO 8601 in UTC, paths are strings, and a missing value is `null`, never an absent key.

- `profile`, `profile_config_dir`: the profile name and its config dir.
- `profile_source`: how sure the profile is. `log`, `environ`, `claude_json` and `dir` are reliable. `inferred` is a guess between profiles that share one `sessions/` dir.
- `address`: `<profile>/<name>`, or `<profile>/pid:<pid>` for a session without a name.
- `pid`, `session_id`, `name`, `name_source` (`user`, `peer`, `derived`, `collision`, `auto`, `hook`).
- `status`: `busy`, `idle`, `shell` or `waiting`. `waiting_for`: what a `waiting` session waits for, from the record's `waitingFor`, and `null` in every other status. The table shows it as `waiting: <reason>`. `kind` (`interactive`, `bg`, `daemon`, `daemon-worker`), `entrypoint`, `version`.
- `started_at`; `updated_at`: the last change of the record. Claude Code writes the record on events only, so an old `updated_at` on an idle session is normal.
- `cwd`: the session's current dir. `launch_cwd`: the dir its process started in, or `null` before the first transcript record.
- `git_branch`: the short branch of `cwd`, `null` when detached, unborn or outside git. `git_worktree_root`: the checkout that holds `cwd`. `git_repo_root`: the main checkout, the same for every worktree of a repo.
- `tmux_pane`: the pane id such as `%14`, or `null` outside tmux. It is the address to use with tmux.
- `tmux_state`: `present`, `gone` (the tmux server has no such pane), `unknown` (tmux is missing or has no server), or `null` outside tmux.
- `tmux_live`: `{session, window_index, window_id, pane_title}` from the running tmux server, or `null` when the pane is not `present`.
- `tmux_recorded`: the record's own `<session>:@<window>.%<pane>`. Claude Code writes it once at startup, so its session part goes stale when a window moves to another tmux session. **Trust `tmux_live`, never the session part of `tmux_recorded`.**
- `messaging_socket`, `messaging_address` (`uds:<socket path>`, the address for messaging the session), `can_receive_messages`, `peer_protocol`, `peer_features`.
- `bridge_session_id`, `scratchpad_dir`, `scratchpad_dir_inferred`, `transcript_path` (`null` before the session writes one), `record_path`.
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

# Is a session still running, and how to resume it
cc-session-tools inspect old-worker --json | jq -r '"\(.state)\t\(.resume_command)"'

# Where each pane really is now
cc-session-tools list-active --tmux --json | jq -r '.[] | "\(.tmux_pane)\t\(.tmux_live.session // "?"):\(.tmux_live.window_index // "?")\t\(.name)"'
```

## inspect

```
Usage: cc-session-tools inspect [OPTIONS] {SESSION}

  Show every field of one Claude Code session, running or ended.

  A running session shows what list-active knows about it, with state running.
  An ended session shows what its transcript and the auth-switch start log
  recorded, with state ended: the profile that ran it last, its last name,
  title and cwd, when it started and was last active, every start, and the
  command that resumes it.

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
  --json              Print every field as one JSON object.
  --help              Show this message and exit.
```

- **Running first.** SESSION is looked up among the running sessions: session id, name, `<profile>/<name>`, `<profile>/pid:<pid>`, pid, or a unique session id prefix. The `session` strings of `cc-session-tools watch` events are of these forms.
- **Then ended sessions**, in this order: by session id or id prefix in every transcript; by the session's name or any earlier name or title, compared without case, among transcripts active in the last `--days` days (14 by default); by its pid from the auth-switch start log in the same window. `<profile>/` in front keeps sessions that profile ran last. A renamed session is found under an old name, and shown as running if it still runs.
- **Several matches** in a step exit with `1` and list the candidates. Narrow with `<profile>/`, a longer id prefix, or the full session id.
- **`--json` keys:** `state` (`running` or `ended`), every key above, `title`, `title_source`, `last_activity_at`, `profile_starts` (`ts`, `pid`, `source`, `profile` per start or resume), and `resume_command`. For an ended session the live-only fields (`pid`, `status`, tmux, messaging, `record_path`, `raw` …) are `null`, `cwd` is the last cwd its transcript recorded, `launch_cwd` the first, and `git_branch` the last.
- **Resume an ended session** with its `resume_command` — tell the user the command rather than run it; it starts an interactive session.
- A name lookup among ended sessions reads every transcript of the window; expect about a second.

## Acting on a pane

Every tmux command accepts the pane id as a target, wherever the window has moved:

- `tmux capture-pane -p -t %14 | tail -40` shows what the session shows now, without touching it. Use it to peek.
- `tmux switch-client -t %14` moves the user's tmux client to the session. Run it only when the user asks to go there.
- `tmux send-keys -t %14 …` types into the session. Never do this without the user's explicit request; to talk to another session, send it a message at its `messaging_address` instead.

When `tmux_state` is `gone`, the tmux server has no pane with that id, so the session no longer runs in the pane it started in. When it is `unknown`, this process cannot reach the tmux server the session runs in.
