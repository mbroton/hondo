"""Listening channels end to end: real Postgres, real DBOS, the screen as a workflow step."""

from __future__ import annotations

import asyncio
import json

from dbos import DBOS
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from hondo.config import RuntimeConfig
from hondo.models import Event, EventKind, SessionKey
from hondo.runtime.durable import run_workflow_id
from hondo.runtime.gateway import Gateway
from hondo.store.postgres import PostgresStore
from tests.fakes import FakeSlack
from tests.test_integration_stage2 import Runtime, eventually, mention, unique

GOAL = "Answer questions about deploys. Not social chat."


async def listen(store: PostgresStore, channel: str = "C01") -> None:
    await store._connected_pool.execute(
        """
        INSERT INTO workspaces
            (slack_team_id, name, status, bot_token_encrypted, listening_channels)
        VALUES ('T01', 'Acme', 'active', 'ciphertext', $1::jsonb)
        """,
        json.dumps({channel: GOAL}),
    )


def message(event_id: str, ts: str, text: str, *, thread_ts: str | None = None) -> Event:
    return Event(
        event_id=event_id,
        kind=EventKind.MESSAGE,
        team="T01",
        channel="C01",
        ts=ts,
        user="U02",
        thread_ts=thread_ts,
        text=text,
    )


class Screen:
    """The screen model: counts its calls; can wait to be released."""

    def __init__(self, answer: bool | Exception) -> None:
        self.answer = answer
        self.calls = 0
        self.started = asyncio.Event()
        self.release: asyncio.Event | None = None

    async def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls += 1
        self.started.set()
        if self.release is not None:
            await self.release.wait()
        if isinstance(self.answer, Exception):
            raise self.answer
        return ModelResponse(
            parts=[ToolCallPart(info.output_tools[0].name, {"response": self.answer})]
        )


class Answer:
    def __init__(self, text: str = "Redeploy the previous tag.") -> None:
        self.text = text
        self.calls = 0

    async def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        self.calls += 1
        return ModelResponse(parts=[TextPart(self.text)])


async def usage_ids(store: PostgresStore) -> list[str]:
    rows = await store._connected_pool.fetch(
        "SELECT workflow_id FROM usage_events ORDER BY workflow_id"
    )
    return [row["workflow_id"] for row in rows]


async def finished(runtime: Runtime, key: SessionKey, event_id: str) -> None:
    async def done() -> bool:
        return await runtime.backend.workflow_finished(run_workflow_id(key, event_id))

    await eventually(done)


async def test_a_message_for_the_goal_is_answered_and_its_thread_is_followed(
    store: PostgresStore, pg_dsn: str
) -> None:
    await listen(store)
    slack, screen, answer = FakeSlack(), Screen(True), Answer()
    runtime = Runtime(store, slack, FunctionModel(answer), screen_model=FunctionModel(screen))
    uid = unique()
    root = f"1700000000.{uid[:6]}"
    key = SessionKey(team="T01", channel="C01", thread_root=root)
    first = message(f"Ev1-{uid}", root, "how do I roll back a deploy?")
    await runtime.start(pg_dsn)
    try:
        await runtime.ingress(first)

        async def answered() -> bool:
            return len(slack.posted) == 1

        await eventually(answered)
        assert [(m["text"], m["thread_ts"]) for m in slack.posted] == [
            ("Redeploy the previous tag.", root)
        ]
        await finished(runtime, key, first.event_id)
        assert await store.get_session(key) is not None
        assert await usage_ids(store) == [
            f"T01:C01:{root}#Ev1-{uid}",
            f"T01:C01:{root}/screen#Ev1-{uid}",
        ]

        followup = message(
            f"Ev2-{uid}", f"1700000100.{uid[:6]}", "and for the payments service?", thread_ts=root
        )
        await runtime.ingress(followup)

        async def answered_again() -> bool:
            return len(slack.posted) == 2

        await eventually(answered_again)
        assert screen.calls == 1  # a joined thread is not screened again
        assert answer.calls == 2
    finally:
        await runtime.stop()


def prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    ]


async def test_a_passed_message_that_hondo_does_not_answer_posts_nothing(
    store: PostgresStore, pg_dsn: str
) -> None:
    await listen(store)
    slack = FakeSlack()
    seen: list[list[str]] = []

    async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(prompts(messages))
        if "runbook" not in seen[-1][-1]:
            return ModelResponse(
                parts=[
                    TextPart("No reply needed."),
                    ToolCallPart("no_reply", {"reason": "a status note, not a question"}),
                ]
            )
        return ModelResponse(parts=[TextPart("Here is the runbook.")])

    runtime = Runtime(store, slack, FunctionModel(model), screen_model=FunctionModel(Screen(True)))
    uid = unique()
    root = f"1700000000.{uid[:6]}"
    key = SessionKey(team="T01", channel="C01", thread_root=root)
    first = message(f"Ev1-{uid}", root, "deploys are slow today")
    await runtime.start(pg_dsn)
    try:
        await runtime.ingress(first)
        await finished(runtime, key, first.event_id)
        assert slack.posted == [] and slack.statuses == []
        assert "<guidance>" in seen[0][-1] and GOAL in seen[0][-1]

        asked = mention(f"Ev2-{uid}", f"1700000100.{uid[:6]}", "<@B01> runbook?", thread_ts=root)
        await runtime.ingress(asked)
        await finished(runtime, key, asked.event_id)

        assert [(m["text"], m["thread_ts"]) for m in slack.posted] == [
            ("Here is the runbook.", root)
        ]
        assert "<guidance>" not in seen[-1][-1]
        stored = ModelMessagesTypeAdapter.validate_python(await store.get_session_messages(key))
        assert [p.split(": ", 1)[1].split("\n")[0] for p in prompts(stored)] == [
            "deploys are slow today",
            "<@B01> runbook?",
        ]
        assert isinstance(stored[-1], ModelResponse)
        assert stored[-1].parts == [TextPart("Here is the runbook.")]
    finally:
        await runtime.stop()


async def test_a_message_the_screen_drops_leaves_no_trace(
    store: PostgresStore, pg_dsn: str
) -> None:
    await listen(store)
    slack, answer = FakeSlack(), Answer()

    async def no_summary(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise AssertionError("a dropped message gets no summary")

    runtime = Runtime(
        store,
        slack,
        FunctionModel(answer),
        summary_model=FunctionModel(no_summary),
        screen_model=FunctionModel(Screen(False)),
    )
    uid = unique()
    root = f"1700000000.{uid[:6]}"
    key = SessionKey(team="T01", channel="C01", thread_root=root)
    event = message(f"Ev1-{uid}", root, "tacos at 1pm?")
    await runtime.start(pg_dsn)
    try:
        await runtime.ingress(event)
        await finished(runtime, key, event.event_id)

        assert answer.calls == 0
        assert slack.posted == [] and slack.statuses == []
        assert await store.get_session(key) is None
        assert await store.thread_summary(key) is None
        assert await usage_ids(store) == [f"T01:C01:{root}/screen#Ev1-{uid}"]
    finally:
        await runtime.stop()


async def test_a_failed_screen_posts_no_error_notice(store: PostgresStore, pg_dsn: str) -> None:
    await listen(store)
    slack, answer = FakeSlack(), Answer()
    runtime = Runtime(
        store,
        slack,
        FunctionModel(answer),
        screen_model=FunctionModel(Screen(RuntimeError("provider down"))),
    )
    uid = unique()
    root = f"1700000000.{uid[:6]}"
    key = SessionKey(team="T01", channel="C01", thread_root=root)
    event = message(f"Ev1-{uid}", root, "how do I roll back a deploy?")
    await runtime.start(pg_dsn)
    try:
        await runtime.ingress(event)
        await finished(runtime, key, event.event_id)
        await runtime.dispatcher.sweep_failed_runs()
        await runtime.dispatcher.deliver_run_notices()

        statuses = await DBOS.list_workflows_async(
            workflow_ids=[run_workflow_id(key, event.event_id)], load_input=False
        )
        assert [status.status for status in statuses] == ["SUCCESS"]
        assert answer.calls == 0
        assert slack.posted == []
        assert await store.get_session(key) is None
    finally:
        await runtime.stop()


async def test_a_mention_during_the_screen_is_answered_after_a_drop(
    store: PostgresStore, pg_dsn: str
) -> None:
    await listen(store)
    slack, screen, answer = FakeSlack(), Screen(False), Answer("Here is the runbook.")
    screen.release = asyncio.Event()
    runtime = Runtime(store, slack, FunctionModel(answer), screen_model=FunctionModel(screen))
    uid = unique()
    root = f"1700000000.{uid[:6]}"
    key = SessionKey(team="T01", channel="C01", thread_root=root)
    first = message(f"Ev1-{uid}", root, "deploys are slow today")
    await runtime.start(pg_dsn)
    try:
        await runtime.ingress(first)
        await asyncio.wait_for(screen.started.wait(), timeout=15)
        # The screen is busy: the mention becomes mail for the live run.
        await runtime.ingress(
            mention(f"Ev2-{uid}", f"1700000100.{uid[:6]}", "<@B01> runbook?", thread_ts=root)
        )
        screen.release.set()
        await finished(runtime, key, first.event_id)

        assert [(m["text"], m["thread_ts"]) for m in slack.posted] == [
            ("Here is the runbook.", root)
        ]
        assert screen.calls == 1
        # The screening run answered the mention itself; no second run started.
        assert not await runtime.backend.workflow_finished(run_workflow_id(key, f"Ev2-{uid}"))
    finally:
        await runtime.stop()


async def test_a_deploy_during_the_screen_posts_nothing(store: PostgresStore, pg_dsn: str) -> None:
    await listen(store)
    slack = FakeSlack()
    uid = unique()
    root = f"1700000000.{uid[:6]}"
    key = SessionKey(team="T01", channel="C01", thread_root=root)
    event = message(f"Ev1-{uid}", root, "how do I roll back a deploy?")
    old = Runtime(
        store,
        slack,
        FunctionModel(Answer()),
        runtime=RuntimeConfig(queue_polling_interval=60),
        screen_model=FunctionModel(Screen(True)),
    )
    await old.start(pg_dsn, application_version="vA")
    try:
        await old.ingress(event)
    finally:
        await old.stop()

    new = Runtime(store, slack, FunctionModel(Answer()), screen_model=FunctionModel(Screen(True)))
    await new.start(pg_dsn, application_version="vB")
    try:
        assert await new.backend.live_workflow_id(key) is not None
        await new.dispatcher.sweep_stale_runs()
        await new.dispatcher.deliver_run_notices()

        assert await new.backend.live_workflow_id(key) is None
        assert slack.posted == []
        assert await store.get_session(key) is None
    finally:
        await new.stop()


async def test_the_gateway_keeps_top_level_messages_only_where_hondo_listens(
    store: PostgresStore,
) -> None:
    await listen(store, channel="C02")
    gateway = Gateway(store)

    def body(event_id: str, channel: str) -> dict[str, object]:
        return {
            "event_id": event_id,
            "team_id": "T01",
            "authorizations": [{"user_id": "B01"}],
            "event": {
                "type": "message",
                "channel_type": "channel",
                "channel": channel,
                "user": "U02",
                "ts": "1700000000.000100",
                "text": "how do I roll back a deploy?",
            },
        }

    assert await gateway.handle(body("Ev-C01", "C01")) is None
    assert await store.get_event("Ev-C01") is None
    kept = await gateway.handle(body("Ev-C02", "C02"))
    assert kept is not None and kept.channel == "C02" and kept.thread_ts is None
    assert await store.get_event("Ev-C02") is not None
