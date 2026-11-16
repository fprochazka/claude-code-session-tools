---
name: session-watch
description: CLI for watching other running Claude Code sessions and reacting as they change — a turn finished, a session waits for its user, got stuck busy, started, ended, was renamed or moved. Use when this session coordinates worker sessions and has to wait for them to finish a turn, notice one that waits for permission or runs too long, or learn when a session starts or ends, or when a `<session-watcher>` message arrives. Covers starting `cc-session-tools watch` in the background, its event kinds and groups, ends, and not watching yourself.
trigger-keywords: watch sessions, wait for session, wait for worker, turn finished, session waiting, session stuck, session-watcher, cc-session-tools watch
allowed-tools: Bash(cc-session-tools --help), Bash(cc-session-tools watch:*), Bash(cc-session-tools inspect:*), Bash(cc-session-tools list-active:*)
---

# cc-session-tools watch

`watch` reports changes to the running Claude Code sessions of every profile as JSON events: a finished turn, a session that waits for its user, one busy for too long, one that started, ended or was renamed. Under Claude Code the events arrive in this conversation as messages, so a coordinator learns that a worker is done without checking. It reads the session registry records, never writes anything, and never contacts the watched sessions.

## Watch or read once

- "What are the sessions doing now?": `cc-session-tools list-active --json`, see the `active-sessions` skill.
- "Tell me when a worker finishes or gets stuck": start a watch. **Never poll** with `sleep`, a loop, or repeated `list-active` calls.

## Starting a watch

Start it with the Bash tool and `run_in_background: true`, and leave `--events-stream` at its default:

```bash
cc-session-tools watch --name 'w*-order-planner' --events=turns,membership
cc-session-tools watch w1-order-planner --until=turn_finished
cc-session-tools watch --profile 'rohlik*' --events=turns --busy-after=30m --waiting-after=5m --idle-after=2h
```

- **The default stream, `auto`,** sends each batch of events to this session as a message. With `--events-stream=stdout` nothing tells you that an event arrived; that mode is for scripts and terminals.
- **Pick the sessions with the filters of `list-active`**: `--profile` and `--name` take globs, plus `--status`, `--here` and `--tmux/--no-tmux`. The filters are evaluated again on every change, so a worker started later, or renamed into the glob, joins the watch.
- **Or name the sessions**: `watch w1-order-planner w2-order-planner` follows exactly those, by id, prefix, name or pid, and ends with reason `sessions_ended` when they all have ended.
- **One watch per purpose.** A second watch with the same selection sends every event twice.
- **Start it from the main session.** A watch started from a subagent messages the main session anyway; Claude Code has no route to a subagent. Forward the events a subagent needs with SendMessage.

### Never watch yourself

The session this command runs under is left out of the selection, because every event it delivers makes this session busy, which would be the next event, in a loop. The header says so in its `own session:` line. `--include-self` turns that off; do not use it under `claude-message`. Naming your own session as SESSION is an error.

## Help

```
Usage: cc-session-tools watch [OPTIONS] [SESSION]...

  Report changes to the running Claude Code sessions as JSON events.

  One JSON event per line on stdout, or as messages to the Claude Code session
  this runs under. The first event is watch_started with the matching
  sessions, the last is watch_ended with a reason. Without SESSION, a session
  that starts to match the filters joins the watch and one that stops matching
  leaves it.

Arguments:
  [SESSION]...  Watch these sessions until they end, instead of every session
                that matches the filters: a session id, a unique session id
                prefix, a name, or a pid. The filters apply before the lookup.

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
  --include-self                  Also watch the session this command runs
                                  under. Left out by default, because every
                                  event it receives changes its own status.
  --events <str>                  Report only these event kinds or groups
                                  (membership, status, turns, thresholds,
                                  changes). Comma-separated, repeatable.
  --exclude-events <str>          Do not report these event kinds or groups.
                                  Comma-separated, repeatable.
  --until <str>                   Stop after the first reported event of this
                                  kind, for example turn_finished.
  --timeout <str>                 Stop this long after the start, for example
                                  90s, 30m or 1h30m.
  --idle-after <str>              Stop when nothing was reported for this
                                  long.
  --busy-after <str>              Report busy_long once per busy stretch of a
                                  session that stays busy this long.
  --waiting-after <str>           Report waiting_long once per waiting stretch
                                  of a session that waits this long.
  --events-stream <auto|stdout|claude-message>
                                  Where events go: auto picks claude-message
                                  under a Claude Code session that accepts
                                  messages, else stdout.  [default: auto]
  --message-priority <now|next|later>
                                  The priority of claude-message messages.
                                  [default: next]
  --help                          Show this message and exit.
```

## The startup header

Read it first in the background task's output. Each line starts with `cc-session-tools watch: `:

- `selection:` the filters, the profiles a `--profile` glob matched, and how many sessions match now.
- `own session:` which session is left out.
- `stream:` confirms `claude-message`. A `stream fallback:` line means the events do not arrive as messages: tell the user why, and read the changelog or the task output until it is fixed. Do not restart with a forced stream.
- `changelog:` the `events.jsonl` that keeps every reported event.
- `details:` `cc-session-tools inspect <session> --json`, the command for every field of a session an event names.
- `events:`, `ignored:`, `thresholds:`, `ends:` show the filter and the options.

## Events and groups

Events are small on purpose: `{"kind", "seq", "observed_at", "data"}`, where a session event's `data` holds `session` and only what changed. `session` is a string such as `rohlik-team/w1-order-planner`, or `rohlik-team/pid:3811117` when the session has no name or shares it with another running session of its profile. **For anything else — cwd, branch, tmux pane, messaging address, pid — run `cc-session-tools inspect <session> --json`** (see the `active-sessions` skill); it resolves the string also after the session ended. The `session` string changes with a rename; `renamed` carries the old one in `from`.

- `lifecycle`, always on: `watch_started` (`selection`, `sessions`: `{session, status}` per matching session, plus `waiting_for` when it waits), `watch_ended` (`reason`).
- `membership`: `session_started` and `session_matched` (`session`, `status`), `session_ended` (`session`, `reason`: `record_removed` or `process_gone`), `session_unmatched` (`session`).
- `status`: `status_changed` (`session`, `from`, `to`, and `waiting_for` when `to` is `waiting`).
- `turns`, derived from the same transition: `turn_finished` (`session`, `from`: `busy` or `waiting`), `waiting` (`session`, `waiting_for`).
- `thresholds`: `busy_long` with `--busy-after`, `waiting_long` with `--waiting-after`, each once per stretch (`session`, `after`).
- `changes`: `renamed` (`session` with the new name, `from`: the old `session`), `cwd_changed` (`session`, `to`), `tmux_moved` (`session`, `to`: `<tmux session>:<window index>` or `null`).

One transition can yield two events: `busy` to `idle` is `status_changed` plus `turn_finished`. Subscribe with `--events=turns` when the derived events are all you need. A typo, or a kind this watch never produces, is a start-up error.

## Ends

- `--until=turn_finished`: stop after the first event of that kind, from any watched session.
- `--timeout=30m`: stop that long after the start. `--idle-after=2h`: stop when nothing was reported for that long; `busy_long` and `waiting_long` do not count.
- Named sessions: the watch ends when all of them have ended.
- `watch_ended` reasons: `until`, `timeout`, `idle`, `sessions_ended`, `session_gone` (this session can no longer receive messages), or a signal. A background task that ends without `watch_ended` ended for an unknown reason; if you still need the watch, start the same command again and tell the user in one line.

Stop a watch by stopping its background task.

## The messages

```text
<session-watcher filter="name=w*-order-planner" seq="5-6">
{"kind":"status_changed","seq":5,"data":{"session":"rohlik-team/w1-order-planner","from":"busy","to":"idle"}}
{"kind":"turn_finished","seq":6,"data":{"session":"rohlik-team/w1-order-planner","from":"busy"}}
</session-watcher>
```

- Events within 3 seconds come as one message. The first message also carries `changelog="…"` and `details="cc-session-tools inspect <session> --json"`.
- A batch above 64 KB arrives as a count per kind, and a gap in `seq` means a message was lost. Read the changelog from the last `seq` you saw.

## Reacting

- **`turn_finished`**: the worker is idle. Get its `tmux_pane` and `messaging_address` with `cc-session-tools inspect <session> --json`, read what it did (`tmux capture-pane -p -t <tmux_pane> | tail -40`, or its transcript), then give it the next task by messaging its `messaging_address`.
- **`waiting`**, **`waiting_long`**: the session waits for its user, usually a permission prompt (`waiting_for`). Tell the user which session and what it waits for. Never answer the prompt by sending keys to its pane.
- **`busy_long`**: a session has been busy for a long time. Peek at its pane before you decide it hangs; tell the user rather than interrupt it.
- **`session_ended`**: the worker is gone. `process_gone` without a clean exit can mean a crash. `cc-session-tools inspect <session> --json` shows its last state and its `resume_command`.
- Never send keys to a tmux pane unless the user asks for exactly that.

## Limits

- The watcher polls once a second, so a status that lasts less than that can be missed.
- Delivery is best effort: the session may drop a message under its rate limit, or hold it until its user approves it. The changelog has every event.
