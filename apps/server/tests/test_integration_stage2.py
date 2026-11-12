"""Done-when integration tests: real Postgres, real DBOS, full serve()-style wiring.

Each test assembles gateway-less ingress (events appended straight to the store
and dispatched), the DBOS queue + drive-session workflow, and the steer path —
everything serve() wires except Socket Mode. DBOS is a per-process singleton, so
each test launches and destroys it; crash recovery is exercised in a separate
child process (see test_crash_recovery + dbos_crash_child.py).
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from dbos import DBOS, Queue
from pydantic_ai import Agent
from pydantic_ai.durable_exec.dbos import DBOSDurability
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelResolutionContext
from pydantic_ai.models.function import AgentInfo, FunctionModel

from hondo.agent.conversation import ConversationCapability
from hondo.agent.tool_boundary import ToolBoundary
from hondo.chat import ChatMessage
from hondo.config import EngagePolicy, RuntimeConfig
from hondo.models import Event, EventKind, SessionKey, SteerPriority
from hondo.runtime.dispatcher import Dispatcher
from hondo.runtime.durable import DBOSBackend, run_workflow_id, summary_workflow_id
from hondo.runtime.model_selection import RunModel
from hondo.runtime.runner import SessionRunner
from hondo.runtime.screen import ChannelScreen, screen_agent
from hondo.runtime.serve import cancel_stale_summaries, summary_starter
from hondo.runtime.summarizer import ThreadSummarizer, summary_agent
from hondo.slack import SlackStyle
from hondo.store.postgres import PostgresStore
from hondo.users import UserDirectory
from tests.fakes import FakeSlack

FAST = RuntimeConfig(queue_polling_interval=0.1, debounce_period=0)


class Runtime:
    """One process: DBOS + queue + drive-session workflow + dispatcher over a store."""

    def __init__(
        self,
        store: PostgresStore,
        slack: FakeSlack,
        model: Model,
        *,
        runtime: RuntimeConfig = FAST,
        summary_model: Model | None = None,
        screen_model: Model | None = None,
    ) -> None:
        self._client = slack
        self._style = SlackStyle()
        self._directory = UserDirectory(slack)
        self.conversation = ConversationCapability(
            client=slack,
            directory=self._directory,
            style=self._style,
        )
        self.agent: Agent[None, str] = Agent(
            model,
            capabilities=[
                ToolBoundary(),
                DBOSDurability(name="hondo_agent", parallel_execution_mode="sequential"),
                self.conversation,
            ],
        )
        self._store = store
        self._config = runtime
        self.screen: ChannelScreen | None = None
        if screen_model is not None:

            async def resolve_screen(ctx: ModelResolutionContext[RunModel], model_id: str) -> Model:
                return screen_model

            async def select_screen_model(team: str) -> RunModel:
                return RunModel("test")

            self.screen = ChannelScreen(
                screen_agent(resolve_screen),
                store=store,
                client=slack,
                style=self._style,
                select_model=select_screen_model,
            )
        self._summarizer: ThreadSummarizer | None = None
        if summary_model is not None:

            async def resolve(ctx: ModelResolutionContext[RunModel], model_id: str) -> Model:
                return summary_model

            async def select_model(team: str) -> RunModel:
                return RunModel("test")

            self._summarizer = ThreadSummarizer(
                summary_agent(resolve),
                store=store,
                summaries=store,
                client=slack,
                style=self._style,
                directory=self._directory,
                select_model=select_model,
                timeout=runtime.run_timeout,
            )

    async def start(self, pg_dsn: str, *, application_version: str | None = None) -> None:
        config: dict[str, Any] = {
            "name": "hondo-test",
            "system_database_url": pg_dsn,
            "run_admin_server": False,
            "dbos_system_schema": "hondo_dbos",
            "log_level": "WARNING",
            "notification_listener_polling_interval_sec": 0.1,
        }
        if application_version is not None:
            config["application_version"] = application_version
        DBOS(config=config)  # type: ignore[arg-type]
        queue = Queue(
            "hondo_sessions",
            concurrency=1,
            partition_queue=True,
            polling_interval_sec=self._config.queue_polling_interval,
        )
        runner = SessionRunner(
            self.agent,
            store=self._store,
            client=self._client,
            style=self._style,
            directory=self._directory,
            runtime=self._config,
            start_summary=(
                summary_starter(self._summarizer, 3) if self._summarizer is not None else None
            ),
            screen=self.screen.decide if self.screen is not None else None,
        )

        @DBOS.workflow(name="hondo.drive_session", max_recovery_attempts=3)
        async def drive_session(key_json: dict[str, Any], event_json: dict[str, Any]) -> str:
            return await runner.drive(key_json, event_json)

        self.backend = DBOSBackend(queue, drive_session)
        self.dispatcher = Dispatcher(
            self._store,
            EngagePolicy(),
            self.backend,
            client=self._client,
        )
        DBOS.launch()

    async def stop(self) -> None:
        DBOS.destroy(destroy_registry=True)

    async def ingress(self, event: Event) -> None:
        await self._store.append_event(event)
        await self.dispatcher.dispatch(event)


def unique() -> str:
    return uuid.uuid4().hex[:8]


def mention(event_id: str, ts: str, text: str, *, thread_ts: str | None = None) -> Event:
    return Event(
        event_id=event_id,
        kind=EventKind.APP_MENTION,
        team="T01",
        channel="C01",
        ts=ts,
        user="U01",
        thread_ts=thread_ts,
        text=text,
        mentions_bot=True,
    )


async def eventually(check: Callable[[], Awaitable[bool]], *, timeout: float = 15.0) -> None:
    async def poll() -> None:
        while not await check():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=timeout)


async def _true(value: bool) -> bool:  # tiny adapter: eventually() wants an awaitable
    return value


def mail_consumed(store: PostgresStore, event_id: str) -> Awaitable[bool]:
    async def check() -> bool:
        mail = await store.mail_for_event(event_id)
        return mail is not None and mail.consumed

    return check()


def user_prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    ]


def has_tool_return(messages: list[ModelMessage]) -> bool:
    return any(
        isinstance(part, ToolReturnPart)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
    )


async def test_steer_mid_run_visibly_changes_the_answer(store: PostgresStore, pg_dsn: str) -> None:
    slack = FakeSlack()
    flags = {"tool_may_finish": False}

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not has_tool_return(messages):
            return ModelResponse(
                parts=[TextPart("On it — checking."), ToolCallPart(tool_name="lookup", args={})]
            )
        if any("purple" in p for p in user_prompts(messages)):
            return ModelResponse(parts=[TextPart("Making it purple instead.")])
        return ModelResponse(parts=[TextPart("Making it blue.")])

    runtime = Runtime(store, slack, FunctionModel(model_fn))

    @runtime.agent.tool_plain
    async def lookup() -> str:
        # Hold the run open at the tool step so the steer lands mid-run; the next
        # model request's ledger claim then injects it and redirects the answer.
        while not flags["tool_may_finish"]:
            await asyncio.sleep(0.02)
        return "resource42"

    uid = unique()
    root = f"1700000000.{uid[:6]}"
    await runtime.start(pg_dsn)
    try:
        await runtime.ingress(mention(f"Ev1-{uid}", root, "<@B01> paint the widget"))
        await eventually(lambda: _true(len(slack.posted) >= 1))  # narration up = run live

        steer = mention(
            f"Ev2-{uid}", "1700000000.000200", "<@B01> no wait, purple please", thread_ts=root
        )
        # ingress() returns only after the steer is in the ledger, targeted at
        # the live workflow; only then may the tool finish.
        await runtime.ingress(steer)
        flags["tool_may_finish"] = True

        # Append-only: narration then the steered answer, each its own message;
        # nothing is ever edited or deleted.
        await eventually(
            lambda: _true(
                len(slack.posted) == 2 and slack.posted[-1]["text"] == "Making it purple instead."
            )
        )
        await eventually(lambda: mail_consumed(store, steer.event_id))
        assert slack.posted[0]["text"] == "On it — checking."
        assert slack.updates == []
        # UX v2: the heard-you signal is the model's own narration; no reactions.
        assert slack.reactions == []
        # posted == 2 (narration + answer) also proves the steer never forked
        # a second run — a fork would have posted its own narration.
    finally:
        await runtime.stop()


async def test_hammering_a_thread_never_duplicates_runs(store: PostgresStore, pg_dsn: str) -> None:
    slack = FakeSlack()
    active = 0
    max_active = 0

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.05)
        active -= 1
        return ModelResponse(parts=[TextPart(f"ack {len(user_prompts(messages))}")])

    runtime = Runtime(store, slack, FunctionModel(model_fn))
    uid = unique()
    root = f"1700000000.{uid[:6]}"
    key = SessionKey(team="T01", channel="C01", thread_root=root)
    await runtime.start(pg_dsn)
    try:
        await runtime.ingress(mention(f"Ev1-{uid}", root, "<@B01> kick it off"))
        for n in range(2, 8):
            await runtime.ingress(
                mention(
                    f"Ev{n}-{uid}", f"1700000000.000{n}00", f"<@B01> and also #{n}", thread_ts=root
                )
            )

        async def quiet() -> bool:
            await runtime.dispatcher.reconcile(0)
            if await runtime.backend.live_workflow_id(key) is not None:
                return False
            return not await store.unconsumed_mail(key)

        await eventually(quiet, timeout=30)
        assert max_active == 1, "two model calls overlapped: duplicate live runs in one thread"
    finally:
        await runtime.stop()


async def test_burst_in_an_engaged_thread_gets_one_answer(
    store: PostgresStore, pg_dsn: str
) -> None:
    """Messages sent close together get one answer that reads them in thread order."""
    slack = FakeSlack()
    prompts: list[str] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompts.append(user_prompts(messages)[-1])
        return ModelResponse(parts=[TextPart(f"answer {len(prompts)}")])

    runtime = Runtime(
        store,
        slack,
        FunctionModel(model_fn),
        runtime=RuntimeConfig(queue_polling_interval=0.1, debounce_period=1.0),
    )
    uid = unique()
    root = f"1700000000.{uid[:6]}"
    key = SessionKey(team="T01", channel="C01", thread_root=root)

    def reply(n: int, text: str) -> Event:
        return Event(
            event_id=f"Ev{n}-{uid}",
            kind=EventKind.MESSAGE,
            team="T01",
            channel="C01",
            ts=f"1700000001.00{n}000",
            user="U01",
            thread_ts=root,
            text=text,
        )

    await runtime.start(pg_dsn)
    try:
        await runtime.ingress(mention(f"Ev1-{uid}", root, "<@B01> watch the deploy"))
        first_run = run_workflow_id(key, f"Ev1-{uid}")
        await eventually(lambda: runtime.backend.workflow_finished(first_run))

        # Slack runs these handlers concurrently; the mention comes a moment later.
        await asyncio.gather(
            runtime.ingress(reply(2, "check it now")),
            runtime.ingress(reply(3, "hello?")),
        )
        await asyncio.sleep(0.3)
        await runtime.ingress(mention(f"Ev4-{uid}", "1700000001.004000", "<@B01>", thread_ts=root))

        await eventually(lambda: _true(len(slack.posted) == 2))
        await asyncio.sleep(1.5)  # time for a split burst to post a third answer
        assert [p["text"] for p in slack.posted] == ["answer 1", "answer 2"]
        assert prompts[1].split("\n\n") == [
            "[2023-11-14 22:13 UTC] jane: check it now",
            "[2023-11-14 22:13 UTC] jane: hello?",
            "[2023-11-14 22:13 UTC] jane: <@B01>",
        ]
    finally:
        await runtime.stop()


async def test_steer_stranded_by_run_end_is_reconciled(store: PostgresStore, pg_dsn: str) -> None:
    """A send that lands after the target's run-end drain must not strand the reply.

    Simulates the residual race directly: the mail targets an already-terminal
    workflow; the reconcile sweep hands it to a fresh run that drains it.
    """
    slack = FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(f"Handled: {user_prompts(messages)[-1]}")])

    runtime = Runtime(store, slack, FunctionModel(model_fn))
    uid = unique()
    root = f"1700000000.{uid[:6]}"
    key = SessionKey(team="T01", channel="C01", thread_root=root)
    await runtime.start(pg_dsn)
    try:
        first = mention(f"Ev1-{uid}", root, "<@B01> kick it off")
        await runtime.ingress(first)
        await eventually(
            lambda: _true(bool(slack.posted) and "kick it off" in slack.posted[-1]["text"])
        )
        # Posting happens mid-workflow (drain + save still follow), so wait for the
        # run to actually reach a terminal status before staging the stranded mail.
        dead_workflow = run_workflow_id(key, first.event_id)
        await eventually(lambda: runtime.backend.workflow_finished(dead_workflow))

        late = mention(
            f"Ev2-{uid}", "1700000000.000900", "<@B01> are you still there?", thread_ts=root
        )
        await store.append_event(late)
        mail = await store.insert_mail(
            key, late, SteerPriority.ASAP, target_workflow_id=dead_workflow
        )
        assert mail is not None  # stranded: the target will never claim it

        await runtime.dispatcher.reconcile(0)
        # U01 resolves to a display name; <@B01> is a bot_id, not a user id,
        # so it stays raw.
        expected = "Handled: [2023-11-14 22:13 UTC] jane: <@B01> are you still there?"
        await eventually(lambda: _true(bool(slack.posted) and slack.posted[-1]["text"] == expected))
        await eventually(lambda: mail_consumed(store, late.event_id))
    finally:
        await runtime.stop()


async def test_failing_run_is_contained_and_thread_stays_usable(
    store: PostgresStore, pg_dsn: str
) -> None:
    """A tool/model failure kills the run, never the session or the server.

    Contract (dogfooding: an MCP tool that 400s forever killed the run and the
    thread just went quiet): the user gets an in-thread system notice (muted
    context block, third person), the workflow completes, and the very next
    mention in the same thread is answered normally — with the dead run's
    trigger and a labeled failure note in its prompt.
    """
    slack = FakeSlack()
    seen_prompts: list[str] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = user_prompts(messages)[-1]
        seen_prompts.append(prompt)
        # Explode only when the *triggering* message (the prompt's last line)
        # asks for it: the retry legitimately re-shows the dead run's trigger.
        if "explode" in prompt.splitlines()[-1]:
            raise RuntimeError("provider exploded")
        return ModelResponse(parts=[TextPart(f"Handled: {prompt}")])

    runtime = Runtime(store, slack, FunctionModel(model_fn))
    uid = unique()
    root = f"1700000000.{uid[:6]}"
    await runtime.start(pg_dsn)
    try:
        bad = mention(f"Ev1-{uid}", root, "<@B01> explode please")
        await runtime.ingress(bad)
        await eventually(
            lambda: _true(bool(slack.posted) and "hit an error" in slack.posted[-1]["text"])
        )
        notice = slack.posted[-1]
        assert notice["blocks"] and notice["blocks"][0]["type"] == "context"
        assert slack.reactions == []

        follow = mention(f"Ev2-{uid}", "1700000000.000900", "<@B01> still alive?", thread_ts=root)
        await runtime.ingress(follow)
        await eventually(
            lambda: _true(bool(slack.posted) and slack.posted[-1]["text"].startswith("Handled:"))
        )
        # Thread continuity across the dead run: its trigger came back as a
        # transcript line and the failure as a runtime note, not model text.
        assert "explode please" in seen_prompts[-1]
        assert "[runtime]" in seen_prompts[-1]
        assert "RuntimeError" not in seen_prompts[-1]  # the cause never reaches the model
    finally:
        await runtime.stop()


async def test_uncancellable_run_is_abandoned_and_bot_stays_alive(
    store: PostgresStore, pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A run whose cancellation hangs (observed live: broken MCP session cleanup
    swallowing CancelledError) is abandoned on a deadline — the workflow
    completes with a notice and other threads keep getting answers."""
    import hondo.runtime.runner as runner_mod

    monkeypatch.setattr(runner_mod, "_CANCEL_GRACE_SECONDS", 0.05)
    slack = FakeSlack()
    cleanup_started = asyncio.Event()
    cleanup_blocked = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_done = asyncio.Event()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if "hang" in user_prompts(messages)[-1]:
            cleanup_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleanup_blocked.set()
                while not release_cleanup.is_set():
                    try:
                        await release_cleanup.wait()
                    except asyncio.CancelledError:
                        pass
            finally:
                cleanup_done.set()
            return ModelResponse(parts=[TextPart("zombie speaks")])
        return ModelResponse(parts=[TextPart(f"Handled: {user_prompts(messages)[-1]}")])

    runtime = Runtime(
        store,
        slack,
        FunctionModel(model_fn),
        # The stall watchdog applies to *every* run, including the healthy one
        # this test sends afterwards. A healthy run needs ~0.3s locally, but on
        # a slow CI runner it once took over 3s before its first progress (DBOS
        # queue and steps) and lost the race to the watchdog meant for the
        # hanging one. 10s still proves the hang is stopped on a deadline.
        runtime=RuntimeConfig(queue_polling_interval=0.1, run_timeout=15.0, stall_timeout=10.0),
    )
    uid = unique()
    stuck = mention(f"Ev1-{uid}", f"1700000000.{uid[:6]}", "<@B01> hang forever")
    other = mention(f"Ev2-{uid}", f"1700000001.{uid[:6]}", "<@B01> other thread")
    await runtime.start(pg_dsn)
    try:
        await runtime.ingress(stuck)
        # The run never spoke and stopped making progress, so the stall
        # watchdog stops it with the "isn't responding" notice.
        await eventually(
            lambda: _true(bool(slack.posted) and "isn't responding" in slack.posted[-1]["text"]),
            timeout=30.0,
        )

        await runtime.ingress(other)
        await eventually(
            lambda: _true(bool(slack.posted) and slack.posted[-1]["text"].startswith("Handled:"))
        )
        await eventually(
            lambda: runtime.backend.workflow_finished(
                run_workflow_id(SessionKey.from_event(other), other.event_id)
            ),
            timeout=60.0,
        )
        assert cleanup_started.is_set()
        assert cleanup_blocked.is_set()
        assert not cleanup_done.is_set()
    finally:
        release_cleanup.set()
        if cleanup_started.is_set():
            await asyncio.wait_for(cleanup_done.wait(), timeout=10)
            await eventually(
                lambda: runtime.backend.workflow_finished(
                    run_workflow_id(SessionKey.from_event(stuck), stuck.event_id)
                )
            )
        await runtime.stop()


async def test_silent_hang_is_stopped_by_stall_watchdog(store: PostgresStore, pg_dsn: str) -> None:
    """A run that hangs before saying anything (model unreachable, wedged
    provider) is stopped at stall_timeout with a 'not responding'
    notice — never a silent eternal PENDING, never run_timeout of dead air."""
    slack = FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await asyncio.Event().wait()
        return ModelResponse(parts=[TextPart("too late")])

    runtime = Runtime(
        store,
        slack,
        FunctionModel(model_fn),
        runtime=RuntimeConfig(queue_polling_interval=0.1, run_timeout=30.0, stall_timeout=0.2),
    )
    uid = unique()
    root = f"1700000000.{uid[:6]}"
    await runtime.start(pg_dsn)
    try:
        event = mention(f"Ev1-{uid}", root, "<@B01> take forever")
        await runtime.ingress(event)
        await eventually(
            lambda: _true(bool(slack.posted) and "isn't responding" in slack.posted[-1]["text"])
        )
        notice = slack.posted[-1]
        assert notice["blocks"] and notice["blocks"][0]["type"] == "context"
        assert slack.reactions == []
    finally:
        await runtime.stop()


async def test_run_that_spoke_then_wedged_hits_time_limit(
    store: PostgresStore, pg_dsn: str
) -> None:
    """Once the run has acked, the watchdog stands down — a later wedge (hung
    tool) is stopped by the run_timeout ceiling with the time-limit notice."""
    slack = FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[TextPart("On it — digging."), ToolCallPart(tool_name="dig", args={})]
        )

    runtime = Runtime(
        store,
        slack,
        FunctionModel(model_fn),
        runtime=RuntimeConfig(queue_polling_interval=0.1, run_timeout=0.5, stall_timeout=0.25),
    )

    @runtime.agent.tool_plain
    async def dig() -> str:
        await asyncio.Event().wait()
        return "never"

    uid = unique()
    root = f"1700000000.{uid[:6]}"
    await runtime.start(pg_dsn)
    try:
        event = mention(f"Ev1-{uid}", root, "<@B01> dig in")
        await runtime.ingress(event)
        await eventually(
            lambda: _true(bool(slack.posted) and "time limit" in slack.posted[-1]["text"]),
            timeout=20.0,
        )
        assert slack.posted[0]["text"] == "On it — digging."
    finally:
        await runtime.stop()


async def test_deploy_version_change_unwedges_the_thread(store: PostgresStore, pg_dsn: str) -> None:
    """A run enqueued by version A is invisible work to version B: DBOS never
    dequeues it, yet it looks live — steers would target it forever and fresh
    runs can't start. Boot B must cancel it, owe the thread a notice, and let
    the next mention engage normally."""
    slack = FakeSlack()
    root_ts = "1709990000.000100"

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Back on it.")])

    # Version A: enqueue with a poll interval so long the run never dequeues,
    # then stop — the workflow row stays ENQUEUED, stamped app_version=vA.
    v1 = Runtime(
        store,
        slack,
        FunctionModel(model_fn),
        runtime=RuntimeConfig(queue_polling_interval=60),
    )
    await v1.start(pg_dsn, application_version="vA")
    stranded = mention(f"Ev-{unique()}", root_ts, "<@B01> audit staging")
    try:
        await v1.ingress(stranded)
    finally:
        await v1.stop()

    v2 = Runtime(store, slack, FunctionModel(model_fn))
    await v2.start(pg_dsn, application_version="vB")
    try:
        key = SessionKey.from_event(stranded)
        assert await v2.backend.live_workflow_id(key) is not None  # the wedge

        await v2.dispatcher.sweep_stale_runs()
        await v2.dispatcher.deliver_run_notices()

        assert await v2.backend.live_workflow_id(key) is None
        assert any("updated mid-task" in m["text"] for m in slack.posted)

        # The thread engages fresh on the new version.
        retry = mention(
            f"Ev-{unique()}", "1709990000.000200", "<@B01> try again", thread_ts=root_ts
        )
        await v2.ingress(retry)

        async def answered() -> bool:
            return any(m["text"] == "Back on it." for m in slack.posted)

        await eventually(answered)
    finally:
        await v2.stop()


async def test_a_finished_run_writes_the_thread_summary(store: PostgresStore, pg_dsn: str) -> None:
    slack = FakeSlack()

    async def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Restart payments-worker.")])

    async def summarize(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert "Restart payments-worker." in user_prompts(messages)[0]
        return ModelResponse(parts=[TextPart("Payments timed out. A restart fixed it.")])

    runtime = Runtime(store, slack, FunctionModel(answer), summary_model=FunctionModel(summarize))
    uid = unique()
    root = f"1700000000.{uid[:6]}"
    slack.thread_messages = [ChatMessage(author="U01", text="<@B01> payments time out", ref=root)]
    key = SessionKey(team="T01", channel="C01", thread_root=root)
    await runtime.start(pg_dsn)
    try:
        await runtime.ingress(mention(f"Ev1-{uid}", root, "<@B01> payments time out"))

        async def summarized() -> bool:
            return await store.thread_summary(key) is not None

        await eventually(summarized)
        summary = await store.thread_summary(key)
        assert summary is not None
        assert summary.text == "Payments timed out. A restart fixed it."
        # The summary covers Hondo's answer, the thread's newest message.
        assert summary.covered_ts == slack.posted[-1]["ts"]
        assert await runtime.backend.live_workflow_id(key) is None
    finally:
        await runtime.stop()


async def test_a_summary_that_a_new_version_interrupted_is_cancelled(
    store: PostgresStore, pg_dsn: str
) -> None:
    slack = FakeSlack()
    never = asyncio.Event()

    async def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Restart payments-worker.")])

    async def hang(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await never.wait()
        raise AssertionError("unreachable")

    uid = unique()
    root = f"1700000000.{uid[:6]}"
    slack.thread_messages = [ChatMessage(author="U01", text="<@B01> payments time out", ref=root)]
    trigger = mention(f"Ev1-{uid}", root, "<@B01> payments time out")
    workflow_id = summary_workflow_id(SessionKey.from_event(trigger), trigger.event_id)

    async def status() -> str | None:
        found = await DBOS.list_workflows_async(workflow_ids=[workflow_id], load_input=False)
        return found[0].status if found else None

    v1 = Runtime(store, slack, FunctionModel(answer), summary_model=FunctionModel(hang))
    await v1.start(pg_dsn, application_version="vA")
    try:
        await v1.ingress(trigger)

        async def summarizing() -> bool:
            return await status() == "PENDING"

        await eventually(summarizing)
    finally:
        await v1.stop()

    v2 = Runtime(store, slack, FunctionModel(answer))
    await v2.start(pg_dsn, application_version="vB")
    try:
        assert await status() == "PENDING"  # it never resumes on this version
        await cancel_stale_summaries()
        assert await status() == "CANCELLED"
    finally:
        await v2.stop()


async def test_a_summary_that_hangs_is_stopped_at_the_run_time_limit(
    store: PostgresStore, pg_dsn: str
) -> None:
    slack = FakeSlack()
    never, stopped = asyncio.Event(), asyncio.Event()

    async def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Restart payments-worker.")])

    async def hang(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        try:
            await never.wait()
        except asyncio.CancelledError:
            stopped.set()
            raise
        raise AssertionError("unreachable")

    uid = unique()
    root = f"1700000000.{uid[:6]}"
    slack.thread_messages = [ChatMessage(author="U01", text="<@B01> payments time out", ref=root)]
    trigger = mention(f"Ev1-{uid}", root, "<@B01> payments time out")
    workflow_id = summary_workflow_id(SessionKey.from_event(trigger), trigger.event_id)
    runtime = Runtime(
        store,
        slack,
        FunctionModel(answer),
        runtime=RuntimeConfig(queue_polling_interval=0.1, debounce_period=0, run_timeout=2),
        summary_model=FunctionModel(hang),
    )
    await runtime.start(pg_dsn)
    try:
        await runtime.ingress(trigger)

        # The hung call itself stops, and the workflow ends.
        await asyncio.wait_for(stopped.wait(), timeout=15)

        async def ended() -> bool:
            found = await DBOS.list_workflows_async(workflow_ids=[workflow_id], load_input=False)
            return bool(found) and found[0].status == "SUCCESS"

        await eventually(ended)
        assert await store.thread_summary(SessionKey.from_event(trigger)) is None
    finally:
        never.set()
        await runtime.stop()
