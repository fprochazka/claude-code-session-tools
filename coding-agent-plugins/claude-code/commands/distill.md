---
description: Distill a past Claude Code session into a recap a follow-up session can resume from
argument-hint: <session-id | latest>
allowed-tools: Bash(cc-session-tools distill:*), Agent, SendMessage
---

# Session Distill

Condense an earlier Claude Code session into a recap that captures only what still matters — final decisions, outcomes, and references — so a fresh session can pick up the thread without replaying the whole history.

## Distill command

```!
cc-session-tools distill --help
```

This command takes a session and outputs a path to an `.md` file that has only the important parts of the session. Do not filter its output, it's short and won't take much context.

Run the distill command in the orchestrator agent (the current one), only hand the output file(s) to the subagents.

## Context

Use the following session id(s) and extra user instructions to do the distill:

$ARGUMENTS

## Your task

Spawn a **single subagent** (`subagent_type` default, `opus` model) to read and distill the transcript. Pass it the transcript file path from the Context output above. Do **not** read the transcript yourself — it is large and belongs in the subagent's context, not yours.

Instruct the subagent exactly as follows:

> You are distilling a Claude Code session transcript into a recap for a future session.
>
> 1. **Read the full transcript file first** (path given below) before writing anything. Read it in its entirety.
> 2. **Collapse superseded history.** When a decision, plan, value, or piece of information was later changed, corrected, or abandoned, keep **only the final state**. Discard the intermediate steps, dead ends, and walked-back choices — the reader does not need the journey, only where it landed. The one exception: if a decision *pivoted*, note the rejected alternative and what triggered the pivot in one line, because that is load-bearing context.
> 3. **Write the recap to `./.claude/plans/session-distill-<slug>.md`** — `<slug>` is a short kebab-case slug of what the session was about, the same naming the `/cc-session-tools:search` command uses for `past-sessions-<slug>.md` (fall back to the session id when the topic is unclear). Create the directory if it does not exist. The full recap lives in this file and nowhere else. Use this structure:
>
>    ## What this session was about
>    2-4 sentences. The high-level goal and where the session ended up.
>
>    ## Final decisions & outcomes
>    Verbose and concrete. Every decision that survived to the end of the session, every outcome reached, every open question left unresolved. State the *current* position, not the history of how it was reached (except pivots, per rule 2).
>
>    ## References
>    Everything the next session will need to fetch fresh. Capture refs, not content:
>    - Linked tickets (Linear `ABC-1234`, GitHub `#789`), with their one-line subject if known
>    - Merge requests / PRs (with URL or `!123` ref)
>    - Local files the session produced or relied on — plans, pre-plans, research notes, handover docs, scratch markdown (**absolute paths**)
>    - Branches, key code paths touched
>    - Any external URLs or dashboards referenced
>
>    ## For the next session
>    Anything else a follow-up session must know that isn't covered above: in-flight work, environment caveats, things explicitly ruled out (so they aren't re-litigated), gotchas discovered.
>
> Be **verbose in the file**. Omit a section only if it genuinely has nothing.
>
> 4. **Your final message is not the recap.** Reply with the absolute path of the recap file, then at most 10 lines that let the reader judge whether you read the right transcript and focused on the right parts:
>    - one line naming the session topic and its date/length,
>    - 3-6 bullets naming the *topics* the recap covers (a few words each, not their content),
>    - one line on what you deliberately dropped as superseded or noise,
>    - one line for anything the extra user instructions asked for and you could not find.
>
>    Never paste the recap sections into the reply.

## Reporting back to the user

Relay **at most 20 lines**: the recap file path, then the subagent's short bullets. Keep it a scan-check, not a second copy of the recap.

- Do **not** repeat the recap content, quote its sections, or expand its bullets.
- Do **not** add your own summary of the findings on top of the subagent's bullets.
- If the user gave extra instructions in `$ARGUMENTS`, say in one line whether the distill covered them.

## Follow-up questions

The distill subagent still holds the whole transcript in its context. You do not.

- When the user asks anything more about the session — a detail the recap left out, why something was dropped, what exactly was said about X — **resume that subagent with `SendMessage`** using its id or name.
- Do **not** re-read the transcript yourself, and do **not** spawn a fresh subagent for the same session. Both replay work the first subagent already did.
- Keep the subagent's id for the rest of the session so you can reach it again later.
- Apply the same reply discipline to its answers: relay the answer, do not paste the recap back.
