---
description: Find past Claude Code sessions and extract decisions and context out of them
argument-hint: <keywords or topic>
allowed-tools: Bash(cc-session-tools search:*), Bash(cc-session-tools distill:*), Agent
---

# Session Mine

Find and mine past sessions for decisions and context.

## Search command

```!
cc-session-tools search --help
```

The command outputs a table with results, do not pipe it through any output filtering, at most redirect the output to a scratchpad file.
The command works in either git root dir or git worktree, and searches across any session relevant for the given git worktree.

## Distill command

```!
cc-session-tools distill --help
```

This command takes a session and outputs a path to an `.md` file that has only the important parts of the session. Do not filter its output, it's short and won't take much context.

## Session mining scope

The user wants you to focus on the following queries to find the context for:

$ARGUMENTS

## Session mining workflow

The user has given you a context/query/focus-area, and wants you to find past sessions using `cc-session-tools search` that have dealt with the context we're searching for.
Once you have them, ideally `cc-session-tools distill` them, but its up to you if you want to do that, or if you need to mine for something specific in the `.jsonl` transcripts.

Make sure to run the mining operation as a subagent (or multiple), ideally orient yourself first in the keywords we're looking for, and make sure to filter out irrelevant sessions that might still turn up for the relevant keywords.
The mining subagent should write its findings to `./.claude/plans/past-sessions-<slug>.md` (`<slug>` — a short kebab-case slug of the query/topic, the same naming the `/cc-session-tools:distill` command uses for `session-distill-<slug>.md`) and return the file as a reply with some summary of what it did, the methodology and what it scanned and what it ignored and why, the findings should be all in the md file.

Once you're done collecting the information you were asked for, read it and present the user with a condensed summary.
