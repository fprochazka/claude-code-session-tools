"""Builders for transcripts with tool calls, subagents, wakeups and compactions, for the time analysis tests.

The record shapes follow transcripts written by Claude Code 2.1.32x on Linux, with only the keys cc-blackbox and the
analysis read. Every id, path, prompt and command is invented: no real transcript content belongs in these tests.

Times are seconds after :data:`START`, so a test reads as a schedule. A builder appends records in the order it is
called, which is the file order; call it out of time order to write a record late, as Claude Code sometimes does.
"""

from __future__ import annotations

import itertools
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from cc_blackbox import SessionTranscript, project_slug, read_session_transcript
from conftest import compaction_records, log_start, sid, write_records

SESSION_ID = sid("time-analysis")
CWD = "/home/user/projects/example"
VERSION = "2.1.322"
MODEL = "claude-opus-5-5"
START = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)

_TURN_ORIGINS = {"human": "human", "peer": "peer", "task-notification": "task_notification"}


def at(t: float) -> str:
    """The transcript timestamp ``t`` seconds after :data:`START`, to the millisecond, in Claude Code's format."""
    moment = START + timedelta(seconds=t)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def usage(*, fresh: int = 3, output: int = 50, cache_read: int = 20_000, cache_creation: int = 500) -> dict[str, Any]:
    """The ``usage`` of one API response. Claude Code repeats it on every content block of the response."""
    return {
        "input_tokens": fresh,
        "cache_creation_input_tokens": cache_creation,
        "cache_read_input_tokens": cache_read,
        "output_tokens": output,
    }


def permission(
    source: str,
    *,
    decision: str = "accept",
    reason_type: str | None = None,
    reason_code: str | None = None,
    grant_scope: str | None = None,
    classifier_category: str | None = None,
    rule_source: str | None = None,
) -> dict[str, Any]:
    """A ``permissionDecision`` for a tool result record.

    ``source`` is ``config`` (a rule, the mode, or the auto mode classifier with ``reason_type="classifier"``),
    ``hook``, ``user_temporary`` (the user approved a prompt), ``user_permanent`` (approved and saved as a rule) or
    ``user_reject``.
    """
    value = {
        "decision": decision,
        "source": source,
        "reasonType": reason_type,
        "reasonCode": reason_code,
        "grantScope": grant_scope,
        "classifierCategory": classifier_category,
        "ruleSource": rule_source,
    }
    return {k: v for k, v in value.items() if v is not None}


@dataclass
class Request:
    """One API request and its response, whose content blocks each become an assistant record."""

    transcript: Transcript
    id: str
    message_id: str
    model: str
    usage: dict[str, Any]
    logged: bool = True
    """``False`` for older versions, which wrote neither the ``api-request`` record nor ``requestRef``."""

    def text(self, t: float, value: str = "Working on it.") -> dict[str, Any]:
        return self.transcript.block(self, t, {"type": "text", "text": value})

    def call(self, t: float, name: str, *, call_id: str | None = None, **tool_input: Any) -> str:
        """Stream a ``tool_use`` block at ``t``; return its id for the result."""
        call_id = call_id or self.transcript.next_id("toolu")
        record = self.transcript.block(self, t, {"type": "tool_use", "id": call_id, "name": name, "input": tool_input})
        self.transcript.call_records[call_id] = record["uuid"]
        return call_id

    def bash(self, t: float, command: str, *, background: bool = False) -> str:
        if background:
            return self.call(t, "Bash", command=command, run_in_background=True)
        return self.call(t, "Bash", command=command)

    def agent(
        self,
        t: float,
        agent_id: str,
        *,
        description: str = "impl: C1",
        agent_type: str = "general-purpose",
        prompt: str = "Do the step and report.",
        background: bool = False,
        started: float | None = None,
    ) -> Subagent:
        """Stream an ``Agent`` call at ``t`` and start its subagent at ``started`` (``t`` by default).

        The subagent's file opens with the prompt, as Claude Code writes it. A call made inside a subagent spawns a
        nested one.
        """
        call_id = self.call(
            t, "Agent", description=description, subagent_type=agent_type, prompt=prompt, run_in_background=background
        )
        return self.transcript.spawn(
            call_id,
            agent_id,
            description=description,
            agent_type=agent_type,
            prompt=prompt,
            background=background,
            started=t if started is None else started,
        )

    def send_message(
        self, t: float, to: Subagent, message: str = "Re-check the fix.", *, result_at: float | None = None
    ) -> str:
        """Resume ``to`` with ``message`` by ``SendMessage`` at ``t``; the call returns at ``result_at`` (``t + 1``)."""
        call_id = self.call(t, "SendMessage", to=to.agent_id, message=message)
        to.resumed(t, message)
        self.transcript.result(
            t + 1 if result_at is None else result_at,
            call_id,
            "Message sent.",
            tool_use_result={"success": True, "message": "Message sent.", "resumedAgentId": to.agent_id},
        )
        return call_id


class Transcript:
    """A session's main transcript, or with ``agent_id`` the transcript of one of its subagents."""

    def __init__(self, *, session_id: str = SESSION_ID, cwd: str = CWD, agent_id: str | None = None) -> None:
        self.session_id = session_id
        self.cwd = cwd
        self.agent_id = agent_id
        self.records: list[dict[str, Any]] = []
        self.subagents: list[Subagent] = []
        """Every subagent of the session, nested ones included; kept on the main transcript only."""
        self.call_records: dict[str, str] = {}
        self._ids: Iterator[int] = itertools.count(1)
        self._last_uuid: str | None = None
        self._turn_start: float | None = None

    @property
    def root(self) -> Transcript:
        return self

    @property
    def query_source(self) -> str:
        """The ``querySource`` of the transcript's ``api-request`` records."""
        return "repl_main_thread"

    def next_id(self, prefix: str) -> str:
        return f"{prefix}_{self.agent_id or 'main'}_{next(self._ids):03d}"

    def _uuid(self) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.session_id}:{self.agent_id}:{next(self._ids)}"))

    def add(self, record: dict[str, Any]) -> dict[str, Any]:
        """Append any record, for shapes no method builds."""
        self.records.append(record)
        if "uuid" in record:
            self._last_uuid = record["uuid"]
        return record

    def envelope(self, record_type: str, t: float, **extra: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "parentUuid": self._last_uuid,
            "isSidechain": self.agent_id is not None,
            "type": record_type,
            "uuid": self._uuid(),
            "timestamp": at(t),
            "userType": "external",
            "entrypoint": "cli",
            "cwd": self.cwd,
            "sessionId": self.session_id,
            "version": VERSION,
            "gitBranch": "main",
        }
        if self.agent_id is not None:
            record["agentId"] = self.agent_id
        record.update(extra)
        return self.add(record)

    def api_error(
        self, t: float, text: str = "Prompt is too long", *, error: str = "invalid_request", status: int | None = None
    ) -> dict[str, Any]:
        """The synthetic assistant record Claude Code writes for a request the API refused."""
        message = {
            "id": self.next_id("msg_err"),
            "type": "message",
            "role": "assistant",
            "model": "<synthetic>",
            "content": [{"type": "text", "text": text}],
            "usage": usage(fresh=0, output=0, cache_read=0, cache_creation=0),
        }
        return self.envelope(
            "assistant", t, message=message, isApiErrorMessage=True, error=error, apiErrorStatus=status
        )

    # Prompts

    def prompt(
        self,
        t: float,
        text: str = "Continue with the next step.",
        *,
        origin: str | None = "human",
        turn_origin: str | None = None,
        sender: str | None = None,
        handback: bool = False,
        wakeup_source: str | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        """A prompt that opens a turn.

        ``origin`` is the ``origin.kind`` (``human``, ``peer``, ``task-notification``) or ``None`` for prompts without
        one: slash commands, scheduled and SDK prompts, and older versions. ``turn_origin`` defaults to the one that
        goes with ``origin``. ``sender`` is a peer's ``origin.from``; ``handback`` marks a subagent's hand-back.
        """
        if turn_origin is None and origin is not None:
            turn_origin = _TURN_ORIGINS.get(origin)
        fields: dict[str, Any] = {"message": {"role": "user", "content": text}}
        if origin is not None:
            fields["origin"] = {"kind": origin}
            if sender is not None:
                fields["origin"]["from"] = sender
            if handback:
                fields["origin"]["handback"] = True
            fields["promptSource"] = "typed" if origin == "human" else "system"
            if origin != "human":
                fields["isMeta"] = True
        if turn_origin is not None:
            fields["turnOrigin"] = turn_origin
        if wakeup_source is not None:
            fields["wakeupSource"] = wakeup_source
        fields.update(extra)
        self._turn_start = t
        return self.envelope("user", t, **fields)

    def sdk_prompt(self, t: float, text: str = "Summarize the change.") -> dict[str, Any]:
        """A headless (SDK) prompt: ``turnOrigin: sdk`` and no ``origin``."""
        return self.prompt(t, text, origin=None, turn_origin="sdk")

    def scheduled(
        self,
        t: float,
        text: str = "Check the build.",
        *,
        wakeup: bool = False,
        task_id: str = "cron-0001",
        fire: bool = True,
    ) -> dict[str, Any]:
        """A cron fire, or with ``wakeup`` a wakeup the session scheduled itself, and the prompt that opens its turn.

        ``fire=False`` leaves out the ``scheduled_task_fire`` record that comes first.
        """
        if fire:
            self.system("scheduled_task_fire", t, taskId=task_id, cron="*/30 * * * *", prompt=text, isMeta=True)
        return self.prompt(
            t,
            text,
            origin=None,
            turn_origin="scheduled",
            wakeup_source="schedule_wakeup" if wakeup else None,
            isMeta=True,
            promptSource="system",
            scheduledTaskId=task_id,
        )

    def handback(self, t: float, sub: Subagent, report: str = "Done; all checks pass.") -> dict[str, Any]:
        """A subagent's report delivered as a peer message from its agent id."""
        return self.prompt(t, f"[Subagent hand-back] {report}", origin="peer", sender=sub.agent_id, handback=True)

    def moved_to_background(
        self,
        t: float,
        call_id: str,
        task_id: str,
        *,
        after_ms: int | None = 180_000,
        capped: bool = True,
        to_deliver_message: bool = False,
        by_user: bool = False,
    ) -> dict[str, Any]:
        """The result of a Bash call that runs as background task ``task_id``: moved there by Claude Code after
        ``after_ms``, with ``capped`` by the subagent cap, with ``to_deliver_message`` so that a message could reach
        the agent, with ``by_user`` by the user, or with ``after_ms=None`` launched there (or moved for a reason this
        version doesn't know, when the call was not launched with ``run_in_background``)."""
        meta: dict[str, Any] = {"stdout": "", "stderr": "", "backgroundTaskId": task_id}
        if to_deliver_message or by_user:
            meta["backgroundedToDeliverMessage" if to_deliver_message else "backgroundedByUser"] = True
            text = f"Command was moved to the background (ID: {task_id})."
        elif after_ms is None:
            text = f"Command running in background with ID: {task_id}."
        else:
            meta["timedOutAfterMs"] = after_ms
            if capped:
                meta["subagentCapApplied"] = True
            text = (
                f"Command was moved to the background (ID: {task_id}) after running for {after_ms // 1000}s."
                " You will be notified when it completes."
            )
        return self.result(t, call_id, text, tool_use_result=meta)

    def task_notification(
        self, t: float, task_id: str, *, call_id: str | None = None, status: str = "completed", queued: bool = False
    ) -> dict[str, Any]:
        """The notice that a background agent or command stopped: a prompt, or with ``queued`` a ``queued_command``
        attachment for one that arrived while the session was busy."""
        text = (
            f"<task-notification><task-id>{task_id}</task-id><tool-use-id>{call_id or ''}</tool-use-id>"
            f"<status>{status}</status><summary>Task {status}</summary></task-notification>"
        )
        if queued:
            return self.queued(t, text, mode="task-notification", origin="task-notification")
        return self.prompt(t, text, origin="task-notification")

    def queued(
        self, t: float, text: str, *, mode: str | None = "prompt", origin: str = "human", sender: str | None = None
    ) -> dict[str, Any]:
        """A ``queued_command`` attachment: a prompt that arrived while the session was busy.

        ``mode=None`` leaves out ``commandMode``, as for a message to a subagent, such as a nested subagent's hand-back
        (``origin="peer"`` with the child's ``sender``).
        """
        payload: dict[str, Any] = {
            "type": "queued_command",
            "prompt": text,
            "origin": {"kind": origin},
            "timestamp": at(t),
        }
        if mode is not None:
            payload["commandMode"] = mode
        if sender is not None:
            payload["origin"]["from"] = sender
        return self.envelope("attachment", t, attachment=payload)

    def local_command(self, t: float, command: str = "/compact") -> dict[str, Any]:
        """A local slash command: a user record with string content and no ``origin``. It starts no model turn."""
        text = f"<command-name>{command}</command-name><command-message>{command.lstrip('/')}</command-message>"
        return self.envelope("user", t, message={"role": "user", "content": text})

    # Model responses and tool calls

    def request(
        self, t: float, *, model: str = MODEL, tokens: dict[str, Any] | None = None, logged: bool = True
    ) -> Request:
        """Start an API request at ``t``; its blocks follow through the returned :class:`Request`.

        ``tokens`` is the :func:`usage` of its response.

        ``logged=False`` leaves out the ``api-request`` record and the blocks' ``requestRef``, as older versions such as
        2.1.269 did.
        """
        request_id = self.next_id("req")
        if logged:
            record: dict[str, Any] = {
                "type": "api-request",
                "id": request_id,
                "timestamp": at(t),
                "sessionId": self.session_id,
                "querySource": self.query_source,
                "params": json.dumps({"model": model}),
            }
            if self.agent_id is not None:
                record["agentId"] = self.agent_id
            self.add(record)
        return Request(self, request_id, self.next_id("msg"), model, tokens if tokens is not None else usage(), logged)

    def block(self, request: Request, t: float, content: dict[str, Any]) -> dict[str, Any]:
        """One content block of ``request``'s response, as its own assistant record."""
        message = {
            "id": request.message_id,
            "type": "message",
            "role": "assistant",
            "model": request.model,
            "content": [content],
            "stop_reason": "tool_use" if content["type"] == "tool_use" else "end_turn",
            "usage": request.usage,
        }
        if not request.logged:
            return self.envelope("assistant", t, message=message, requestId=request.message_id)
        return self.envelope("assistant", t, message=message, requestId=request.message_id, requestRef=request.id)

    def result(
        self,
        t: float,
        call_id: str,
        content: str = "ok",
        *,
        permission: dict[str, Any] | None = None,
        tool_use_result: Any = None,
        is_error: bool = False,
        denial: str | None = None,
    ) -> dict[str, Any]:
        """The result record of ``call_id``. ``permission`` is a :func:`permission`; leave it out for old versions."""
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call_id, "content": content}
        if is_error:
            block["is_error"] = True
        fields: dict[str, Any] = {
            "message": {"role": "user", "content": [block]},
            "toolUseResult": tool_use_result if tool_use_result is not None else {"stdout": content, "stderr": ""},
            "sourceToolAssistantUUID": self.call_records.get(call_id),
        }
        if permission is not None:
            fields["permissionDecision"] = permission
        if denial is not None:
            fields["toolDenialKind"] = denial
        return self.envelope("user", t, **fields)

    def agent_returned(self, t: float, sub: Subagent, report: str = "Done; all checks pass.") -> dict[str, Any]:
        """The result of ``sub``'s ``Agent`` call: its report for a foreground one, the launch notice for a background
        one."""
        if sub.background:
            meta: dict[str, Any] = {
                "status": "async_launched",
                "isAsync": True,
                "agentId": sub.agent_id,
                "canReadOutputFile": False,
            }
            return self.result(t, sub.call_id, "Async agent launched.", tool_use_result=meta)
        meta = {
            "status": "completed",
            "agentId": sub.agent_id,
            "agentType": sub.agent_type,
            "totalDurationMs": round((t - sub.started) * 1000),
            "totalTokens": 0,
            "totalToolUseCount": 0,
        }
        return self.result(t, sub.call_id, report, tool_use_result=meta)

    def spawn(
        self,
        call_id: str,
        agent_id: str,
        *,
        description: str,
        agent_type: str,
        prompt: str,
        background: bool,
        started: float,
    ) -> Subagent:
        sub = Subagent(
            self,
            call_id,
            agent_id,
            description=description,
            agent_type=agent_type,
            background=background,
            started=started,
        )
        self.root.subagents.append(sub)
        sub.envelope("user", started, message={"role": "user", "content": prompt})
        return sub

    # System records

    def system(self, subtype: str, t: float, **extra: Any) -> dict[str, Any]:
        return self.envelope("system", t, subtype=subtype, level="info", **extra)

    def turn_end(
        self, t: float, *, duration_ms: int | None = None, pending_background: int | None = None
    ) -> dict[str, Any]:
        """The ``turn_duration`` record that ends a turn. ``duration_ms`` defaults to the time since the last prompt."""
        if duration_ms is None:
            duration_ms = round((t - (self._turn_start or 0)) * 1000)
        extra = {} if pending_background is None else {"pendingBackgroundAgentCount": pending_background}
        return self.system("turn_duration", t, durationMs=duration_ms, messageCount=2, isMeta=False, **extra)

    def compaction(
        self, t: float, *, trigger: str = "auto", duration_ms: int | None = 60_000, pre_tokens: int = 180_000
    ) -> list[dict[str, Any]]:
        """A compaction at ``t``: the boundary and the summary. ``duration_ms=None`` writes the older shape without
        ``durationMs``; a manual compaction whose summary was precomputed takes about 20 ms."""
        records = compaction_records(
            self._uuid(),
            timestamp=at(t),
            logical_parent=self._last_uuid,
            trigger=trigger,
            pre_tokens=pre_tokens,
            duration_ms=duration_ms,
        )
        for record in records:
            if self.agent_id is not None:
                record.update(isSidechain=True, agentId=self.agent_id, sessionId=self.session_id)
            self.add(record)
        return records

    # Files

    def path(self, projects_dir: Path) -> Path:
        return projects_dir / project_slug(self.cwd) / f"{self.session_id}.jsonl"

    def write(self, projects_dir: Path) -> Path:
        """Write the main transcript and its subagents' files under ``projects_dir``; return the main file."""
        main = write_records(self.path(projects_dir), self.records)
        folder = main.with_suffix("") / "subagents"
        for sub in self.subagents:
            write_records(folder / f"agent-{sub.agent_id}.jsonl", sub.records)
            if sub.has_meta:
                (folder / f"agent-{sub.agent_id}.meta.json").write_text(json.dumps(sub.meta()), encoding="utf-8")
        return main

    def load(self, projects_dir: Path) -> SessionTranscript:
        """Write the session and read it back as cc-blackbox does."""
        return read_session_transcript(self.write(projects_dir))

    def log_start(self, projects_dir: Path, t: float, **fields: Any) -> Path:
        """Append a start at ``t`` to the auth-switch start log, for example with ``role="worker"``."""
        ms = round((START + timedelta(seconds=t)).timestamp() * 1000)
        return log_start(self.path(projects_dir).parent, self.session_id, ms, **fields)


class Subagent(Transcript):
    """A subagent's transcript and its ``.meta.json``."""

    def __init__(
        self,
        parent: Transcript,
        call_id: str,
        agent_id: str,
        *,
        description: str,
        agent_type: str,
        background: bool,
        started: float,
    ) -> None:
        super().__init__(session_id=parent.session_id, cwd=parent.cwd, agent_id=agent_id)
        self.parent = parent
        self.call_id = call_id
        self.description = description
        self.agent_type = agent_type
        self.background = background
        self.started = started
        self.finished = True
        self.has_meta = True
        """``False`` leaves out the ``.meta.json``, as for subagents of older versions."""

    @property
    def root(self) -> Transcript:
        return self.parent.root

    @property
    def query_source(self) -> str:
        return f"agent:builtin:{self.agent_type}"

    @property
    def depth(self) -> int:
        return self.parent.depth + 1 if isinstance(self.parent, Subagent) else 1

    def resumed(self, t: float, message: str) -> dict[str, Any]:
        """A message that resumes the subagent for another round: a ``queued_command`` from its coordinator."""
        payload = {"type": "queued_command", "prompt": message, "origin": {"kind": "coordinator"}, "isMeta": True}
        return self.envelope("attachment", t, attachment=payload)

    def meta(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "agentType": self.agent_type,
            "description": self.description,
            "toolUseId": self.call_id,
            "spawnDepth": self.depth,
            "requestShape": "background" if self.background else "foreground",
            "requestNonInteractive": False,
            "isBuiltIn": self.agent_type == "general-purpose",
        }
        if isinstance(self.parent, Subagent):
            value["parentAgentId"] = self.parent.agent_id
        if self.finished:
            value["finished"] = True
        return value
