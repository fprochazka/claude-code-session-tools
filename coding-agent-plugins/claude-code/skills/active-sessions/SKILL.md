---
name: active-sessions
description: CLI for listing the Claude Code sessions that run on this machine right now, across every Claude Code profile — their profile, name, status, branch, live tmux pane and messaging address. Use when you need to find another running session, check which sessions are busy, find the tmux pane or profile a session runs in, get a session's messaging address before sending it a message, or peek at what another session shows. Triggered by requests involving running sessions, other agents, "which sessions are busy", "where is session X", "what is the pane of", "is X still running", or tmux panes of Claude sessions.
trigger-keywords: running sessions, active sessions, live sessions, other sessions, tmux pane, session status, busy sessions, messaging address, list-active
allowed-tools: Bash(cc-session-tools --help), Bash(cc-session-tools list-active:*)
---

# cc-session-tools list-active

Claude Code writes one registry record per running session into `<config dir>/sessions/<pid>.json`. `list-active` reads the records of every profile (`~/.claude` and `~/.claude-profiles/*`), drops records whose process has ended, and resolves two things live: the git branch of the session's cwd, and where its tmux pane is now. It never writes anything and never contacts a session.

```
Usage: cc-session-tools list-active [OPTIONS] [SESSION]

  List the running Claude Code sessions of every profile.

  One row per session, by profile and then start time. Updated is the time
  since the session's registry record last changed, which happens on a status
  change. The tmux pane is the pane id with the tmux session and window that
  pane is in now, read from the tmux server.

Arguments:
  SESSION  Show every field of one session: a session id, a unique session id
           prefix, a name, or a pid.

Options:
  -p, --profile <str>             Only sessions of this profile. Repeatable.
  --status <busy|shell|idle|waiting>
                                  Only sessions with this status. Repeatable.
  --here                          Only sessions whose cwd is in the current
                                  git repo or any of its worktrees; outside a
                                  repo, the exact current directory.
  --tmux / --no-tmux              Only sessions that run in tmux, or only
                                  those that do not.
  --name <str>                    Only sessions whose name matches this glob,
                                  case-insensitively.
  --json                          Print every field as JSON: an array of
                                  sessions, or one object with SESSION.
  --help                          Show this message and exit.
```

## How to use it

- **Prefer `--json` and `jq`.** The table is for people and truncates long branches and tmux session names. The JSON has every field and is the stable interface.
- **Filters combine with AND**, and they also apply before a SESSION lookup: `list-active twin --profile lab` picks the `twin` of the `lab` profile when two sessions share the name.
- **SESSION lookup order:** exact session id, exact name, pid, then a unique session id prefix. An all-digit SESSION is a pid when a live session has that pid. No match or several matches exit with code `1` and list the candidates on stderr.
- **`--here`** uses the scope of `search`: the main checkout, every worktree, and the legacy `<repo>-worktrees/` sibling dir. It matches the session's current `cwd`, which changes when the session moves into a worktree.
- The command takes about 0.2 seconds for 30 sessions. `--json` and SESSION also look up each transcript path, which costs a scan of the profile's project dirs for a session that has no transcript yet.

## The JSON fields

`list-active --json` prints an array; `list-active SESSION --json` prints one object with the same keys. Timestamps are ISO 8601 in UTC, paths are strings, and a missing value is `null`, never an absent key.

- `profile`, `profile_config_dir`: the profile name and its config dir.
- `profile_source`: how sure the profile is. `log`, `environ`, `claude_json` and `dir` are reliable. `inferred` is a guess between profiles that share one `sessions/` dir.
- `address`: `<profile>/<name>`, or `<profile>/pid:<pid>` for a session without a name.
- `pid`, `session_id`, `name`, `name_source` (`user`, `peer`, `derived`, `collision`, `auto`, `hook`).
- `status`: `busy`, `idle`, `shell` or `waiting`. `kind` (`interactive`, `bg`, `daemon`, `daemon-worker`), `entrypoint`, `version`.
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
cc-session-tools list-active w1-order-planner --json | jq -r '.tmux_pane'

# Sessions of one profile working in this repo, with their branch and pane
cc-session-tools list-active --here --profile rohlik-team --json | jq -r '.[] | "\(.name)\t\(.git_branch)\t\(.tmux_pane)"'

# Messaging address of a session
cc-session-tools list-active cc-auth-switch --json | jq -r '.messaging_address'

# Where each pane really is now
cc-session-tools list-active --tmux --json | jq -r '.[] | "\(.tmux_pane)\t\(.tmux_live.session // "?"):\(.tmux_live.window_index // "?")\t\(.name)"'
```

## Acting on a pane

Every tmux command accepts the pane id as a target, wherever the window has moved:

- `tmux capture-pane -p -t %14 | tail -40` shows what the session shows now, without touching it. Use it to peek.
- `tmux switch-client -t %14` moves the user's tmux client to the session. Run it only when the user asks to go there.
- `tmux send-keys -t %14 …` types into the session. Never do this without the user's explicit request; to talk to another session, send it a message at its `messaging_address` instead.

When `tmux_state` is `gone`, the tmux server has no pane with that id, so the session no longer runs in the pane it started in. When it is `unknown`, this process cannot reach the tmux server the session runs in.
