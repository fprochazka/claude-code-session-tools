---
name: session-tools
description: CLI for finding and distilling past Claude Code sessions — list and grep the sessions of the current repo and its git worktrees, then condense one transcript into markdown. Use when the user refers to an earlier conversation, asks what was decided or tried before, wants a past session summarized, or needs the session id of something that already happened. Triggered by requests involving past sessions, an earlier conversation, a session transcript, "what did we decide before", or "find the session where".
trigger-keywords: claude code session, past session, previous session, session transcript, session history, ccdistill, session search
allowed-tools: Bash(cc-session-tools --help), Bash(cc-session-tools search:*), Bash(cc-session-tools distill:*)
---

# cc-session-tools

Claude Code writes every session as a JSONL transcript under `<config dir>/projects/<encoded-cwd>/<session-id>.jsonl`, where `<encoded-cwd>` is the absolute launch directory with every non-alphanumeric character replaced by `-`. This tool reads the projects dir of every Claude Code profile on the machine, each shared dir once — or only `CLAUDE_PROJECTS_DIR` when it is set — and never writes to it. `search` maps the current directory back onto its project dirs and lists the sessions; `distill` turns one transcript into a markdown recap. Both read the transcript content itself rather than file metadata, so ordering and names reflect the real conversation.

## search

```
Usage: cc-session-tools search [OPTIONS]

  List Claude Code sessions for the current directory, newest interaction
  first.

Options:
  -n, --limit <int>  Maximum number of sessions to show (newest first). A
                     value <= 0 means no limit.  [default: 20]
  --rg <str>         Only show sessions whose transcript contains this literal
                     text (case-insensitive; uses ripgrep when installed).
                     Repeatable: every keyword must match somewhere in the
                     session.
  --rg-any           Match sessions containing ANY of the --rg keywords
                     instead of all of them. Only meaningful alongside --rg.
  --all              List sessions from every project instead of the current
                     directory and its git worktrees.
  --help             Show this message and exit.
```

Operational notes:

- **Do not filter the output.** The table is short; piping it through `head`, `grep` or `jq` loses rows and the column alignment. At most redirect it to a scratchpad file.
- **Scope.** Inside a git repo the listing covers the main checkout and every worktree of it, including worktrees that have since been deleted — their transcripts still exist. Outside a git repo only the exact current directory is covered. `--all` ignores the repo entirely and lists every project, with the last column showing each session's own directory. When the rows come from more than one projects dir, a `Profile` column names the profile each session belongs to.
- **A session belongs to the directory it was launched from**, taken from the first `cwd` record in the transcript. A session that started one directory up and later moved into the repo is not listed here; `--all` still finds it.
- **Ordering** is by the last interaction timestamp read from inside the transcript, not by file mtime — Claude Code appends titles and summaries retroactively, which bumps mtime long after the work happened.
- **`--rg` is literal, case-insensitive text**, not a regex. It uses `rg` (ripgrep) when it is on PATH and a slower built-in scan otherwise. Matches inside subagent transcripts and tool-result sidecars count and fold up into the parent session.
- **Repeating `--rg` is AND at the level of the session**: every keyword must appear somewhere in it, but they may sit in different messages or in a subagent it spawned. `--rg-any` flips that to OR. Each AND keyword costs its own ripgrep pass; the OR costs one pass no matter how many keywords.
- **`--all` reads every project** — about 2 seconds over ~3,000 sessions with a warm disk cache, plus one ripgrep pass per `--rg` keyword. Prefer the repo-scoped default.
- **A dimmed name** is a title derived from the first human message, not a real session name.
- Exit codes: `0` including when nothing matched (the notice goes to stderr), `2` when ripgrep fails.

## distill

```
Usage: cc-session-tools distill [OPTIONS] {target}

  Render a condensed markdown transcript of a session.

Arguments:
  target  A .jsonl path, a session UUID, or the literal 'latest'.  [required]

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
- **A UUID target** is looked up across every project dir, so it works from anywhere. Subagent sidecars under `<session-id>/subagents/` are not sessions and cannot be reached by id.
- **Headings name who sent a prompt**: `👤 User` for the user, `📨 Peer session` for a message from another Claude Code session, `🔔 Task notification` for a finished background task. Only `👤 User` prompts are the user's own words.
- **A compacted session is split at its compactions.** The main file `<session-id>.md` holds only what the model worked with after the last compaction: the compaction summary under `## 📦 Compaction summary`, the stretch of conversation Claude Code kept verbatim, and everything after. Its first line under the title is a `> Compacted session: …` pointer to the absolute path of the part before, `<session-id>.pre-compact-<k>.md` in the same dir, and each part points back to the one before it, down to `pre-compact-1`, the oldest. Each pre-compact part ends with a `> Continued after compaction <k> in …` line. For a recap of the whole session, follow the pointers and read every part, oldest first; for what the session ended up with, the main file is enough.
- `--stdout` writes the main markdown to stdout instead of a file; the stats table still goes to stderr, so the stdout stays valid markdown. The pre-compact parts of a compacted session are still written to `--output-dir`, so the pointer line names files that exist.
- Exit code `1` when the target does not resolve or the file cannot be read; the message is a plain `Error: …` line on stderr.

## Slash commands

Two commands wrap the full workflows and should be preferred over driving the CLI by hand:

- `/session-tools:search <keywords or topic>` — mine past sessions for decisions and context, writing the findings to `./.claude/plans/past-sessions-<slug>.md`.
- `/session-tools:distill <session-id | latest>` — condense one session into a recap at `./.claude/plans/session-distill-<slug>.md`.
