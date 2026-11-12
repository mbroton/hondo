from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from typing import Any, TypedDict, cast
from uuid import uuid4

import logfire_api as logfire
import structlog
from pydantic import BaseModel, ConfigDict
from pydantic_ai import Agent, ToolOutput
from pydantic_ai._enqueue import PendingMessage
from pydantic_ai.agent import AbstractAgent
from pydantic_ai.messages import (
    ImageUrl,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    UserContent,
    UserPromptPart,
)

from hondo.chat import ChatClient, ChatFile, ChatStyle
from hondo.config import RuntimeConfig
from hondo.context import RunScope, TodoState, WaitClock, reset_scope, set_scope
from hondo.models import (
    NO_REPLY,
    Event,
    NoReply,
    PendingFailure,
    Session,
    SessionKey,
    SessionState,
)
from hondo.runtime.durable import claim_token, run_workflow_id, sleep, step
from hondo.runtime.model_selection import ModelSelector, RunModel
from hondo.runtime.progress import ProgressReporter
from hondo.store.base import Store
from hondo.usage import rate_usage_rows, usage_rows_from_responses
from hondo.users import UserDirectory, expand_mentions, mention_ids

logger = structlog.get_logger("hondo.runner")

# How long a cancelled run body gets to unwind cleanly before it is abandoned.
_CANCEL_GRACE_SECONDS = 5.0
_WAIT_CHECK_SECONDS = 1.0


@dataclass(frozen=True)
class _Snapshot:
    messages: list[ModelMessage]
    stored_message_count: int
    covers: frozenset[str]
    delivered_steer_ids: frozenset[int]


@dataclass(frozen=True)
class _Recovery:
    covered: frozenset[str]
    delivered_steer_ids: frozenset[int]
    adopted: bool


class _SnapshotDecision(TypedDict):
    adopted: list[str]
    adopted_count: int
    covered: list[str]
    delivered_steer_ids: list[int]


class _SnapshotPayload(BaseModel):
    model_config = ConfigDict(strict=True, hide_input_in_errors=True)

    messages: list[dict[str, Any]]
    covers: list[str]
    delivered_steer_ids: list[int]


async def _new_agent_run_id() -> str:
    return str(uuid4())


def _dump_messages(messages: list[ModelMessage]) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", ModelMessagesTypeAdapter.dump_python(messages, mode="json"))


def _pending_contents(pending: Sequence[PendingMessage]) -> list[list[UserContent]]:
    """Flatten undelivered enqueue leftovers into prompt-ready turns."""
    contents: list[list[UserContent]] = []
    for item in pending:
        for message in item.messages:
            if isinstance(message, ModelRequest):
                for part in message.parts:
                    if isinstance(part, UserPromptPart):
                        content = part.content
                        contents.append([content] if isinstance(content, str) else list(content))
    return contents


def _line(content: Sequence[UserContent]) -> str:
    """The transcript line of one rendered message."""
    return next((item for item in content if isinstance(item, str)), "")


def prompt_content(messages: Iterable[Sequence[UserContent]]) -> str | list[UserContent]:
    """Rendered messages as one prompt: lines apart by a blank line, each
    image after the line of its message."""
    parts: list[UserContent] = []
    for content in messages:
        for item in content:
            if isinstance(item, str) and parts and isinstance(parts[-1], str):
                parts[-1] = f"{parts[-1]}\n\n{item}"
            else:
                parts.append(item)
    if not parts:
        return ""
    return parts[0] if len(parts) == 1 and isinstance(parts[0], str) else parts


def _snapshot_from_payload(payload: dict[str, Any]) -> _Snapshot:
    validated = _SnapshotPayload.model_validate(payload)
    messages = ModelMessagesTypeAdapter.validate_python(validated.messages)
    if not any(
        isinstance(message, ModelRequest)
        and any(isinstance(part, UserPromptPart) for part in message.parts)
        for message in messages
    ):
        raise ValueError("snapshot must contain a user prompt")
    if not any(isinstance(message, ModelResponse) for message in messages):
        raise ValueError("snapshot must contain a model response")
    # A dying run's snapshot can end in a response whose tool calls never got
    # answers. Mark that response interrupted — which is what happened — so
    # pydantic-ai closes its calls with synthesized returns instead of
    # rejecting the next prompt over unprocessed tool calls.
    last = messages[-1]
    if isinstance(last, ModelResponse) and last.tool_calls and last.state != "interrupted":
        messages[-1] = replace(last, state="interrupted")
    return _Snapshot(
        messages=messages,
        stored_message_count=len(messages),
        covers=frozenset(validated.covers),
        delivered_steer_ids=frozenset(validated.delivered_steer_ids),
    )


def render_message(
    user: str | None,
    text: str,
    ts: str,
    roster: dict[str, str],
    style: ChatStyle,
    files: Sequence[ChatFile] = (),
) -> str:
    """One Slack message as a transcript line: "[time] Name: text".

    Pure: names come from the roster (resolved in a durable step upstream).
    Whether a message is *for* the bot is visible in the text itself — a
    mention of the bot renders as its own @name — so there is no addressing
    label; etiquette teaches the model to read it like a person would.
    """
    speaker = (roster.get(user) or style.mention(user)) if user else "someone"
    notes = [f"[file: {file.name}]" for file in files if file.image_url is None]
    body = " ".join(part for part in (expand_mentions(text, roster), *notes) if part)
    stamp = style.format_time(ts)
    prefix = f"[{stamp}] " if stamp else ""
    return f"{prefix}{speaker}: {body}"


def render_content(
    message: dict[str, Any], roster: dict[str, str], style: ChatStyle
) -> list[UserContent]:
    """A message row ({user, text, ts, files}) as its line and its images.

    The prompt keeps only image URLs. MediaContent loads each image when a
    model request needs it, so no image is saved with the history."""
    files = [ChatFile(**file) for file in message.get("files") or ()]
    line = render_message(message["user"], message["text"], message["ts"], roster, style, files)
    images = [
        ImageUrl(file.image_url, media_type=file.image_type, identifier=file.name)
        for file in files
        if file.image_url is not None
    ]
    return [line, *images]


REPLY_OUTPUT = [
    str,
    ToolOutput(
        NoReply,
        name=NO_REPLY,
        description="End your turn without posting anything. Give a short reason. "
        "Only the logs keep it.",
    ),
]


def listening_guidance(goal: str) -> str:
    """The note on a message that Hondo got only because it listens in the channel."""
    return (
        "<guidance>\nNobody mentioned you in this message, and you did not join this "
        "thread. You got it because an admin set you to listen in this channel for "
        f"this goal:\n<goal>\n{goal}\n</goal>\nAnswer only when the goal applies and "
        f"you can add something specific and useful. Otherwise call {NO_REPLY}.\n</guidance>"
    )


# Request parts that don't reference the preceding model response. Anything
# else (tool returns, retry prompts) is response-bound: cutting history there
# orphans it from its subject and providers reject the request.
_INDEPENDENT_PARTS = (SystemPromptPart, UserPromptPart)


def _log_usage_recorded(key: SessionKey, event: Event, rows: list[dict[str, Any]]) -> None:
    """One info line per recorded batch — the spend trail in the logs.

    Called after the record_usage step, never from inside it: the write stays
    a pure recorded step. A recovered workflow re-runs this line on replay;
    that duplication is harmless and not worth suppressing. Costs come from
    rate_usage_rows — the single rows-to-money path — so the logged number can
    never drift from what the usage API or quota enforcement would compute.
    """
    rated = rate_usage_rows(rows)
    logger.info(
        "usage recorded",
        session=str(key),
        event_id=event.event_id,
        workflow_id=rows[0]["workflow_id"],
        rows=[
            {
                "provider": line.provider,
                "resource": line.resource,
                "in": line.quantity_in,
                "out": line.quantity_out,
                "cache_read": line.cache_read,
                "cache_write": line.cache_write,
                "cost_usd": str(line.cost_usd) if line.cost_usd is not None else None,
            }
            for line in rated.lines
        ],
        total_cost_usd=str(rated.total_cost_usd),
    )


def _trim_history(messages: list[ModelMessage], limit: int) -> list[ModelMessage]:
    """Bound history without separating tool returns from their calls.

    A request can contain both a user prompt and tool returns, so a user prompt
    alone does not identify a safe cut.
    """
    if len(messages) <= limit:
        return messages
    for i in range(len(messages) - limit, len(messages)):
        message = messages[i]
        if (
            isinstance(message, ModelRequest)
            and any(isinstance(part, UserPromptPart) for part in message.parts)
            and all(isinstance(part, _INDEPENDENT_PARTS) for part in message.parts)
        ):
            return messages[i:]
    return []


class SessionRunner:
    """The drive-session workflow body: one engaged session run, durably.

    `drive` runs as a DBOS workflow (registered by serve()): Slack I/O and store
    writes are steps, model/tool calls are steps via DBOSDurability and the tool
    boundary, and steers are claimed from the ledger in
    recorded steps — a crash at any point recovers by replaying recorded
    outputs, re-executing nothing that completed. Outside DBOS (unit tests,
    bring-your-own-loop) the same code runs as a plain coroutine with steps
    becoming direct calls.
    """

    def __init__(
        self,
        agent: AbstractAgent[Any, Any],
        *,
        store: Store,
        client: ChatClient,
        style: ChatStyle,
        directory: UserDirectory,
        runtime: RuntimeConfig | None = None,
        model_selector: ModelSelector | None = None,
        start_summary: Callable[[SessionKey, str], Awaitable[None]] | None = None,
        screen: Callable[[SessionKey, Event], Awaitable[bool]] | None = None,
    ) -> None:
        self._agent = agent
        self._store = store
        self._client = client
        self._style = style
        self._directory = directory
        self._runtime = runtime if runtime is not None else RuntimeConfig()
        self._model_selector = model_selector
        self._start_summary = start_summary
        self._screen = screen

    async def drive(self, key_json: dict[str, Any], event_json: dict[str, Any]) -> str:
        """Workflow entrypoint. JSON-shaped args so workflow inputs stay portable."""
        key = SessionKey.model_validate(key_json)
        event = Event.model_validate(event_json)
        with logfire.span(
            "session run {channel}/{thread_root}",
            channel=key.channel,
            thread_root=key.thread_root,
            event_id=event.event_id,
            slack_user=event.user,
            permalink=self._style.message_link(event.channel, event.ts),
        ):
            # A fresh run enqueued for a steered event can lose the race with live
            # injection: if the mail was already consumed, the reply reached a run.
            mail = await step("load_event_mail", self._load_event_mail, event.event_id)
            if mail is not None and mail["consumed"]:
                return ""
            if self._screen is not None and not await step("screen", self._screen, key, event):
                if mail is not None:
                    await step("consume_screened_mail", self._store.consume_mail, [mail["id"]])
                return ""

            output = await self._run(key, event, mail)
        # Every run ends here, also one that failed: the summary job reads the
        # thread from Slack, so it catches up with everything people wrote.
        if self._start_summary is not None and not event.is_dm:
            try:
                await self._start_summary(key, event.event_id)
            except Exception:
                logger.warning("thread summary did not start", session=str(key), exc_info=True)
        return output

    async def _run(self, key: SessionKey, event: Event, mail: dict[str, Any] | None) -> str:
        progress = ProgressReporter(
            self._client,
            channel=key.channel,
            thread_ts=None if event.is_dm else key.thread_root,
        )
        state: dict[str, Any] = {
            "answered": False,
            "spoke": False,
            "partial": [],
            "run_ids": set(),
            "enqueued_steers": {},
            "pending": [],
            "covers": [],
            "dead": False,
            "tool_calls": 0,
            "todos": TodoState(),
            "wait": WaitClock(),
            "answering": False,
        }

        def working() -> bool:
            # Most runs for a message without a mention end in silence, so
            # their status waits until Hondo starts to work with tools.
            return (
                not state["answering"]
                and state["wait"].since is None
                and (event.mentions_bot or state["tool_calls"] > 0)
            )

        status = asyncio.ensure_future(progress.keep_status(working))
        try:
            return await self._supervise(key, event, mail, progress, state)
        finally:
            status.cancel()
            await asyncio.wait({status})
            await progress.clear_status()

    async def _supervise(
        self,
        key: SessionKey,
        event: Event,
        mail: dict[str, Any] | None,
        progress: ProgressReporter,
        state: dict[str, Any],
    ) -> str:
        await self._debounce(key, event)

        # Wall clock, logging only — never a step output, so replay stays safe.
        started = time.monotonic()
        logger.info("run started", session=str(key), event_id=event.event_id, user=event.user)

        state["progressed_at"] = started
        # A separate task lets the workflow stop if cleanup ignores cancellation.
        body = asyncio.ensure_future(self._prepare_run(key, event, progress, state, mail))
        wait: WaitClock = state["wait"]
        try:
            stall = min(self._runtime.stall_timeout, self._runtime.run_timeout)
            while not body.done():
                if wait.since is not None:
                    await asyncio.wait({body}, timeout=_WAIT_CHECK_SECONDS)
                    continue
                # Until a run posts, a stretch with no model response or tool result
                # means it hangs.
                watched = not state["spoke"]
                stalls_at = max(state["progressed_at"], wait.resumed_at) + stall
                ends_at = started + self._runtime.run_timeout + wait.total
                now = time.monotonic()
                if watched and now >= stalls_at:
                    await self._stop_body(body, key, event, state)
                    logger.error(
                        "run made no progress before its first post; stopped",
                        session=str(key),
                        event_id=event.event_id,
                        stall=stall,
                    )
                    await self._persist_failure(
                        key, event, f"made no progress for {stall:.0f}s and was stopped", state
                    )
                    await self._report_failure(
                        progress,
                        state,
                        answered=False,
                        text=(
                            "⚠️ Hondo isn't responding. This run made no progress for "
                            f"{stall:.0f}s and was stopped. Mention it again to retry."
                        ),
                    )
                    return ""
                if now >= ends_at:
                    raise TimeoutError
                wake = min(ends_at, stalls_at) if watched else ends_at
                await asyncio.wait({body}, timeout=wake - now)
            output = body.result()
            logger.info(
                "run finished",
                session=str(key),
                event_id=event.event_id,
                answered=bool(state["answered"]),
                duration=round(time.monotonic() - started, 1),
            )
            return output
        except TimeoutError:
            await self._stop_body(body, key, event, state)
            logger.error(
                "run exceeded its time limit; stopped",
                session=str(key),
                event_id=event.event_id,
                timeout=self._runtime.run_timeout,
            )
            await self._persist_failure(
                key,
                event,
                f"hit the {self._runtime.run_timeout:.0f}s run time limit",
                state,
            )
            await self._report_failure(
                progress,
                state,
                answered=bool(state["answered"]),
                text=(
                    f"⚠️ This run hit its time limit ({self._runtime.run_timeout:.0f}s) "
                    "and was stopped. Mention Hondo again to retry."
                ),
            )
            return ""
        except asyncio.CancelledError:
            body.cancel()
            raise
        except Exception as exc:
            logger.exception("run failed; contained", session=str(key), event_id=event.event_id)
            # Type name only: the record must never hold exception messages
            # (they can carry secrets), and the note renders no cause anyway.
            await self._persist_failure(key, event, type(exc).__name__, state)
            await self._report_failure(
                progress,
                state,
                answered=bool(state["answered"]),
                text="⚠️ Hondo hit an error and couldn't finish this. Mention it again to retry.",
            )
            return ""

    async def _debounce(self, key: SessionKey, event: Event) -> None:
        """Wait until the thread is quiet, so one run answers a burst of messages.

        Messages that arrive meanwhile find this run live and become steer
        mail, which the opening prompt claims.
        """
        for attempt in itertools.count():
            wait = await step(f"debounce:{attempt}", self._debounce_wait, key, event.event_id)
            if wait <= 0:
                return
            await sleep(wait)

    async def _debounce_wait(self, key: SessionKey, event_id: str) -> float:
        # No wait before Hondo's first reply in a thread: a fast first answer
        # matters more, a burst is unlikely there, and a late message still
        # steers the run.
        if not await self._store.get_session_messages(key, limit=1):
            return 0.0
        ages = await self._store.waiting_message_ages(key, event_id)
        if ages is None:
            return 0.0
        since_first, since_last = ages
        return min(
            self._runtime.debounce_period - since_last,
            self._runtime.debounce_max_wait - since_first,
        )

    async def _prepare_run(
        self,
        key: SessionKey,
        event: Event,
        progress: ProgressReporter,
        state: dict[str, Any],
        mail: dict[str, Any] | None,
    ) -> str:
        selected_model = (
            await step("load_model", self._model_selector, key.team, key.channel)
            if self._model_selector is not None
            else None
        )
        if self._model_selector is not None and selected_model is None:
            if event.is_dm or event.mentions_bot:
                await step(
                    "model_not_configured",
                    self._client.post_notice,
                    channel=key.channel,
                    thread_ts=None if event.is_dm else key.thread_root,
                    text="Hondo needs a model before it can respond. "
                    "Ask a Hondo admin to select one in Settings. "
                    "Then send your request again.",
                )
            if mail is not None:
                await step("consume_unconfigured_mail", self._store.consume_mail, [mail["id"]])
            return ""
        session_data = await step("load_session", self._load_session, key)
        state["unjoined"] = session_data is None and not (event.mentions_bot or event.is_dm)
        history: list[ModelMessage] | None = None
        if session_data is not None and session_data.get("messages"):
            history = ModelMessagesTypeAdapter.validate_python(session_data["messages"])
            history = _trim_history(history, self._runtime.max_history_messages) or None
        pending = (
            PendingFailure.list_from_context(session_data.get("context") or {})
            if session_data
            else []
        )
        roster: dict[str, str] = dict(session_data.get("roster") or {}) if session_data else {}
        goal = (
            await step("load_listening_goal", self._store.listening_goal, key.team, key.channel)
            if state["unjoined"]
            else None
        )
        state["guidance"] = listening_guidance(goal) if goal is not None else None
        state["bot_user"] = event.bot_user
        scope = RunScope(
            key=key, event=event, roster=roster, todos=state["todos"], wait=state["wait"]
        )
        token = set_scope(scope)
        try:
            return await self._drive_body(
                key, event, history, progress, state, roster, pending, selected_model
            )
        finally:
            reset_scope(token)

    async def _drive_body(
        self,
        key: SessionKey,
        event: Event,
        history: list[ModelMessage] | None,
        progress: ProgressReporter,
        state: dict[str, Any],
        roster: dict[str, str],
        pending: list[PendingFailure],
        selected_model: RunModel | None,
    ) -> str:
        history_empty = history is None
        snapshot_decision: _SnapshotDecision = (
            await step(
                "load_snapshots",
                self._load_snapshot_decision,
                [failure.workflow_id for failure in pending],
            )
            if pending
            else {
                "adopted": [],
                "adopted_count": 0,
                "covered": [],
                "delivered_steer_ids": [],
            }
        )
        adopted_ids = snapshot_decision["adopted"]
        recovery = _Recovery(
            covered=frozenset(snapshot_decision["covered"]),
            delivered_steer_ids=frozenset(snapshot_decision["delivered_steer_ids"]),
            adopted=bool(adopted_ids),
        )
        # Payloads stay outside the recorded step log. Replay control flow comes
        # only from the recorded decision above, so a replay after save_session
        # deleted the rows still runs the same steps. Missing history content is
        # safe here because model and tool work is already recorded separately.
        try:
            raw_snapshots = await self._store.get_run_snapshots(adopted_ids) if adopted_ids else {}
        except Exception:
            logger.warning(
                "adopted run snapshot payload read failed; continuing with recorded decision",
                exc_info=True,
            )
            raw_snapshots = {}

        adopted_snapshots: list[_Snapshot] = []
        for workflow_id in adopted_ids:
            payload = raw_snapshots.get(workflow_id)
            if payload is None:
                logger.warning(
                    "adopted run snapshot payload is missing; continuing with recorded decision",
                    workflow_id=workflow_id,
                )
                continue
            try:
                snapshot = _snapshot_from_payload(payload)
                if workflow_id not in snapshot.covers:
                    raise ValueError("snapshot does not cover its owning workflow")
            except Exception:
                logger.warning(
                    "adopted run snapshot payload is invalid; continuing with recorded decision",
                    workflow_id=workflow_id,
                    exc_info=True,
                )
                continue
            adopted_snapshots.append(snapshot)

        adopted_messages = [
            message for snapshot in adopted_snapshots for message in snapshot.messages
        ]
        saved_budget = max(
            0, self._runtime.max_history_messages - snapshot_decision["adopted_count"]
        )
        trimmed_saved = _trim_history(history or [], saved_budget)
        history = [*trimmed_saved, *adopted_messages] or None
        # pydantic-ai merges back-to-back requests in the history it loads, so
        # the save is what is new, not an index into all_messages().
        unsaved = list(adopted_messages)
        state["partial"] = list(history or [])
        state["covers"] = [run_workflow_id(key, event.event_id)]
        seq = iter(range(1_000_000))  # deterministic step names across runs
        prompt, rendered_bare = await self._build_prompt(
            key,
            event,
            seq,
            roster,
            pending,
            state,
            recovery=recovery,
            history_empty=history_empty,
        )
        state["covers"].extend(rendered_bare)
        followups = 0
        output: str | None = None
        while True:
            output, leftovers, messages, new = await self._run_once(
                key, prompt, history, progress, seq, state, roster, selected_model
            )
            unsaved += new
            if output is not None:
                if state["dead"]:
                    raise asyncio.CancelledError
                state["answering"] = True
                await step(f"post_answer:{followups}", progress.post, output)
                state["answered"] = True
                state["spoke"] = True
            # Run-end drain: a steer whose ledger insert raced this run's
            # completion still gets absorbed as a follow-up run. Drain only
            # with follow-up budget left — claiming mail this run cannot
            # process would consume it into the void; left unclaimed, the
            # reconciler hands it to a fresh run instead.
            if followups < self._runtime.max_followup_runs:
                if state["dead"]:
                    raise asyncio.CancelledError
                drained = await self._claim_mail(key, f"drain_mailbox:{followups}")
                await self._extend_roster(
                    roster, [m["user"] for m in drained], [m["text"] for m in drained], seq
                )
                leftovers += [self._render(m, roster, state) for m in drained]
            # Leftovers a run ended holding (an enqueue that raced its end) are
            # steers *already claimed* from the ledger — nothing else can
            # recover them, so they always get a follow-up run: the cap bounds
            # draining new mail, never delivery of accepted mail.
            if leftovers:
                followups += 1
                prompt = prompt_content(leftovers)
                history = messages
                state["answering"] = False
                continue
            break
        # Trimming only bounds history in memory on load. Stored rows are never
        # rewritten, so the full transcript remains and every load re-trims it.
        delta = _dump_messages(unsaved)
        # Consume exactly the pending entries this prompt carried — entry-level,
        # so a record merged mid-run (a sweep announcing another dead workflow)
        # survives for the next run. A run that fails here leaves them all.
        # This run's own id rides along: a hand-resumed MAX_RECOVERY workflow
        # replays a load_session snapshot that predates its own sweep-written
        # entry, and succeeding must not leave that stale record behind.
        consumed = (
            PendingFailure.CONTEXT_KEY,
            [
                *(failure.workflow_id for failure in pending),
                run_workflow_id(key, event.event_id),
            ],
        )
        if state["dead"]:
            raise asyncio.CancelledError
        await step("save_session", self._save_session, key, event.event_id, delta, roster, consumed)
        # After save_session the run has succeeded, so nothing past it may
        # fail the workflow (failure handling would snapshot already-saved
        # messages as duplicate history) — the usage write is best-effort by
        # contract; convergence/replay reasoning lives on _record_usage.
        await self._record_usage("record_usage", messages, key, event, state)
        return output or ""

    async def _load_snapshot_decision(self, workflow_ids: list[str]) -> _SnapshotDecision:
        metadata = await self._store.list_run_snapshots(workflow_ids)
        listed_ids = [item["workflow_id"] for item in metadata]
        try:
            raw_snapshots = await self._store.get_run_snapshots(listed_ids)
        except Exception:
            logger.warning(
                "run snapshot payload read failed; falling back to pending rows",
                exc_info=True,
            )
            raw_snapshots = {}

        requested_ids = set(workflow_ids)
        created_at = {
            item["workflow_id"]: item["created_at"]
            for item in metadata
            if item["workflow_id"] in requested_ids
        }
        snapshots: dict[str, _Snapshot] = {}
        for workflow_id in created_at:
            payload = raw_snapshots.get(workflow_id)
            if payload is None:
                logger.warning(
                    "run snapshot payload is missing; falling back to pending rows",
                    workflow_id=workflow_id,
                )
                continue
            try:
                snapshot = _snapshot_from_payload(payload)
                if workflow_id not in snapshot.covers:
                    raise ValueError("snapshot does not cover its owning workflow")
            except Exception:
                logger.warning(
                    "run snapshot is invalid; falling back to pending rows",
                    workflow_id=workflow_id,
                    exc_info=True,
                )
                continue
            snapshots[workflow_id] = snapshot

        ordered_snapshots = sorted(snapshots.items(), key=lambda item: created_at[item[0]])
        adopted_reversed: list[tuple[str, _Snapshot]] = []
        adopted_count = 0
        for item in reversed(ordered_snapshots):
            snapshot = item[1]
            if (
                adopted_reversed
                and adopted_count + snapshot.stored_message_count
                > self._runtime.max_history_messages
            ):
                break
            adopted_reversed.append(item)
            adopted_count += snapshot.stored_message_count
        adopted = list(reversed(adopted_reversed))
        return {
            "adopted": [workflow_id for workflow_id, _ in adopted],
            "adopted_count": adopted_count,
            "covered": sorted(
                {workflow_id for _, snapshot in adopted for workflow_id in snapshot.covers}
            ),
            "delivered_steer_ids": sorted(
                {mail_id for _, snapshot in adopted for mail_id in snapshot.delivered_steer_ids}
            ),
        }

    async def _save_session(
        self,
        key: SessionKey,
        run_id: str,
        messages: list[dict[str, Any]],
        roster: dict[str, str],
        clear_context_entries: tuple[str, list[str]] | None = None,
    ) -> None:
        await self._store.save_session_run(
            Session(
                key=key,
                state=SessionState.IDLE,
                roster=roster,
                last_event_id=run_id,
            ),
            run_id=run_id,
            messages=messages,
            clear_context_entries=clear_context_entries,
        )

    async def _record_usage(
        self,
        step_name: str,
        messages: Iterable[ModelMessage],
        key: SessionKey,
        event: Event,
        state: dict[str, Any],
    ) -> None:
        """Persist usage without masking the run outcome.

        Writes converge on (workflow_id, kind, resource). The run_id filter
        excludes history already charged to other runs.
        """
        try:
            rows = usage_rows_from_responses(
                (m for m in messages if m.run_id in state["run_ids"]),
                key=key,
                workflow_id=run_workflow_id(key, event.event_id),
            )
            if rows:
                await step(step_name, self._store.record_usage, rows)
                _log_usage_recorded(key, event, rows)
        except Exception:
            logger.warning(
                "usage recording failed; continuing",
                step=step_name,
                session=str(key),
                event_id=event.event_id,
                exc_info=True,
            )

    async def _stop_body(
        self,
        body: asyncio.Future[str],
        key: SessionKey,
        event: Event,
        state: dict[str, Any],
    ) -> None:
        """Cancel the run body; abandon it as a logged zombie if cancellation hangs."""
        body.cancel()
        done, _ = await asyncio.wait({body}, timeout=_CANCEL_GRACE_SECONDS)
        if not done:
            state["dead"] = True
            logger.error(
                "run cancellation hung; task abandoned as a zombie",
                session=str(key),
                event_id=event.event_id,
            )

    async def _persist_failure(
        self,
        key: SessionKey,
        event: Event,
        reason: str,
        state: dict[str, Any],
    ) -> None:
        """Record the dead run for the next run's prompt.

        A usable partial transcript is quarantined first. The pending record
        remains the durable pointer and the fallback when capture is empty,
        or persistence fails.

        Raises on a failed pending write: swallowing it would end the workflow in
        SUCCESS with the trigger lost for good. Escaping drive() lands the
        workflow in ERROR, where the failed-run sweep durably retries both
        the pending record and the user notice.
        """
        delta = [message for message in state["partial"] if message.run_id in state["run_ids"]]
        await self._record_usage("record_usage:failed", delta, key, event, state)
        if any(isinstance(message, ModelResponse) for message in delta):
            try:
                workflow_id = run_workflow_id(key, event.event_id)
                pending_lines = {_line(content) for content in state["pending"]}
                delivered_steer_ids = sorted(
                    mail_id
                    for mail_id, line in state["enqueued_steers"].items()
                    if line not in pending_lines
                )
                payload = {
                    "messages": _dump_messages(delta),
                    "covers": list(state["covers"]),
                    "delivered_steer_ids": delivered_steer_ids,
                }
                await step(
                    "persist_snapshot",
                    self._store.save_run_snapshot,
                    workflow_id,
                    key,
                    payload,
                )
            except Exception:
                logger.exception(
                    "run snapshot persist failed; falling back to pending rows",
                    session=str(key),
                    event_id=event.event_id,
                )
        try:
            await step("persist_failure", self._persist_failure_once, key, event.event_id, reason)
        except Exception:
            logger.exception(
                "pending-failure persist failed; failing the workflow so the sweep retries",
                session=str(key),
                event_id=event.event_id,
            )
            raise

    async def _persist_failure_once(self, key: SessionKey, event_id: str, reason: str) -> None:
        failure = PendingFailure(
            workflow_id=run_workflow_id(key, event_id),
            event_id=event_id,
            reason=reason,
            failed_at=datetime.now(UTC).isoformat(),
        )
        await self._store.ensure_session(key)
        await self._store.merge_context_key(key, PendingFailure.CONTEXT_KEY, failure.to_patch())

    async def _report_failure(
        self, progress: ProgressReporter, state: dict[str, Any], *, answered: bool, text: str
    ) -> None:
        """Post a failure notice unless an answer was delivered. Never raises.

        A run for a message that Hondo only heard by listening posts nothing
        until Hondo has spoken in the thread: nobody asked it.
        """
        if answered or (state.get("unjoined") and not state["spoke"]):
            return
        try:
            await step("notify_failure", progress.post_notice, text)
        except Exception:
            logger.warning("failure notice post failed", exc_info=True)

    async def _run_once(
        self,
        key: SessionKey,
        prompt: str | Sequence[UserContent] | None,
        history: list[ModelMessage] | None,
        progress: ProgressReporter,
        seq: Any,
        state: dict[str, Any],
        roster: dict[str, str],
        selected_model: RunModel | None,
    ) -> tuple[str | None, list[list[UserContent]], list[ModelMessage], list[ModelMessage]]:
        """One agent run. The output is None when the model called no_reply."""
        run_id = await step(f"agent_run_id:{next(seq)}", _new_agent_run_id)
        async with self._agent.iter(
            prompt,
            output_type=REPLY_OUTPUT,
            run_id=run_id,
            message_history=history,
            model=selected_model.model_id if selected_model else None,
            deps=selected_model,
        ) as run:
            state["run_ids"].add(run.run_id)

            def refresh_partial() -> None:
                state["partial"] = list(run.all_messages())
                state["pending"] = _pending_contents(run.pending_messages)

            # Drive via next() rather than bare `async for`: only hooked iteration
            # fires after_node_run, which delivers 'when_idle' steers and end-of-run
            # 'asap' redirects (bare iteration raises UndrainedPendingMessagesError).
            node = run.next_node
            first_request = True
            stashed_text: str | None = None
            try:
                while not Agent.is_end_node(node):
                    if state["dead"]:
                        # The runtime already decided this run is dead; stop executing steps.
                        raise asyncio.CancelledError
                    if Agent.is_call_tools_node(node):
                        # The model's narration alongside tool calls ("on it, …",
                        # "got it, …" after a steer) becomes a real message. A text-only
                        # response is stashed until the next node reveals whether a
                        # redirect resumed the run or post_answer owns the final answer.
                        # Best-effort: narration must never fail the run.
                        text = "".join(
                            part.content
                            for part in node.model_response.parts
                            if isinstance(part, TextPart)
                        ).strip()
                        names = [
                            part.tool_name
                            for part in node.model_response.parts
                            if isinstance(part, ToolCallPart)
                        ]
                        calls = len(names)
                        state["tool_calls"] += sum(name != NO_REPLY for name in names)
                        if calls and text and NO_REPLY not in names and not state["dead"]:
                            try:
                                await step(f"narrate:{next(seq)}", progress.post, text)
                                state["spoke"] = True
                            except Exception:
                                logger.warning("interim narration post failed", exc_info=True)
                        elif not calls:
                            stashed_text = text
                    if (
                        Agent.is_model_request_node(node)
                        and not first_request
                        and not state["dead"]
                    ):
                        # Steer injection point: claim ledger mail before each model
                        # request. The claim is a recorded step, so replay after a
                        # crash injects the same steers at the same point.
                        steers = await self._claim_mail(key, f"claim_steers:{next(seq)}")
                        if steers and not state["dead"]:
                            await self._extend_roster(
                                roster,
                                [s["user"] for s in steers],
                                [s["text"] for s in steers],
                                seq,
                            )
                            if not state["dead"]:
                                for steer in steers:
                                    content = self._render(steer, roster, state)
                                    state["enqueued_steers"][steer["id"]] = _line(content)
                                    run.enqueue(*content, priority=steer["priority"])
                    first_request = first_request and not Agent.is_model_request_node(node)
                    node = await run.next(node)
                    state["progressed_at"] = time.monotonic()
                    refresh_partial()
                    if stashed_text is not None:
                        if not Agent.is_end_node(node) and stashed_text and not state["dead"]:
                            try:
                                await step(f"narrate:{next(seq)}", progress.post, stashed_text)
                                state["spoke"] = True
                            except Exception:
                                logger.warning("interim narration post failed", exc_info=True)
                        stashed_text = None
            except BaseException:
                # A raising tool node can append a partial return before raising.
                # Refresh here instead of relying on all_messages() being a live alias.
                refresh_partial()
                raise
            # Flush enqueue callbacks scheduled via call_soon_threadsafe so a steer
            # that raced the run's end lands in pending_messages before we read it.
            await asyncio.sleep(0)
            refresh_partial()
            leftovers = state["pending"]
            result = run.result
            assert result is not None, "the node loop only exits on End"
            if isinstance(result.output, NoReply):
                logger.info("no reply", session=str(key), reason=result.output.reason)
                return None, leftovers, result.all_messages(), result.new_messages()
            return str(result.output), leftovers, result.all_messages(), result.new_messages()

    async def _build_prompt(
        self,
        key: SessionKey,
        event: Event,
        seq: Any,
        roster: dict[str, str],
        pending: list[PendingFailure],
        state: dict[str, Any],
        *,
        recovery: _Recovery,
        history_empty: bool,
    ) -> tuple[str | list[UserContent], list[str]]:
        """Render the run's opening prompt as a plain transcript.

        Every message — mention-triggered, DM, or ambient — is the same
        "[time] Name: text" line: the model reads addressing from the text
        itself (its own @name present or not), the way a person would. The
        send-time stamp is the model's only clock: it lives in the user
        message — never in instructions, where a per-run timestamp would
        invalidate the prompt-cache prefix — and comes from event data, not
        wall clock, so crash-recovery replay rebuilds the identical prompt.
        """
        # Mail carrying this very event (a fresh run for a stranded steer) is covered
        # by the event text below; claim it without duplicating.
        claimed = [
            m
            for m in await self._claim_mail(key, "claim_mailbox")
            if m["event_id"] != event.event_id
        ]
        pending_rows = {
            failure.workflow_id: await step(
                f"load_pending:{failure.workflow_id}", self._load_pending_rows, failure
            )
            for failure in pending
        }
        backfill: list[dict[str, Any]] = []
        # Root triggers backfill too: Slack can deliver a root event after
        # newer replies, and either way a history-less thread reconstructs.
        if history_empty and not recovery.adopted and not event.is_dm:
            backfill = await step("thread_backfill", self._backfill, key, event) or []
        # Each Slack message renders once, whichever sources carry it. The ts
        # is the dedupe key: unique per channel message, present in every
        # source (backfill rows have no event id). Pending rows enter `seen`
        # before backfill so covered triggers cannot leak through that route.
        seen = (
            {event.ts}
            | {m["ts"] for m in claimed}
            | {row["ts"] for rows in pending_rows.values() for row in rows}
        )
        backfill = [m for m in backfill if m["ts"] not in seen]
        rendered_seen = {event.ts} | {m["ts"] for m in claimed}
        recovered: list[dict[str, Any]] = []
        rendered_bare: list[str] = []
        for failure in pending:
            rows = pending_rows[failure.workflow_id]
            if failure.workflow_id not in recovery.covered:
                if rows:
                    rendered_bare.append(failure.workflow_id)
                for row in rows:
                    if row["ts"] not in rendered_seen:
                        rendered_seen.add(row["ts"])
                        recovered.append(row)
            else:
                for row in rows:
                    mail_id = row["mail_id"]
                    if mail_id is None:
                        # The trigger is inside the snapshot by definition (it
                        # was the run's prompt). Its ts still joins the dedupe
                        # set: a steer redispatched as its own run appears both
                        # as that run's trigger and as its claimed mail — the
                        # mail row must not re-render the same Slack message.
                        rendered_seen.add(row["ts"])
                    elif (
                        mail_id not in recovery.delivered_steer_ids
                        and row["ts"] not in rendered_seen
                    ):
                        rendered_seen.add(row["ts"])
                        recovered.append(row)
        await self._extend_roster(
            roster,
            [event.user, event.bot_user, *(m["user"] for m in claimed + backfill + recovered)],
            [event.text, *(m["text"] for m in claimed + backfill + recovered)],
            seq,
        )
        parts: list[list[UserContent]] = []
        # All sources are one stream in Slack order: their messages interleave
        # (a dead run's failure notice follows its trigger, a burst splits into
        # trigger and mail). It splits at the oldest message this run answers:
        # older ones are context, then the runtime notes, then the rest.
        # Backfill implies no adopted snapshot, so nothing already in history
        # is reordered — this block is rendered once and only ever appended to.
        trigger = {
            "user": event.user,
            "text": event.text,
            "ts": event.ts,
            "files": [asdict(file) for file in event.files],
        }
        transcript = sorted([*backfill, *recovered, *claimed, trigger], key=lambda m: m["ts"])
        first_unanswered = min(m["ts"] for m in [*claimed, trigger])
        earlier = [m for m in transcript if m["ts"] < first_unanswered]
        if earlier:
            block: list[UserContent] = ["Earlier messages in this thread:"] if backfill else []
            for content in (render_content(m, roster, self._style) for m in earlier):
                for item in content:
                    if isinstance(item, str) and block and isinstance(block[-1], str):
                        block[-1] = f"{block[-1]}\n{item}"
                    else:
                        block.append(item)
            parts.append(block)
        if pending:
            # Bare input is rebuilt; covered input stays in adopted history.
            # The note is visibly runtime-authored and never carries a cause.
            notes: list[str] = []
            if any(failure.workflow_id in recovery.covered for failure in pending):
                notes.append(
                    "[runtime] A previous run in this thread was interrupted; "
                    "the conversation above includes its unfinished work."
                )
            if any(failure.workflow_id not in recovery.covered for failure in pending):
                notes.append(
                    "[runtime] A previous run in this thread failed. Its partial transcript "
                    "is unavailable, but external actions may already have completed. "
                    "Do not repeat actions from those messages without first checking their "
                    "outcome. If you cannot verify the outcome, ask a person to check."
                )
            # One block keeps runtime text separate from the user turn below.
            parts.append(["\n".join(notes)])
        parts.extend(
            self._render(m, roster, state) for m in transcript if m["ts"] >= first_unanswered
        )
        return prompt_content(parts), rendered_bare

    def _render(
        self, message: dict[str, Any], roster: dict[str, str], state: dict[str, Any]
    ) -> list[UserContent]:
        """A message for the model, with the listening guidance while it applies:
        the message does not mention Hondo, and Hondo has not spoken in the
        thread."""
        line, *images = render_content(message, roster, self._style)
        guidance = state.get("guidance")
        bot = state.get("bot_user")
        if (
            guidance is None
            or state["spoke"]
            or (bot and self._style.mention(bot) in message["text"])
        ):
            return [line, *images]
        return [f"{line}\n{guidance}", *images]

    async def _extend_roster(
        self,
        roster: dict[str, str],
        authors: list[str | None],
        texts: list[str],
        seq: Any,
    ) -> None:
        """Resolve unseen authors and mentioned ids into the roster, durably.

        Name lookups are external I/O, so they run as a recorded step: replay
        reuses the recorded map instead of re-calling Slack. The step input is
        a sorted list for a deterministic step signature. Unresolvable ids
        (deleted users, bot_id authors) are simply absent — they render in raw
        <@id> form and the lookup_user tool is the model's escape hatch.
        """
        ids = {author for author in authors if author} | mention_ids(texts)
        missing = sorted(ids - roster.keys())
        if missing:
            roster.update(await step(f"resolve_names:{next(seq)}", self._resolve_names, missing))

    async def _resolve_names(self, user_ids: list[str]) -> dict[str, str]:
        return await self._directory.resolve_many(user_ids)

    async def _claim_mail(self, key: SessionKey, name: str) -> list[dict[str, Any]]:
        """Atomically consume all unconsumed ledger mail, as the named step.

        The claimed mail is the step's recorded output, so a crash-recovery
        replay injects the same steers at the same point. The claim's own
        commit and DBOS's record of the step are two transactions, though — a
        kill between them replays the step against an already-consumed ledger.
        The claim token (workflow id + step name, deterministic across replay)
        closes that gap: re-execution re-reads what it already claimed. The
        "heard you, folding it in" acknowledgment is the model's own next
        narration line ("got it, …", mandated by the writing rules) — no
        reactions.
        """
        return await step(name, self._claim_mail_once, key, name)

    async def _claim_mail_once(self, key: SessionKey, name: str) -> list[dict[str, Any]]:
        mail = await self._store.claim_mail(key, token=claim_token(name))
        return [
            {
                "id": m.id,
                "event_id": m.event_id,
                "user": m.user,
                "text": m.text,
                "files": [asdict(file) for file in m.files],
                "ts": m.ts,
                "priority": m.priority.value,
            }
            for m in mail
        ]

    async def _load_session(self, key: SessionKey) -> dict[str, Any] | None:
        session = await self._store.get_session(key)
        if session is None:
            return None
        # The +1 preserves _trim_history's over-limit branch: it only ever
        # inspects the last `limit` messages, so this tail trims exactly like a
        # full fetch would — without reading (and recording as the step output
        # in the DBOS log) the whole transcript every run.
        return {
            **session.model_dump(mode="json"),
            "messages": await self._store.get_session_messages(
                key, limit=self._runtime.max_history_messages + 1
            ),
        }

    async def _load_event_mail(self, event_id: str) -> dict[str, Any] | None:
        mail = await self._store.mail_for_event(event_id)
        return mail.model_dump(mode="json") if mail is not None else None

    async def _load_pending_rows(self, failure: PendingFailure) -> list[dict[str, Any]]:
        """A dead run's model-visible input, rebuilt from durable tables: its
        trigger from the events log, its claimed steers from the mail ledger.
        Raw rows in Slack order — rendering happens outside this step, after
        names resolve. Mail ids let snapshot recovery distinguish delivered
        steers from ones claimed just before death."""
        rows: list[dict[str, Any]] = []
        trigger = await self._store.get_event(failure.event_id)
        if trigger is not None:
            rows.append(
                {
                    "user": trigger.user or "",
                    "text": trigger.text,
                    "ts": trigger.ts,
                    "files": [asdict(file) for file in trigger.files],
                    "mail_id": None,
                }
            )
        for mail in await self._store.mail_claimed_by(failure.workflow_id):
            rows.append(
                {
                    "user": mail.user or "",
                    "text": mail.text,
                    "ts": mail.ts,
                    "files": [asdict(file) for file in mail.files],
                    "mail_id": mail.id,
                }
            )
        return sorted(rows, key=lambda row: row["ts"])

    async def _backfill(self, key: SessionKey, event: Event) -> list[dict[str, Any]] | None:
        """First engagement mid-thread: fetch prior messages to inject as speaker
        turns inside the user prompt (never as fabricated model-turn history).
        Returns raw {user, ts, text, files} rows — rendering happens outside this step,
        after names resolve. Best-effort."""
        try:
            messages = await self._client.fetch_thread(
                channel=key.channel,
                root=key.thread_root,
                limit=self._runtime.backfill_limit,
            )
        except Exception:
            logger.warning("thread backfill failed", session=str(key), exc_info=True)
            return None
        rows = [
            {
                "user": message.author,
                "ts": message.ref,
                "text": message.text,
                "files": [asdict(file) for file in message.files],
            }
            for message in messages[: self._runtime.backfill_limit]
            # The triggering message is the prompt itself.
            if message.ref != event.ts
        ]
        # Slack order, not delivery order — a delayed root must not scramble it.
        rows.sort(key=lambda row: row["ts"])
        return rows or None
