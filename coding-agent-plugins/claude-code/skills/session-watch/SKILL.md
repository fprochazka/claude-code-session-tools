---
name: session-watch
description: CLI for watching other running Claude Code sessions and reacting as they change — a turn finished, a session waits for its user, got stuck busy, filled its context window, started, ended, was renamed or moved. Use when this session coordinates worker sessions and has to wait for them to finish a turn, notice one that waits for permission, runs too long or nears the end of its context, or learn when a session starts or ends, or when a `<session-watcher>` message arrives. Covers starting `cc-session-tools watch` in the background, its event kinds and groups, ends, and not watching yourself.
trigger-keywords: watch sessions, wait for session, wait for worker, turn finished, session waiting, session stuck, context full, context threshold, session-watcher, cc-session-tools watch
allowed-tools: Bash(cc-session-tools --help), Bash(cc-session-tools watch:*), Bash(cc-session-tools inspect:*), Bash(cc-session-tools list-active:*)
---

# cc-session-tools watch

`watch` reports changes to the running Claude Code sessions of every profile as JSON events: a finished turn, a session that waits for its user, one busy for too long, one whose context fills up, one that started, ended or was renamed. Under Claude Code the events arrive in this conversation as messages, so a coordinator learns that a worker is done without checking. It reads the session registry records, never writes anything, and never contacts the watched sessions.

## Watch or read once

- "What are the sessions doing now?": `cc-session-tools list-active --json`, see the `active-sessions` skill.
- "Tell me when a worker finishes or gets stuck": start a watch. **Never poll** with `sleep`, a loop, or repeated `list-active` calls.

## Starting a watch

Start it with the Bash tool and `run_in_background: true`, and leave `--events-stream` at its default:

```bash
cc-session-tools watch --name 'w*-order-planner' --events=turns,membership
cc-session-tools watch w1-order-planner --until=turn_finished
cc-session-tools watch --profile 'work*' --events=turns --busy-after=30m --waiting-after=5m --stop-after-quiet=2h
cc-session-tools watch --tmux-session current --events=thresholds --idle-after=10m --idle-every=50m
cc-session-tools watch --name 'w*-order-planner' --events=turns,context_high --context-above 75% --context-above 90%
```

- **The default stream, `auto`,** sends each batch of events to this session as a message. With `--events-stream=stdout` nothing tells you that an event arrived; that mode is for scripts and terminals.
- **Pick the sessions with the filters of `list-active`**: `--profile` and `--name` take globs, plus `--status`, `--here`, `--tmux/--no-tmux`, `--tmux-session <name>|current` and `--role <glob>`. The filters are evaluated again on every change, so a worker started later, renamed into the glob, or whose window was moved into the tmux session, joins the watch. A session counts as in a tmux session only when its process runs in the pane it recorded. `--tmux-session current` is the tmux session this command runs in, resolved once at the start. Renaming that tmux session keeps its sessions in the watch. A session whose role is not known yet when it starts joins with `session_matched` once its role is read, usually within seconds, at the latest at its first turn.
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
                                  a window moved with move-window counts. A
                                  pane counts only when the session's process
                                  runs in it. 'current' is the tmux session
                                  this command runs in. It is resolved once,
                                  at the start.
  --role <str>                    Only sessions whose auth-switch role matches
                                  this glob, case-sensitively, for example
                                  'worker-*'. The role is the one its running
                                  process was started with. A session without
                                  a role never matches. Repeatable.
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
  --stop-after-quiet <str>        Stop when nothing was reported for this
                                  long.
  --busy-after <str>              Report busy_long once per busy stretch of a
                                  session that stays busy this long.
  --waiting-after <str>           Report waiting_long once per waiting stretch
                                  of a session that waits this long.
  --idle-after <str>              Report idle_long once per idle stretch of a
                                  session that is idle this long after its
                                  last model response. A stretch ends at the
                                  next model response. Repeatable.
  --idle-every <str>              Report idle_long at every multiple of this
                                  duration within an idle stretch.
  --context-above <N%|tokens>     Report context_high once when a session's
                                  context fill reaches this mark, and again
                                  after it dropped below it, as after a
                                  compaction. 75% is a percentage of the
                                  context window (a bare number below 100 is
                                  one too); 150000, 150k or 1M is a number of
                                  tokens. Repeatable.
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

Events are small on purpose: `{"kind", "seq", "observed_at", "data"}`, where a session event's `data` holds `session` and only what changed. `session` is a string such as `work-team/w1-order-planner`, or `work-team/pid:3811117` when the session has no name or shares it with another running session of its profile. **For anything else — cwd, branch, tmux pane, messaging address, pid — run `cc-session-tools inspect <session> --json`** (see the `active-sessions` skill); it resolves the string also after the session ended. The `session` string changes with a rename; `renamed` carries the old one in `from`.

- `lifecycle`, always on: `watch_started` (`selection`, `sessions`: `{session, status}` per matching session, plus `waiting_for` when it waits), `watch_ended` (`reason`).
- `membership`: `session_started` and `session_matched` (`session`, `status`), `session_ended` (`session`, `reason`: `record_removed` or `process_gone`), `session_unmatched` (`session`).
- `status`: `status_changed` (`session`, `from`, `to`, and `waiting_for` when `to` is `waiting`).
- `turns`, derived from the same transition: `turn_finished` (`session`, `from`: `busy` or `waiting`), `waiting` (`session`, `waiting_for`, and `plan_file` when `waiting_for` is `plan approval`). `waiting_for` is `plan approval` when the session's pane shows the plan approval dialog of plan mode, which the record calls `permission prompt` like any permission prompt; `plan_file` is the plan's path, or `null` when the transcript names none.
- `thresholds`: `busy_long` with `--busy-after`, `waiting_long` with `--waiting-after`, each once per stretch (`session`, `after`). `idle_long` (`session`, `threshold`) with `--idle-after`, repeatable, once per threshold, and with `--idle-every` at every multiple of its duration. They count from the session's last model response, and a new response ends the idle stretch, so a message that makes the session answer starts the count again. A resumed session counts from no earlier than its process start, and a `/clear` starts the count again; one made before the watch counts only when the session was started through auth-switch. Past several thresholds at once, only the longest is reported. `context_high` (`session`, `used`, `size`, `percent`, `mark`, `mark_tokens`, `model`) with `--context-above`, repeatable: once when the session's context fill reaches the mark, and again only after the fill dropped below it, as after a compaction or a `/clear`. Write a mark as a percentage of the session's window, `75%`, which needs no quotes in bash or zsh, or as tokens, `150000`, `150k` or `1M`; a bare number below 100 is a percentage. Past several marks at once, only the highest is reported. The fill is read from the transcript at most every 3 seconds.
- `changes`: `renamed` (`session` with the new name, `from`: the old `session`), `cwd_changed` (`session`, `to`), `tmux_moved` (`session`, `to`: `<tmux session>:<window index>` or `null`).

One transition can yield two events: `busy` to `idle` is `status_changed` plus `turn_finished`. Subscribe with `--events=turns` when the derived events are all you need. A typo, or a kind this watch never produces, is a start-up error.

## Ends

- `--until=turn_finished`: stop after the first event of that kind, from any watched session.
- `--timeout=30m`: stop that long after the start. `--stop-after-quiet=2h`: stop when nothing was reported for that long; `busy_long`, `waiting_long`, `idle_long` and `context_high` do not count.
- Named sessions: the watch ends when all of them have ended.
- `watch_ended` reasons: `until`, `timeout`, `quiet`, `sessions_ended`, `session_gone` (this session can no longer receive messages), or a signal. A background task that ends without `watch_ended` ended for an unknown reason; if you still need the watch, start the same command again and tell the user in one line.

Stop a watch by stopping its background task.

## The messages

```text
<session-watcher filter="name=w*-order-planner" seq="5-6">
{"kind":"status_changed","seq":5,"data":{"session":"work-team/w1-order-planner","from":"busy","to":"idle"}}
{"kind":"turn_finished","seq":6,"data":{"session":"work-team/w1-order-planner","from":"busy"}}
</session-watcher>
```

- Events within 3 seconds come as one message. The first message also carries `changelog="…"` and `details="cc-session-tools inspect <session> --json"`.
- A batch above 64 KB arrives as a count per kind, and a gap in `seq` means a message was lost. Read the changelog from the last `seq` you saw.

## Reacting

- **`turn_finished`**: the worker is idle. Get its `tmux_pane` and `messaging_address` with `cc-session-tools inspect <session> --json`, read what it did (`tmux capture-pane -p -t <tmux_pane> | tail -40`, or its transcript), then give it the next task by messaging its `messaging_address`.
- **`waiting`**, **`waiting_long`**: the session waits for its user, usually a permission prompt or a plan approval (`waiting_for`). Tell the user which session and what it waits for. Never answer the prompt by sending keys to its pane.
- **`idle_long`**: a session has had nothing to do for `threshold` since its last model response. Give it work, or tell the user it is idle; whether to keep its cache warm or let it go is their decision.
- **`context_high`**: a session's context is `percent` full, past `mark`. Do what your instructions say for that mark, such as compacting the session at its next idle moment; without such instructions, tell the user which session and how full it is. Before you type `/compact` into its pane, check with `cc-session-tools inspect <session> --screen --json` that its prompt is empty (see the `active-sessions` skill).
- **`busy_long`**: a session has been busy for a long time. Peek at its pane before you decide it hangs; tell the user rather than interrupt it.
- **`session_ended`**: the worker is gone. `process_gone` without a clean exit can mean a crash. `cc-session-tools inspect <session> --json` shows its last state and its `resume_command`.
- Never send keys to a tmux pane unless the user asks for exactly that.

## Limits

- The watcher polls once a second, so a status that lasts less than that can be missed.
- Delivery is best effort: the session may drop a message under its rate limit, or hold it until its user approves it. The changelog has every event.
