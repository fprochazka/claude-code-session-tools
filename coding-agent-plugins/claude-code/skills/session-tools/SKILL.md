---
name: session-tools
description: CLI for finding, distilling and timing past Claude Code sessions — list and grep the sessions of the current repo and its git worktrees, condense one transcript into markdown, and show where a session's time went (turns, subagents, tool calls, idle gaps, waits on the user). Use when the user refers to an earlier conversation, asks what was decided or tried before, wants a past session summarized, needs the session id of something that already happened, or asks why a session took so long or what it spent its time on. Triggered by requests involving past sessions, an earlier conversation, a session transcript, "what did we decide before", "find the session where", "where did the time go", "why did this session take so long", or the slowest or longest sessions.
trigger-keywords: claude code session, past session, previous session, session transcript, session history, ccdistill, session search, session timeline, tool stats, where the time went, long session, session duration
allowed-tools: Bash(cc-session-tools --help), Bash(cc-session-tools search:*), Bash(cc-session-tools distill:*), Bash(cc-session-tools timeline:*), Bash(cc-session-tools tool-stats:*)
---

# cc-session-tools

Claude Code writes every session as a JSONL transcript under `<config dir>/projects/<encoded-cwd>/<session-id>.jsonl`, where `<encoded-cwd>` is the absolute launch directory with every non-alphanumeric character replaced by `-`. This tool reads the projects dir of every Claude Code profile on the machine, each shared dir once — or only `CLAUDE_PROJECTS_DIR` when it is set — and never writes to it. `search` maps the current directory back onto its project dirs and lists the sessions; `distill` turns one transcript into a markdown recap; `timeline` and `tool-stats` show where its time went. All of them read the transcript content itself rather than file metadata, so ordering and names reflect the real conversation.

## search

```
Usage: cc-session-tools search [OPTIONS]

  List Claude Code sessions for the current directory, newest interaction
  first.

Options:
  -n, --limit <int>   Maximum number of sessions to show (newest first). A
                      value <= 0 means no limit.  [default: 20]
  --rg <str>          Only show sessions whose transcript contains this
                      literal text (case-insensitive; uses ripgrep when
                      installed). Repeatable: every keyword must match
                      somewhere in the session.
  --rg-any            Match sessions containing ANY of the --rg keywords
                      instead of all of them. Only meaningful alongside --rg.
  --all               List sessions from every project instead of the current
                      directory and its git worktrees.
  --since <str>       Only sessions whose last interaction is this recent: a
                      duration such as 2h, 3d or 2w, or an ISO date such as
                      2026-11-01.
  --json              Print the rows as a JSON array instead of a table.
  --durations         Add each session's wall time, active time, subagents,
                      compactions and kind. Reads every shown session whole,
                      which takes seconds per large session.
  --min-active <str>  Only sessions active at least this long, such as 2h or
                      1h30m; implies --durations. Active time counts a tool
                      call that stayed pending for hours, such as an
                      unanswered prompt.
  --sort <str>        Order by the last interaction (last) or by active time,
                      longest first (active); active implies --durations.
                      [default: last]
  --help              Show this message and exit.
```

Operational notes:

- **Do not filter the output.** The table is short; piping it through `head`, `grep` or `jq` loses rows and the column alignment. At most redirect it to a scratchpad file.
- **Scope.** Inside a git repo the listing covers the main checkout and every worktree of it, including worktrees that have since been deleted — their transcripts still exist. Outside a git repo only the exact current directory is covered. `--all` ignores the repo entirely and lists every project, with the last column showing each session's own directory. When the rows come from more than one projects dir, a `Profile` column names the profile each session belongs to.
- **A session belongs to the directory it was launched from**, taken from the first `cwd` record in the transcript. A session that started one directory up and later moved into the repo is not listed here; `--all` still finds it.
- **Ordering** is by the last interaction timestamp read from inside the transcript, not by file mtime — Claude Code appends titles and summaries retroactively, which bumps mtime long after the work happened.
- **`--rg` is literal, case-insensitive text**, not a regex. It uses `rg` (ripgrep) when it is on PATH and a slower built-in scan otherwise. Matches inside subagent transcripts and tool-result sidecars count and fold up into the parent session.
- **Repeating `--rg` is AND at the level of the session**: every keyword must appear somewhere in it, but they may sit in different messages or in a subagent it spawned. `--rg-any` flips that to OR. Each AND keyword costs its own ripgrep pass; the OR costs one pass no matter how many keywords.
- **`--since 3d`** (or `2h`, `2w`, an ISO date such as `2026-11-01`) keeps the sessions whose last interaction is that recent. It is applied before `--rg` and the limit, so it also makes `--rg` cheaper.
- **`--json`** prints the rows as an array: `session_id`, `name`, `name_is_derived`, `last_interaction` (ISO), `last_interaction_from_transcript`, `profile`, `location` (the last column of the table), `cwd`, `transcript_path`. Nothing matched prints `[]`, with the notice on stderr. Prefer it to parsing the table.
- **`--durations`** adds **Wall**, **Active** (main turns and subagent bursts, overlaps once), **Subagents**, **Compactions** (the main agent's; the subagents' beside them as `38 (+14)`, never added) and **Kind** (`worker`, `dispatcher`, … from the start log, else `loop` or `interactive`); the same numbers as the `timeline` header. In `--json` the rows also get `wall` and `active` in seconds, `subagents`, `compactions`, `subagent_compactions`, `kind`, `kind_source`.
- **`--min-active 2h`** keeps sessions active at least that long, and **`--sort active`** orders by active time; both imply `--durations` and apply before the limit, so add `-n 0` to get every match. Active time counts a tool call left pending for hours (an unanswered prompt) as active; check the suspected waits in `timeline` before trusting a large number.
- **The duration columns are expensive**: they read every shown session whole, in parallel processes — about 10–30 seconds for 60 days of sessions with `--all --since 60d --min-active 2h`, several seconds for one session with gigabytes of transcripts. Plain `search` stays at its head-and-tail read. Narrow with `--since` first.
- **`--all` reads every project** — about 2 seconds over ~3,000 sessions with a warm disk cache, plus one ripgrep pass per `--rg` keyword. Prefer the repo-scoped default.
- **A dimmed name** is a title derived from the first human message, not a real session name.
- Exit codes: `0` including when nothing matched (the notice goes to stderr), `1` when `--since`, `--min-active` or `--sort` can't be read, `2` when ripgrep fails.

## distill

```
Usage: cc-session-tools distill [OPTIONS] {target}

  Render a condensed markdown transcript of a session.

Arguments:
  target  A .jsonl path, a session id or a prefix of one, or the literal
          'latest'.  [required]

Options:
  --include-thinking   Include assistant thinking blocks.
  --output-dir <path>  Directory to write <session-id>.md, and the pre-compact
                       parts of a compacted session, into.  [default:
                       /tmp/claude-code-session-tools]
  --stdout             Print the main markdown to stdout instead of writing it
                       to a file. A compacted session's pre-compact parts are
                       still written to the output dir.
  --help               Show this message and exit.
```

Operational notes:

- **Do not filter the output.** In file mode stdout is exactly one line — the path of the written markdown — and the stats table goes to stderr. Read the file it names; it is short and costs little context.
- **`latest` uses the same scope as `search`**: the newest session of the current repo and its worktrees. Transcripts with no timestamped records — empty stubs — are skipped. There is no "newest session overall" fallback — if the scope is empty the command fails rather than distilling something unrelated.
- **A session id target**, or a prefix of one of at least 4 characters, is looked up across every project dir, so it works from anywhere; a prefix that matches several sessions fails and lists them. The same goes for `timeline` and `tool-stats`. Subagent sidecars under `<session-id>/subagents/` are not sessions and cannot be reached by id.
- **Headings name who sent a prompt**: `👤 User` for the user, `📨 Peer session` for a message from another Claude Code session, `🔔 Task notification` for a finished background task. Only `👤 User` prompts are the user's own words.
- **A compacted session is split at its compactions.** The main file `<session-id>.md` holds only what the model worked with after the last compaction: the compaction summary under `## 📦 Compaction summary`, the stretch of conversation Claude Code kept verbatim, and everything after. Its first line under the title is a `> Compacted session: …` pointer to the absolute path of the part before, `<session-id>.pre-compact-<k>.md` in the same dir, and each part points back to the one before it, down to `pre-compact-1`, the oldest. Each pre-compact part ends with a `> Continued after compaction <k> in …` line. For a recap of the whole session, follow the pointers and read every part, oldest first; for what the session ended up with, the main file is enough.
- `--stdout` writes the main markdown to stdout instead of a file; the stats table still goes to stderr, so the stdout stays valid markdown. The pre-compact parts of a compacted session are still written to `--output-dir`, so the pointer line names files that exist.
- Exit code `1` when the target does not resolve or the file cannot be read; the message is a plain `Error: …` line on stderr.

## Where the time went

`timeline` and `tool-stats` analyse where a session spent its time. Both take the same target as `distill` and `--json`; their options and the full output contract are in `cc-session-tools timeline --help`, `cc-session-tools tool-stats --help` and the project README (`### timeline`, `### tool-stats`). Run them in this order:

1. **Find the sessions**: `cc-session-tools search --all --since 30d --min-active 2h -n 0` (add `--sort active` for the longest first). The columns cost seconds per large session; narrow with `--since` first.
2. **`timeline <session>`** for the shape: when it worked, idled, waited and compacted, and which subagents ran in which turn. Long sessions are collapsed to `--max-lines`.
3. **`tool-stats <session>`** for where the busy time went: per tool, Bash by purpose (test full or targeted, build, wait, git, …), slowest and repeated commands, permission prompts, and subagent cost by role.
4. **Zoom in** with `timeline <session> --from 18:30 --to 19:00` (every turn and gap of the stretch, nothing merged) or `timeline <session> --subagent <agent-id>` (one subagent's bursts, calls and children).
5. **Many sessions**: `tool-stats --sessions-from ids.txt` (or `-` for stdin, e.g. ids from `search --json` through `jq -r '.[].session_id'`) adds the sections up, with a row per session and totals per kind.
6. **The why** is not in the numbers: `distill` the session and read the turns the timeline pointed at.

Reading the `timeline` header:

- **wall** is first to last record; **active** is main turns ∪ subagent activity; **away** is idle gaps over 2 h ended by a human or cron prompt. **✖ stalls** are the stretches in which the context was full and every prompt was refused (`Prompt is too long`) until a compaction; they count apart from away and idle, so a loop that kept firing into a full context doesn't look like the user was away. A loop session (`kind` from the start log, or guessed from what opened the turns) idles by design.
- **main agent busy** splits into model, tools, foreground subagents, waiting on the user and blocking compactions. The parts overlap and don't add up to busy. **busy by what opened the turn** says which trigger kept the main agent working, and **crons by prompt** which cron did, by its prompt's first line.
- **idle between turns** is split by what ended it: `human`, `peer`, `handback` (its own subagent reporting), `task-notification`, `cron`. Idle time with subagents active is background work, not lost time.
- **subagents** line: counts, fg/bg, **subagent active** as the union of their activity (as the clock ran), **lane time** when it differs (each subagent's own active time added up, a parent counting its wait on nested ones), and tokens. The last line counts the **⌛ suspected waits** and how much busy and active time lies inside them.

Reading `tool-stats`: the header gives the call counts, the calls that waited on the user, results held behind a subagent, the suspected waits, and the Bash calls Claude Code moved to the background (`↷`), which the sections count until their completion notice while the lane and busy totals count only the time until the move; each section's totals are sums of elapsed times.

Limits that change the conclusion:

- **Suspected waits still count.** A call over 1 h (`⌛`) probably waited on something the transcript can't show: an unanswered permission prompt, a machine that slept overnight. Its time stays in busy and active, also in `search --min-active`; subtract what the header reports before calling a session long.
- **Lane time is not elapsed time.** Calls overlap, subagents run in parallel, and a parent's active time includes its wait on nested subagents, so per-tool and per-role sums exceed the clock. Compare against the session's union (`active`, the subagents' elapsed line), never add lanes up.
- **Roles with `?` are guesses** from the agent type or the description's words; only a description prefix (`impl:`, `validate:`, `run:`, `explore:`) is certain.
- **User waits are left out of the tool sections**: a call the user answered a prompt or dialog for waited an unknown part of its wall. Sessions recorded before Claude Code wrote permission decisions count dialogs only, and the header says so.
- **Model time is `n/a`** on sessions from Claude Code versions without `api-request` records; that is unknown, not zero.
- **Redaction is a safety net, not a guarantee.** Paths, hosts, long hex ids and credential-looking values are replaced, but names, ticket keys and secrets in an unusual shape pass through. Read the output before pasting it anywhere shared.
- **Your own CLIs land in `run`**, with every other program the built-in rules don't know, unless they wait through a `--wait`/`--watch` option or subcommand, which makes them `wait`. So `run` is inflated until you map them: add them to `~/.config/cc-session-tools/bash-groups.toml` (`[programs] my-db-cli = "db"`, `[scripts]` globs), as the README's `tool-stats` section shows.

## Slash commands

Two commands wrap the full workflows and should be preferred over driving the CLI by hand:

- `/session-tools:search <keywords or topic>` — mine past sessions for decisions and context, writing the findings to `./.claude/plans/past-sessions-<slug>.md`.
- `/session-tools:distill <session-id | latest>` — condense one session into a recap at `./.claude/plans/session-distill-<slug>.md`.
