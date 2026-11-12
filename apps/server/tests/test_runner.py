"""SessionRunner unit tests: drive() as a plain coroutine (no DBOS launch).

Outside a workflow, steps are direct calls and the recv mailbox is silent, so
steering exercises the store-ledger path — the same code the workflow runs.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    BinaryContent,
    ImageUrl,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from hondo.agent.conversation import ConversationCapability
from hondo.agent.todos import TodosCapability
from hondo.agent.tool_boundary import ToolBoundary
from hondo.chat import ChatFile, ChatMessage
from hondo.config import RuntimeConfig
from hondo.media import MediaContent
from hondo.models import (
    Event,
    PendingFailure,
    Session,
    SessionKey,
    SessionState,
    SteerPriority,
)
from hondo.runtime import progress as progress_module
from hondo.runtime.durable import run_workflow_id
from hondo.runtime.model_selection import RunModel
from hondo.runtime.progress import ProgressReporter
from hondo.runtime.runner import SessionRunner, render_message
from hondo.slack import SlackStyle
from hondo.users import UserDirectory
from tests.fakes import FakeSlack, FakeStore, make_event

FAST = RuntimeConfig(debounce_period=0)
STYLE = SlackStyle()


def make_conversation(slack: FakeSlack) -> ConversationCapability:
    return ConversationCapability(
        client=slack,
        directory=UserDirectory(slack),
        style=STYLE,
    )


def make_runner(
    agent: Agent[Any, Any],
    store: FakeStore,
    slack: FakeSlack,
    conversation: ConversationCapability,
    *,
    runtime: RuntimeConfig,
    start_summary: Callable[[SessionKey, str], Awaitable[None]] | None = None,
) -> SessionRunner:
    return SessionRunner(
        agent,
        store=store,
        client=slack,
        style=STYLE,
        directory=conversation.directory,
        runtime=runtime,
        start_summary=start_summary,
    )


def user_prompts(messages: list[ModelMessage]) -> list[str]:
    return [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    ]


def build(
    model: Any,
    store: FakeStore,
    slack: FakeSlack,
    runtime: RuntimeConfig = FAST,
    start_summary: Callable[[SessionKey, str], Awaitable[None]] | None = None,
) -> SessionRunner:
    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(model, capabilities=[ToolBoundary(), conversation])
    return make_runner(
        agent, store, slack, conversation, runtime=runtime, start_summary=start_summary
    )


async def drive(runner: SessionRunner, event: Event) -> str:
    key = SessionKey.from_event(event)
    return await runner.drive(key.model_dump(mode="json"), event.model_dump(mode="json"))


def snapshot_payload(store: FakeStore, workflow_id: str) -> dict[str, Any]:
    return deepcopy(store.run_snapshots[workflow_id][2])


def snapshot_messages(store: FakeStore, workflow_id: str) -> list[ModelMessage]:
    return ModelMessagesTypeAdapter.validate_python(
        snapshot_payload(store, workflow_id)["messages"]
    )


async def test_final_answer_is_a_new_message_and_session_persists() -> None:
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("All good.")])

    runner = build(FunctionModel(model_fn), store, slack)
    event = make_event()
    output = await drive(runner, event)

    assert output == "All good."
    assert [m["text"] for m in slack.posted] == ["All good."]
    assert slack.updates == []
    assert slack.reactions == []
    session = store.sessions[SessionKey.from_event(event)]
    assert session.state is SessionState.IDLE
    assert session.last_event_id == event.event_id
    assert await store.get_session_messages(SessionKey.from_event(event))
    # Everyone seen this run — requester and the bot itself — is in the roster.
    assert session.roster == {"U01": "jane", "UBOT": "Hondo"}


async def test_prompt_carries_send_time_from_event_not_wall_clock() -> None:
    store, slack = FakeStore(), FakeSlack()
    seen: list[list[str]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Hondo.")])

    runner = build(FunctionModel(model_fn), store, slack)
    event = make_event()
    await drive(runner, event)

    # The stamp is the model's only clock; it derives from event.ts (replay-safe)
    # and lives in the user message, never in cached instructions.
    assert STYLE.format_time(event.ts) == "2023-11-14 22:13 UTC"
    # Transcript line: resolved speaker, expanded bot mention.
    assert seen[0][0] == "[2023-11-14 22:13 UTC] jane: @Hondo what is our deploy process?"


def test_malformed_ts_drops_the_stamp_never_the_run() -> None:
    assert STYLE.format_time("1700000000.000100") == "2023-11-14 22:13 UTC"
    assert STYLE.format_time("not-a-ts") == ""
    assert render_message("U01", "hi", "not-a-ts", {"U01": "jane"}, STYLE) == "jane: hi"


def test_render_message_falls_back_to_raw_ids_outside_the_roster() -> None:
    # Unresolved speakers and mentions stay in raw <@id> form — still meaningful,
    # and the lookup_user tool is the model's escape hatch.
    line = render_message("U9", "ask <@U8> or <@U01>", "not-a-ts", {"U01": "jane"}, STYLE)
    assert line == "<@U9>: ask <@U8> or @jane"
    assert render_message(None, "hi", "not-a-ts", {}, STYLE) == "someone: hi"


def _user_turn(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(text)])


def _answer(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(text)])


def test_trim_history_cuts_at_a_user_turn_boundary() -> None:
    from hondo.runtime.runner import _trim_history

    messages: list[ModelMessage] = [
        _user_turn("a"),
        _answer("1"),
        _user_turn("b"),
        _answer("2"),
        _user_turn("c"),
        _answer("3"),
    ]
    assert _trim_history(messages, 6) == messages  # under the limit: untouched
    # Limit 3 lands mid-exchange; the cut advances to the next user turn.
    assert _trim_history(messages, 3) == messages[4:]


def test_trim_history_with_no_boundary_drops_rather_than_orphans() -> None:
    # A tail of tool traffic with no user turn: slicing it would orphan tool
    # returns from their calls, which providers reject. Fresh beats broken.
    from pydantic_ai.messages import ToolReturnPart

    from hondo.runtime.runner import _trim_history

    messages: list[ModelMessage] = [
        _user_turn("a"),
        _answer("1"),
        ModelRequest(parts=[ToolReturnPart(tool_name="t", content="r", tool_call_id="c1")]),
        _answer("2"),
    ]
    assert _trim_history(messages, 2) == []


def test_trim_history_skips_a_steer_riding_on_a_tool_return() -> None:
    # pydantic-ai appends an injected steer to a pending tool-return request:
    # one ModelRequest carrying both parts. It is NOT a clean boundary — the
    # tool returns' calls live in the preceding (dropped) response.
    from pydantic_ai.messages import ToolReturnPart

    from hondo.runtime.runner import _trim_history

    combined = ModelRequest(
        parts=[
            ToolReturnPart(tool_name="t", content="r", tool_call_id="c1"),
            UserPromptPart("[Slack reply from <@U01>]: make it purple"),
        ]
    )
    messages: list[ModelMessage] = [
        _user_turn("a"),
        _answer("1"),
        combined,
        _answer("2"),
        _user_turn("b"),
        _answer("3"),
    ]
    # The combined request inside the tail is skipped; the next clean user
    # turn is the boundary.
    assert _trim_history(messages, 4) == messages[4:]
    # And when the combined request is the only candidate, drop everything.
    assert _trim_history(messages[:4], 3) == []


def test_trim_history_skips_a_steer_riding_on_a_retry_prompt() -> None:
    # Retry prompts are response-bound too: they answer a failed tool call or
    # invalid output in the preceding response. Not a clean boundary.
    from pydantic_ai.messages import RetryPromptPart

    from hondo.runtime.runner import _trim_history

    combined = ModelRequest(
        parts=[
            RetryPromptPart(content="bad args", tool_name="t", tool_call_id="c1"),
            UserPromptPart("[Slack reply from <@U01>]: hurry up"),
        ]
    )
    messages: list[ModelMessage] = [_user_turn("a"), _answer("1"), combined, _answer("2")]
    assert _trim_history(messages, 3) == []


async def test_run_ids_mark_snapshot_delta_and_merged_history_stays_unowned() -> None:
    calls = 0

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart("lookup", {}, tool_call_id="own-call")])
        return ModelResponse(parts=[TextPart("done")])

    agent: Agent[None, str] = Agent(FunctionModel(model_fn))

    @agent.tool_plain
    async def lookup() -> str:
        return "found"

    serialized_history = ModelMessagesTypeAdapter.dump_json(
        [
            # The tool return needs a matching call: since 2.10 pydantic-ai
            # repairs message_history and drops orphaned tool returns.
            ModelResponse(
                parts=[ToolCallPart("lookup", {}, tool_call_id="call-1")],
                run_id="history-run-1",
            ),
            ModelRequest(
                parts=[ToolReturnPart(tool_name="lookup", content="result", tool_call_id="call-1")],
                run_id="history-run-1",
            ),
            ModelRequest(
                parts=[UserPromptPart("trailing history request")],
                run_id="history-run-2",
            ),
        ]
    )
    history = ModelMessagesTypeAdapter.validate_json(serialized_history)
    async with agent.iter("new prompt", message_history=history) as run:
        node = run.next_node
        while not Agent.is_end_node(node):
            node = await run.next(node)
        run_id = run.run_id
        all_messages = run.all_messages()
        result = run.result
        assert result is not None
        new_messages = result.new_messages()

    # Contract behind snapshot-delta filtering: run-owned messages are stamped,
    # while normalized history remains outside every run's ownership boundary.
    # The tool-return request matters most — an unstamped one would silently
    # drop tool results from every snapshot.
    assert new_messages
    assert all(message.run_id == run_id for message in new_messages)
    assert any(
        isinstance(message, ModelRequest)
        and any(isinstance(part, ToolReturnPart) for part in message.parts)
        for message in new_messages
    )
    assert [message.run_id for message in history] == [
        "history-run-1",
        "history-run-1",
        "history-run-2",
    ]
    assert isinstance(all_messages[1], ModelRequest)
    assert [type(part) for part in all_messages[1].parts] == [ToolReturnPart, UserPromptPart]
    assert all_messages[1].run_id is None


async def test_backfill_composed_correctly() -> None:
    store, slack = FakeStore(), FakeSlack()
    slack.thread_messages = [
        ChatMessage(author="U07", ref="1700000000.000100", text="we saw 500s earlier"),
        ChatMessage(author="U01", ref="1700000000.000300", text="<@UBOT> thoughts?"),
    ]
    seen: list[list[str]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Looking.")])

    runner = build(FunctionModel(model_fn), store, slack)
    event = make_event(
        event_id="Ev9",
        ts="1700000000.000300",
        thread_ts="1700000000.000100",
        text="<@UBOT> thoughts?",
    )
    await drive(runner, event)

    prompt = seen[0][0]
    assert "Earlier messages in this thread:" in prompt
    # U07 isn't resolvable in the fake workspace: raw id form, never a crash.
    assert "[2023-11-14 22:13 UTC] <@U07>: we saw 500s earlier" in prompt
    assert prompt.rstrip().endswith("jane: @Hondo thoughts?")
    # The triggering message itself is the prompt, not part of the backfill.
    assert prompt.count("thoughts?") == 1


async def test_unconsumed_mail_drains_into_fresh_prompt() -> None:
    store, slack = FakeStore(), FakeSlack()
    root = make_event()
    key = SessionKey.from_event(root)
    stranded = make_event(event_id="Ev5", ts="1700000000.000500", text="try the eu cluster")
    mail = await store.insert_mail(key, stranded, SteerPriority.ASAP, target_workflow_id=None)
    assert mail is not None
    seen: list[list[str]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("On it.")])

    runner = build(FunctionModel(model_fn), store, slack)
    await drive(runner, root)

    assert seen[0][0] == (
        "[2023-11-14 22:13 UTC] jane: @Hondo what is our deploy process?\n\n"
        "[2023-11-14 22:13 UTC] jane: try the eu cluster"
    )
    assert mail.id in store.consumed


def thread_message(event_id: str, ts: str, text: str) -> Event:
    return make_event(
        event_id=event_id,
        kind="message",
        ts=ts,
        thread_ts="1700000000.000100",
        text=text,
        mentions_bot=False,
    )


async def test_burst_reaches_the_model_in_slack_order() -> None:
    """The trigger and the mail around it read in thread order, whatever the ledger order."""
    store, slack = FakeStore(), FakeSlack()
    request = thread_message("Ev-1", "1700000000.000200", "check the deploy")
    key = SessionKey.from_event(request)
    # Concurrent deliveries can enter the ledger out of order.
    for later in (
        thread_message("Ev-3", "1700000000.000400", "<@UBOT>"),
        thread_message("Ev-2", "1700000000.000300", "hello?"),
    ):
        await store.insert_mail(key, later, SteerPriority.ASAP, target_workflow_id=None)
    seen: list[list[str]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Deploy is fine.")])

    await drive(build(FunctionModel(model_fn), store, slack), request)

    assert seen == [
        [
            "[2023-11-14 22:13 UTC] jane: check the deploy\n\n"
            "[2023-11-14 22:13 UTC] jane: hello?\n\n"
            "[2023-11-14 22:13 UTC] jane: @Hondo"
        ]
    ]
    assert [m["text"] for m in slack.posted] == ["Deploy is fine."]


async def test_mail_older_than_backfill_keeps_slack_order() -> None:
    """A correction posted after a waiting request must read after it."""
    store, slack = FakeStore(), FakeSlack()
    request = thread_message("Ev-2", "1700000000.000200", "roll it back")
    key = SessionKey.from_event(request)
    await store.insert_mail(key, request, SteerPriority.ASAP, target_workflow_id=None)
    slack.thread_messages = [
        ChatMessage(author="U01", ref="1700000000.000100", text="the deploy failed"),
        ChatMessage(author="U01", ref="1700000000.000200", text="roll it back"),
        ChatMessage(author="U01", ref="1700000000.000300", text="never mind, it was the cache"),
    ]
    seen: list[list[str]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Leaving it.")])

    trigger = thread_message("Ev-4", "1700000000.000400", "<@UBOT> what now?")
    await drive(build(FunctionModel(model_fn), store, slack), trigger)

    assert seen == [
        [
            "Earlier messages in this thread:\n"
            "[2023-11-14 22:13 UTC] jane: the deploy failed\n\n"
            "[2023-11-14 22:13 UTC] jane: roll it back\n\n"
            "[2023-11-14 22:13 UTC] jane: never mind, it was the cache\n\n"
            "[2023-11-14 22:13 UTC] jane: @Hondo what now?"
        ]
    ]


async def test_first_reply_in_a_thread_does_not_wait() -> None:
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Fast.")])

    runner = build(
        FunctionModel(model_fn),
        store,
        slack,
        RuntimeConfig(debounce_period=60, debounce_max_wait=60),
    )
    event = make_event()
    await store.append_event(event)

    await asyncio.wait_for(drive(runner, event), timeout=5)

    assert [m["text"] for m in slack.posted] == ["Fast."]


async def test_engaged_thread_waits_for_quiet_and_answers_a_burst_once() -> None:
    store, slack = FakeStore(), FakeSlack()
    seen: list[str] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages)[-1])
        return ModelResponse(parts=[TextPart(f"answer {len(seen)}")])

    runner = build(
        FunctionModel(model_fn),
        store,
        slack,
        RuntimeConfig(debounce_period=1.0, debounce_max_wait=10),
    )
    first = make_event()
    await store.append_event(first)
    await drive(runner, first)

    request = thread_message("Ev-2", "1700000000.000200", "check the deploy")
    key = SessionKey.from_event(request)
    await store.append_event(request)
    run = asyncio.create_task(drive(runner, request))
    for later in (
        thread_message("Ev-3", "1700000000.000300", "hello?"),
        thread_message("Ev-4", "1700000000.000400", "<@UBOT>"),
    ):
        await asyncio.sleep(0.2)
        # What the dispatcher does with a message that finds the run live.
        await store.insert_mail(key, later, SteerPriority.WHEN_IDLE, target_workflow_id=None)
    await run

    assert seen[1] == (
        "[2023-11-14 22:13 UTC] jane: check the deploy\n\n"
        "[2023-11-14 22:13 UTC] jane: hello?\n\n"
        "[2023-11-14 22:13 UTC] jane: @Hondo"
    )
    assert [m["text"] for m in slack.posted] == ["answer 1", "answer 2"]


async def test_wait_ends_at_the_cap_counted_from_the_first_waiting_message() -> None:
    store, slack = FakeStore(), FakeSlack()
    seen: list[str] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages)[-1])
        return ModelResponse(parts=[TextPart(f"answer {len(seen)}")])

    runner = build(
        FunctionModel(model_fn),
        store,
        slack,
        RuntimeConfig(debounce_period=60, debounce_max_wait=15),
    )
    first = make_event()
    await store.append_event(first)
    await drive(runner, first)

    waiting = thread_message("Ev-2", "1700000000.000200", "check the deploy")
    key = SessionKey.from_event(waiting)
    mail = await store.insert_mail(key, waiting, SteerPriority.WHEN_IDLE, target_workflow_id=None)
    assert mail is not None
    store.mail_created[mail.id] -= 20  # arrived 20 s ago: past the 15 s cap
    request = thread_message("Ev-3", "1700000000.000300", "hello?")
    await store.append_event(request)

    await asyncio.wait_for(drive(runner, request), timeout=5)

    assert seen[1] == (
        "[2023-11-14 22:13 UTC] jane: check the deploy\n\n[2023-11-14 22:13 UTC] jane: hello?"
    )


async def test_run_end_drain_triggers_followup_run() -> None:
    store, slack = FakeStore(), FakeSlack()
    root = make_event()
    key = SessionKey.from_event(root)
    calls = 0

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            # A reply lands in the store right as this run finishes: no live
            # injection possible; the run-end sweep must pick it up.
            late = make_event(event_id="Ev6", ts="1700000000.000600", text="and also prod")
            await store.insert_mail(key, late, SteerPriority.ASAP, target_workflow_id=None)
            return ModelResponse(parts=[TextPart("Done with staging.")])
        return ModelResponse(parts=[TextPart("Done with prod too.")])

    runner = build(FunctionModel(model_fn), store, slack)
    output = await drive(runner, root)

    assert calls == 2
    assert output == "Done with prod too."
    # Both answers are their own messages, in order.
    assert [m["text"] for m in slack.posted] == ["Done with staging.", "Done with prod too."]
    assert not await store.unconsumed_mail(key)


async def test_followup_cap_leaves_run_end_mail_unconsumed() -> None:
    # At the cap the drain is skipped entirely: claiming mail the run cannot
    # process would consume it into the void. Left unconsumed, the reconciler
    # hands it to a fresh run — accepted steers are never dropped.
    store, slack = FakeStore(), FakeSlack()
    root = make_event()
    key = SessionKey.from_event(root)
    calls = 0

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        late = make_event(event_id="Ev6", ts="1700000000.000600", text="and also prod")
        await store.insert_mail(key, late, SteerPriority.ASAP, target_workflow_id=None)
        return ModelResponse(parts=[TextPart("Done with staging.")])

    runner = build(
        FunctionModel(model_fn), store, slack, runtime=RuntimeConfig(max_followup_runs=0)
    )
    output = await drive(runner, root)

    assert calls == 1
    assert output == "Done with staging."
    assert [m.event_id for m in await store.unconsumed_mail(key)] == ["Ev6"]


async def test_undelivered_leftovers_get_a_followup_even_at_the_cap() -> None:
    # Leftovers are steers a run already claimed from the ledger (an enqueue
    # that raced its end): nothing else can recover them, so the cap must not
    # apply — dropping them would lose accepted messages permanently.
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise AssertionError("the model is never reached; _run_once is faked")

    runner = build(
        FunctionModel(model_fn), store, slack, runtime=RuntimeConfig(max_followup_runs=0)
    )
    results = [
        ("first answer", [["[2023-11-14 22:13 UTC] jane: also prod"]]),
        ("second answer", []),
    ]
    prompts: list[str | None] = []

    async def fake_run_once(
        key: Any,
        prompt: Any,
        history: Any,
        progress: Any,
        seq: Any,
        state: Any,
        roster: Any,
        selected_model: Any,
    ) -> tuple[str, list[list[str]], list[ModelMessage], list[ModelMessage]]:
        prompts.append(prompt)
        output, leftovers = results.pop(0)
        return output, leftovers, [], []

    runner._run_once = fake_run_once  # type: ignore[method-assign]
    output = await drive(runner, make_event())

    assert output == "second answer"
    assert [m["text"] for m in slack.posted] == ["first answer", "second answer"]
    assert prompts[1] is not None and "also prod" in prompts[1]


async def test_history_from_another_schema_version_starts_fresh() -> None:
    # Old-schema rows must not be replayed as model turns — and must not be
    # parsed at all (this row is deliberately not a valid message dump).
    from hondo.models import HISTORY_SCHEMA_VERSION, Session

    store, slack = FakeStore(), FakeSlack()
    event = make_event()
    key = SessionKey.from_event(event)
    await store.save_session(Session(key=key))
    store.session_messages[key] = [
        (
            "old-run",
            0,
            HISTORY_SCHEMA_VERSION + 1,
            {"not": "a message schema we know"},
        )
    ]
    seen: list[list[str]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Fresh start.")])

    runner = build(FunctionModel(model_fn), store, slack)
    output = await drive(runner, event)

    assert output == "Fresh start."
    assert len(seen[0]) == 1  # only the triggering prompt; no stale turns


async def test_long_history_is_trimmed_on_load() -> None:
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    from hondo.models import Session

    store, slack = FakeStore(), FakeSlack()
    event = make_event()
    key = SessionKey.from_event(event)
    long_history: list[ModelMessage] = []
    for i in range(4):
        long_history.append(ModelRequest(parts=[UserPromptPart(f"turn {i}")]))
        long_history.append(ModelResponse(parts=[TextPart(f"answer {i}")]))
    await store.save_session(Session(key=key))
    await store.append_session_messages(
        key,
        run_id="seed",
        messages=cast(
            "list[dict[str, Any]]",
            ModelMessagesTypeAdapter.dump_python(long_history, mode="json"),
        ),
    )
    seen: list[list[str]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Bounded.")])

    runner = build(
        FunctionModel(model_fn), store, slack, runtime=RuntimeConfig(max_history_messages=2)
    )
    await drive(runner, event)

    prompts = seen[0]
    assert prompts[0] == "turn 3"  # the kept tail starts at a user turn
    assert len(prompts) == 2  # turn 3 + the triggering prompt; turns 0-2 dropped

    # Trimming bounds only what the model sees: the stored transcript keeps all
    # seeded rows, and the run appends exactly its own delta — never the loaded
    # (or trimmed-away) history again.
    rows = store.session_messages[key]
    assert [row for row in rows if row[0] == "seed"] == rows[:8]
    assert [(run_id, seq) for run_id, seq, _, _ in rows[8:]] == [
        (event.event_id, 0),
        (event.event_id, 1),
    ]


async def test_consecutive_runs_append_only_their_message_deltas() -> None:
    store, slack = FakeStore(), FakeSlack()
    root = make_event()
    key = SessionKey.from_event(root)
    calls = 0

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        return ModelResponse(parts=[TextPart(f"Answer {calls}.")])

    runner = build(FunctionModel(model_fn), store, slack)
    await drive(runner, root)
    first_rows = deepcopy(store.session_messages[key])

    followup = make_event(
        event_id="Ev002",
        ts="1700000001.000200",
        thread_ts=root.ts,
        text="<@UBOT> one more thing",
    )
    await drive(runner, followup)

    rows = store.session_messages[key]
    assert rows[: len(first_rows)] == first_rows
    second_rows = rows[len(first_rows) :]
    assert [(run_id, seq) for run_id, seq, _, _ in second_rows] == [
        (followup.event_id, 0),
        (followup.event_id, 1),
    ]
    assert len(rows) == 4  # two request/response deltas, not 2 then 4


async def test_mid_run_steer_is_claimed_acked_and_answered_below() -> None:
    """The answer lands below the steer it incorporates; the steer message gets
    the heard-you reaction; nothing is ever edited or deleted."""
    store, slack = FakeStore(), FakeSlack()
    root = make_event()
    key = SessionKey.from_event(root)
    steer = make_event(event_id="Ev7", ts="1700000000.000700", text="make it purple")

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        from pydantic_ai.messages import ToolCallPart

        prompts = user_prompts(messages)
        if any("purple" in p for p in prompts):
            return ModelResponse(parts=[TextPart("Purple it is.")])
        # First request: narrate + call the tool; a reply lands while the model
        # "works"; the claim before the post-tool request must pick it up.
        await store.insert_mail(key, steer, SteerPriority.ASAP, target_workflow_id=None)
        return ModelResponse(
            parts=[TextPart("On it — looking it up."), ToolCallPart(tool_name="lookup", args={})]
        )

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    async def lookup() -> str:
        return "resource42"

    runner = make_runner(agent, store, slack, conversation, runtime=FAST)
    output = await drive(runner, root)

    assert output == "Purple it is."
    assert [m["text"] for m in slack.posted] == ["On it — looking it up.", "Purple it is."]
    assert slack.updates == []
    assert slack.reactions == []
    mail = await store.mail_for_event(steer.event_id)
    assert mail is not None and mail.consumed


async def test_end_of_run_steer_posts_displaced_answer_before_steer_answer() -> None:
    store, slack = FakeStore(), FakeSlack()
    root = make_event()
    key = SessionKey.from_event(root)
    steer = make_event(event_id="Ev8", ts="1700000000.000800", text="make it purple")
    calls = 0

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        from pydantic_ai.messages import ToolCallPart

        nonlocal calls
        calls += 1
        if calls == 1:
            # The tool creates a request boundary where the runner claims the steer;
            # when_idle holds it until the text-only answer would end the run.
            await store.insert_mail(key, steer, SteerPriority.WHEN_IDLE, target_workflow_id=None)
            return ModelResponse(parts=[ToolCallPart(tool_name="lookup", args={})])
        if any("purple" in prompt for prompt in user_prompts(messages)):
            return ModelResponse(parts=[TextPart("Purple it is.")])
        return ModelResponse(parts=[TextPart("The deploy is healthy.")])

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    async def lookup() -> str:
        return "healthy"

    runner = make_runner(agent, store, slack, conversation, runtime=FAST)
    output = await drive(runner, root)

    assert output == "Purple it is."
    assert [message["text"] for message in slack.posted] == [
        "The deploy is healthy.",
        "Purple it is.",
    ]
    mail = await store.mail_for_event(steer.event_id)
    assert mail is not None and mail.consumed


async def test_consumed_steer_event_skips_the_run() -> None:
    store, slack = FakeStore(), FakeSlack()
    event = make_event()
    key = SessionKey.from_event(event)
    mail = await store.insert_mail(key, event, SteerPriority.ASAP, target_workflow_id="wf-x")
    assert mail is not None
    await store.consume_mail([mail.id])

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise AssertionError("the run should never reach the model")

    runner = build(FunctionModel(model_fn), store, slack)
    output = await drive(runner, event)

    assert output == ""
    assert slack.posted == [] and slack.updates == []


def screened_runner(
    store: FakeStore,
    slack: FakeSlack,
    model_fn: Any,
    passes: bool,
    screened: list[tuple[SessionKey, str]],
    summaries: list[str],
) -> SessionRunner:
    async def screen(key: SessionKey, event: Event) -> bool:
        screened.append((key, event.event_id))
        return passes

    async def start_summary(key: SessionKey, event_id: str) -> None:
        summaries.append(event_id)

    conversation = make_conversation(slack)
    return SessionRunner(
        Agent(FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]),
        store=store,
        client=slack,
        style=STYLE,
        directory=conversation.directory,
        runtime=FAST,
        start_summary=start_summary,
        screen=screen,
    )


async def test_screened_out_message_ends_the_run_quietly() -> None:
    store, slack = FakeStore(), FakeSlack()
    event = make_event(kind="message", mentions_bot=False, text="lunch anyone?")
    key = SessionKey.from_event(event)
    mail = await store.insert_mail(key, event, SteerPriority.WHEN_IDLE, target_workflow_id="wf-x")
    assert mail is not None
    screened: list[tuple[SessionKey, str]] = []
    summaries: list[str] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise AssertionError("the run should never reach the model")

    runner = screened_runner(store, slack, model_fn, False, screened, summaries)

    assert await drive(runner, event) == ""
    assert screened == [(key, event.event_id)]
    assert slack.posted == [] and slack.statuses == []
    assert summaries == []
    assert store.sessions == {}
    # A redispatched message that the screen drops must not be redispatched again.
    stored = await store.mail_for_event(event.event_id)
    assert stored is not None and stored.consumed


async def test_a_message_that_passes_the_screen_gets_a_normal_run() -> None:
    store, slack = FakeStore(), FakeSlack()
    event = make_event(kind="message", mentions_bot=False, text="how do I roll back?")
    key = SessionKey.from_event(event)
    screened: list[tuple[SessionKey, str]] = []
    summaries: list[str] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Redeploy the previous tag.")])

    runner = screened_runner(store, slack, model_fn, True, screened, summaries)

    assert await drive(runner, event) == "Redeploy the previous tag."
    assert screened == [(key, event.event_id)]
    assert [(m["text"], m["thread_ts"]) for m in slack.posted] == [
        ("Redeploy the previous tag.", event.ts)
    ]
    assert summaries == [event.event_id]
    assert await store.get_session_messages(key)


def no_reply(reason: str = "", text: str | None = None) -> ModelResponse:
    parts: list[TextPart | ToolCallPart] = [] if text is None else [TextPart(text)]
    return ModelResponse(parts=[*parts, ToolCallPart("no_reply", {"reason": reason})])


@pytest.mark.parametrize("text", [None, "No reply needed.", "[silent]"])
async def test_no_reply_posts_nothing_and_logs_the_reason(
    text: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return no_reply("This message is about lunch, not deploys.", text)

    runner = build(FunctionModel(model_fn), store, slack)
    assert await drive(runner, ambient_event()) == ""

    assert slack.posted == [] and slack.statuses == []
    logs = [record.msg for record in caplog.records if isinstance(record.msg, dict)]
    assert [entry["reason"] for entry in logs if entry["event"] == "no reply"] == [
        "This message is about lunch, not deploys."
    ]


async def test_text_next_to_no_reply_after_a_tool_call_is_not_narrated() -> None:
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart("lookup", {})])
        return no_reply(text="Nothing about deploys here, so I stay out.")

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    async def lookup() -> str:
        return "nothing relevant"

    runner = make_runner(agent, store, slack, conversation, runtime=FAST)

    assert await drive(runner, ambient_event()) == ""
    assert slack.posted == []


@pytest.mark.parametrize(
    "answer",
    [
        "I call no_reply when a message is not for me, and that posts nothing.",
        "Old replies said [silent]",
    ],
)
async def test_a_text_answer_is_always_posted(answer: str) -> None:
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart(answer)])

    runner = build(FunctionModel(model_fn), store, slack)

    assert await drive(runner, make_event(text="<@UBOT> how do you stay quiet?")) == answer
    assert [m["text"] for m in slack.posted] == [answer]


async def test_the_model_can_reply_or_call_no_reply() -> None:
    store, slack = FakeStore(), FakeSlack()
    tools: list[list[str]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        tools.append([tool.name for tool in info.output_tools])
        assert info.allow_text_output
        return no_reply()

    await drive(build(FunctionModel(model_fn), store, slack), ambient_event())

    assert tools == [["no_reply"]]


LISTENING_GUIDANCE = (
    "<guidance>\n"
    "Nobody mentioned you in this message, and you did not join this thread. "
    "You got it because an admin set you to listen in this channel for this goal:\n"
    "<goal>\nAnswer questions about deploys.\n</goal>\n"
    "Answer only when the goal applies and you can add something specific and useful. "
    "Otherwise call no_reply.\n"
    "</guidance>"
)


async def test_a_listened_message_carries_the_goal_as_guidance() -> None:
    store, slack = FakeStore(), FakeSlack()
    store.listening_goals[("T01", "C01")] = "Answer questions about deploys."
    seen: list[tuple[list[str], str | None]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        seen.append((user_prompts(messages), request.instructions))
        return no_reply()

    runner = build(FunctionModel(model_fn), store, slack)
    await drive(runner, make_event(kind="message", mentions_bot=False, text="how to deploy?"))

    [(prompts, instructions)] = seen
    assert prompts == [f"[2023-11-14 22:13 UTC] jane: how to deploy?\n{LISTENING_GUIDANCE}"]
    assert instructions is not None and "deploys" not in instructions


async def test_a_mention_in_the_same_run_gets_no_guidance() -> None:
    store, slack = FakeStore(), FakeSlack()
    store.listening_goals[("T01", "C01")] = "Answer questions about deploys."
    root = "1700000000.000100"
    event = make_event(kind="message", mentions_bot=False, ts=root, text="deploys are slow")
    key = SessionKey.from_event(event)
    mention = make_event(
        event_id="Ev002", ts="1700000000.000200", thread_ts=root, text="<@UBOT> runbook?"
    )
    await store.insert_mail(key, mention, SteerPriority.ASAP, target_workflow_id="wf-x")
    seen: list[str] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Here is the runbook.")])

    await drive(build(FunctionModel(model_fn), store, slack), event)

    assert seen == [
        f"[2023-11-14 22:13 UTC] jane: deploys are slow\n{LISTENING_GUIDANCE}\n\n"
        "[2023-11-14 22:13 UTC] jane: @Hondo runbook?"
    ]


async def test_a_reply_after_hondo_spoke_gets_no_guidance() -> None:
    store, slack = FakeStore(), FakeSlack()
    store.listening_goals[("T01", "C01")] = "Answer questions about deploys."
    root = "1700000000.000100"
    event = make_event(kind="message", mentions_bot=False, ts=root, text="deploys fail")
    key = SessionKey.from_event(event)
    reply = make_event(
        event_id="Ev002",
        kind="message",
        mentions_bot=False,
        ts="1700000000.000200",
        thread_ts=root,
        text="same here",
    )
    seen: list[str] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen[:] = user_prompts(messages)
        if len(messages) == 1:
            return ModelResponse(
                parts=[TextPart("Checking the deploy log."), ToolCallPart("lookup", {})]
            )
        if seen[-1].endswith("same here"):
            return no_reply()
        return ModelResponse(parts=[TextPart("The deploy failed on a migration.")])

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    async def lookup() -> str:
        await store.insert_mail(key, reply, SteerPriority.WHEN_IDLE, target_workflow_id="wf-x")
        return "migration 42 failed"

    await drive(make_runner(agent, store, slack, conversation, runtime=FAST), event)

    assert seen == [
        f"[2023-11-14 22:13 UTC] jane: deploys fail\n{LISTENING_GUIDANCE}",
        "[2023-11-14 22:13 UTC] jane: same here",
    ]
    assert [m["text"] for m in slack.posted] == [
        "Checking the deploy log.",
        "The deploy failed on a migration.",
    ]


async def test_a_thread_that_hondo_joined_gets_no_guidance() -> None:
    store, slack = FakeStore(), FakeSlack()
    store.listening_goals[("T01", "C01")] = "Answer questions about deploys."
    event = ambient_event(text="how to deploy?")
    await store.save_session(Session(key=SessionKey.from_event(event)))
    seen: list[str] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(user_prompts(messages))
        return no_reply()

    await drive(build(FunctionModel(model_fn), store, slack), event)

    assert seen and "<guidance>" not in "\n".join(seen)


async def test_the_history_after_no_reply_keeps_every_message() -> None:
    store, slack = FakeStore(), FakeSlack()
    root = "1700000000.000100"
    quiet = ambient_event(event_id="Ev1", ts="1700000000.000200", thread_ts=root, text="lunch?")
    key = SessionKey.from_event(quiet)
    await store.save_session(Session(key=key))
    asked = make_event(
        event_id="Ev2", ts="1700000000.000300", thread_ts=root, text="<@UBOT> deploy status?"
    )
    seen: list[list[str]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return no_reply() if "lunch" in seen[-1][-1] else _answer("All green.")

    runner = build(FunctionModel(model_fn), store, slack)
    await drive(runner, quiet)
    await drive(runner, asked)

    assert seen[1] == [
        "[2023-11-14 22:13 UTC] jane: lunch?",
        "[2023-11-14 22:13 UTC] jane: @Hondo deploy status?",
    ]
    stored = ModelMessagesTypeAdapter.validate_python(await store.get_session_messages(key))
    assert user_prompts(stored) == seen[1]
    assert isinstance(stored[-1], ModelResponse) and stored[-1].parts == [TextPart("All green.")]
    assert [m["text"] for m in slack.posted] == ["All green."]


def ambient_event(**overrides: object) -> Event:
    """An un-addressed reply in an engaged thread — the untagged-participation trigger."""
    defaults: dict[str, object] = {
        "kind": "message",
        "mentions_bot": False,
        "thread_ts": "1700000000.000001",
        "text": "anyone up for lunch?",
    }
    return make_event(**{**defaults, **overrides})


async def test_ambient_trigger_is_a_plain_transcript_line_and_silence_posts_nothing() -> None:
    store, slack = FakeStore(), FakeSlack()
    seen: list[list[str]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return no_reply()

    runner = build(FunctionModel(model_fn), store, slack)
    event = ambient_event()
    output = await drive(runner, event)

    # No addressing label: the message reads like any transcript line, and the
    # model sees it doesn't contain the bot's @name.
    assert "[2023-11-14 22:13 UTC] jane: anyone up for lunch?" in seen[0][0]
    # ...and the model's silence verdict posts nothing at all.
    assert output == ""
    assert slack.posted == [] and slack.reactions == []
    # But the bot heard it: the exchange persists, so it keeps following the thread.
    assert await store.get_session_messages(SessionKey.from_event(event))


async def test_ambient_trigger_the_model_engages_with_is_answered() -> None:
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Deploy finished 10 minutes ago, all green.")])

    runner = build(FunctionModel(model_fn), store, slack)
    output = await drive(runner, ambient_event(text="did the deploy finish?"))

    assert output == "Deploy finished 10 minutes ago, all green."
    assert [m["text"] for m in slack.posted] == [output]


async def test_ambient_run_that_dies_posts_the_failure_notice() -> None:
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("model exploded")

    event = ambient_event()
    await store.save_session(Session(key=SessionKey.from_event(event)))
    runner = build(FunctionModel(model_fn), store, slack)
    output = await drive(runner, event)

    assert output == ""
    assert len(slack.posted) == 1 and "hit an error" in slack.posted[0]["text"]


async def test_a_listened_run_that_dies_before_it_speaks_posts_nothing() -> None:
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("model exploded")

    runner = build(FunctionModel(model_fn), store, slack)
    event = make_event(kind="message", mentions_bot=False, text="how do I roll back?")

    assert await drive(runner, event) == ""
    assert slack.posted == []
    # The failure is still on record, so the thread's next run can recover it.
    session = store.sessions[SessionKey.from_event(event)]
    assert list(session.context["pending"]) == ["T01:C01:1700000000.000100#Ev001"]


async def test_a_listened_run_that_dies_after_it_spoke_posts_the_notice() -> None:
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if len(messages) == 1:
            return ModelResponse(
                parts=[TextPart("Checking the deploy runbook."), ToolCallPart("lookup", {})]
            )
        raise RuntimeError("model exploded")

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    async def lookup() -> str:
        return "runbook"

    runner = make_runner(agent, store, slack, conversation, runtime=FAST)
    event = make_event(kind="message", mentions_bot=False, text="how do I roll back?")

    assert await drive(runner, event) == ""
    assert [m["text"] for m in slack.posted][0] == "Checking the deploy runbook."
    assert len(slack.posted) == 2 and "hit an error" in slack.posted[1]["text"]


async def test_addressed_run_that_dies_still_posts_the_failure_notice() -> None:
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("model exploded")

    runner = build(FunctionModel(model_fn), store, slack)
    output = await drive(runner, make_event())

    assert output == ""
    assert len(slack.posted) == 1 and "hit an error" in slack.posted[0]["text"]


async def test_failed_run_persists_a_pending_failure_record() -> None:
    """A failure before the first response leaves only the durable pointer."""
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("boom")

    event = make_event()
    key = SessionKey.from_event(event)
    await store.append_event(event)
    output = await drive(build(FunctionModel(model_fn), store, slack), event)

    assert output == ""
    pending = PendingFailure.list_from_context(store.sessions[key].context)
    assert len(pending) == 1
    failure = pending[0]
    assert failure.workflow_id == run_workflow_id(key, event.event_id)
    assert failure.event_id == event.event_id
    assert failure.reason == "RuntimeError"  # type name only, internal diagnostic
    datetime.fromisoformat(failure.failed_at)
    assert await store.get_session_messages(key) == []
    assert store.run_snapshots == {}


async def test_next_run_recovers_the_failed_trigger_and_labels_the_failure() -> None:
    """The next successful run sees the failed trigger, runtime note, then itself."""
    store, slack = FakeStore(), FakeSlack()

    async def failing_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("boom")

    failed = make_event(event_id="Ev-failed", text="<@UBOT> recover this failed request")
    key = SessionKey.from_event(failed)
    await store.append_event(failed)
    await drive(build(FunctionModel(failing_model), store, slack), failed)
    slack.thread_messages.clear()

    seen: list[list[str]] = []

    async def succeeding_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Recovered.")])

    current = make_event(
        event_id="Ev-current",
        ts="1700000000.000300",
        thread_ts=failed.ts,
        text="<@UBOT> try again now",
    )
    await store.append_event(current)
    await drive(build(FunctionModel(succeeding_model), store, slack), current)

    prompt = seen[0][0]
    failed_line = render_message(
        failed.user, failed.text, failed.ts, {"U01": "jane", "UBOT": "Hondo"}, STYLE
    )
    current_line = render_message(
        current.user, current.text, current.ts, {"U01": "jane", "UBOT": "Hondo"}, STYLE
    )
    assert failed_line in prompt
    assert "[runtime] " in prompt
    assert "boom" not in prompt  # the cause never reaches the model
    assert prompt.index(failed_line) < prompt.index("[runtime] ") < prompt.index(current_line)
    assert PendingFailure.CONTEXT_KEY not in store.sessions[key].context
    rows = store.session_messages[key]
    assert rows and {run_id for run_id, _, _, _ in rows} == {current.event_id}


async def test_incident_replay_existing_session_variant() -> None:
    """A failed turn remains visible after saved history and is consumed on recovery."""
    store, slack = FakeStore(), FakeSlack()

    async def first_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Run one answer.")])

    first = make_event(event_id="Ev-1", text="<@UBOT> inspect the first alert")
    key = SessionKey.from_event(first)
    await store.append_event(first)
    await drive(build(FunctionModel(first_model), store, slack), first)
    first_history = deepcopy(store.session_messages[key])

    async def failing_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("rate limited")

    failed = make_event(
        event_id="Ev-2",
        ts="1700000000.000200",
        thread_ts=first.ts,
        text="does the second alert have the same cause?",
    )
    await store.append_event(failed)
    await drive(build(FunctionModel(failing_model), store, slack), failed)
    assert store.session_messages[key] == first_history

    seen: list[list[ModelMessage]] = []

    async def recovery_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(deepcopy(messages))
        return ModelResponse(parts=[TextPart("Continuing the second alert.")])

    current = make_event(
        event_id="Ev-3",
        ts="1700000000.000300",
        thread_ts=first.ts,
        text="continue",
        mentions_bot=False,
    )
    await store.append_event(current)
    await drive(build(FunctionModel(recovery_model), store, slack), current)

    prompts = user_prompts(seen[0])
    first_line = render_message(
        first.user, first.text, first.ts, {"U01": "jane", "UBOT": "Hondo"}, STYLE
    )
    assert prompts[0] == first_line
    recovery_prompt = prompts[-1]
    assert failed.text in recovery_prompt
    assert "[runtime] " in recovery_prompt
    assert "rate limited" not in recovery_prompt  # the cause never reaches the model
    assert recovery_prompt.index(failed.text) < recovery_prompt.index("[runtime] ")
    assert recovery_prompt.rstrip().endswith("jane: continue")
    assert PendingFailure.CONTEXT_KEY not in store.sessions[key].context
    rows = store.session_messages[key]
    assert rows[: len(first_history)] == first_history
    assert {run_id for run_id, _, _, _ in rows[len(first_history) :]} == {current.event_id}


async def test_claimed_steer_of_a_dead_run_is_recovered_once() -> None:
    """A dead workflow's consumed steer appears once, in Slack order, on recovery."""
    store, slack = FakeStore(), FakeSlack()

    async def failing_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("boom")

    failed = make_event(event_id="Ev-A", text="<@UBOT> start the investigation")
    key = SessionKey.from_event(failed)
    await store.append_event(failed)
    await drive(build(FunctionModel(failing_model), store, slack), failed)
    slack.thread_messages.clear()

    workflow_id = run_workflow_id(key, failed.event_id)
    claimed = make_event(
        event_id="Ev-B",
        kind="message",
        ts="1700000000.000200",
        thread_ts=failed.ts,
        text="claimed steer detail",
        mentions_bot=False,
    )
    await store.append_event(claimed)
    mail = await store.insert_mail(key, claimed, SteerPriority.ASAP, target_workflow_id=workflow_id)
    assert mail is not None
    store.consumed.add(mail.id)
    store.claims[f"{workflow_id}:claim_steers:3"] = {mail.id}

    seen: list[list[str]] = []

    async def recovery_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Recovered both messages.")])

    current = make_event(
        event_id="Ev-C",
        ts="1700000000.000300",
        thread_ts=failed.ts,
        text="<@UBOT> continue",
    )
    await store.append_event(current)
    await drive(build(FunctionModel(recovery_model), store, slack), current)

    prompt = seen[0][0]
    failed_line = render_message(
        failed.user, failed.text, failed.ts, {"U01": "jane", "UBOT": "Hondo"}, STYLE
    )
    claimed_line = render_message(
        claimed.user, claimed.text, claimed.ts, {"U01": "jane", "UBOT": "Hondo"}, STYLE
    )
    assert prompt.count(claimed.text) == 1
    assert prompt.index(failed_line) < prompt.index(claimed_line) < prompt.index("[runtime] ")


async def test_pending_trigger_deduped_against_backfill() -> None:
    """A pending trigger already found by thread backfill renders once with its note."""
    store, slack = FakeStore(), FakeSlack()

    async def failing_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("boom")

    root = "1700000000.000100"
    failed = make_event(
        event_id="Ev-A",
        ts="1700000000.000200",
        thread_ts=root,
        text="<@UBOT> pending duplicate question",
    )
    await store.append_event(failed)
    await drive(build(FunctionModel(failing_model), store, slack), failed)
    slack.thread_messages = [
        ChatMessage(author="U07", ref=root, text="thread root"),
        ChatMessage(author="U01", ref=failed.ts, text=failed.text),
        ChatMessage(author="U07", ref="1700000000.000300", text="one more detail"),
    ]

    seen: list[list[str]] = []

    async def recovery_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Recovered.")])

    current = make_event(
        event_id="Ev-B",
        ts="1700000000.000400",
        thread_ts=root,
        text="<@UBOT> retry",
    )
    await store.append_event(current)
    await drive(build(FunctionModel(recovery_model), store, slack), current)

    prompt = seen[0][0]
    assert prompt.count("pending duplicate question") == 1
    # One chronological stream: the rebuilt trigger sits between the backfill
    # messages that surround it in the real thread, note after all of them.
    assert (
        prompt.index("Earlier messages in this thread:")
        < prompt.index("thread root")
        < prompt.index("pending duplicate question")
        < prompt.index("one more detail")
        < prompt.index("[runtime] ")
    )


async def test_root_trigger_with_empty_history_backfills_the_thread() -> None:
    """A root delivered after its replies still reads before them."""
    store, slack = FakeStore(), FakeSlack()
    slack.thread_messages = [
        ChatMessage(author="U07", ref="1700000000.000200", text="first reply"),
        ChatMessage(author="U01", ref="1700000000.000300", text="second reply"),
    ]
    seen: list[list[str]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Caught up.")])

    root = make_event(event_id="Ev-root", text="<@UBOT> delayed root")
    assert root.thread_ts is None and not root.is_dm
    await drive(build(FunctionModel(model_fn), store, slack), root)

    assert seen == [
        [
            "[2023-11-14 22:13 UTC] jane: @Hondo delayed root\n\n"
            "[2023-11-14 22:13 UTC] <@U07>: first reply\n\n"
            "[2023-11-14 22:13 UTC] jane: second reply"
        ]
    ]


async def test_failure_after_answer_still_persists_pending_without_a_notice() -> None:
    """A post-answer save failure snapshots the answer without alarming the user."""
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Answer posted.")])

    original_save = store.save_session_run
    save_attempts = 0

    async def fail_save_once(*args: Any, **kwargs: Any) -> None:
        nonlocal save_attempts
        save_attempts += 1
        if save_attempts == 1:
            raise RuntimeError("save exploded")
        await original_save(*args, **kwargs)

    store.save_session_run = fail_save_once  # type: ignore[method-assign]
    event = make_event()
    key = SessionKey.from_event(event)
    await store.append_event(event)
    output = await drive(build(FunctionModel(model_fn), store, slack), event)

    assert output == ""
    assert save_attempts == 1
    assert [message["text"] for message in slack.posted] == ["Answer posted."]
    assert slack.posted[0]["blocks"] is None
    pending = PendingFailure.list_from_context(store.sessions[key].context)
    assert len(pending) == 1
    assert pending[0].workflow_id == run_workflow_id(key, event.event_id)
    assert pending[0].reason == "RuntimeError"  # type name only, internal diagnostic
    workflow_id = pending[0].workflow_id
    assert any(
        isinstance(part, TextPart) and part.content == "Answer posted."
        for message in snapshot_messages(store, workflow_id)
        if isinstance(message, ModelResponse)
        for part in message.parts
    )

    seen: list[list[ModelMessage]] = []

    async def recovery_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(deepcopy(messages))
        return ModelResponse(parts=[TextPart("Already answered; continuing.")])

    current = make_event(
        event_id="Ev-after-save-failure",
        ts="1700000000.000300",
        thread_ts=event.ts,
        text="<@UBOT> continue",
    )
    await store.append_event(current)
    await drive(build(FunctionModel(recovery_model), store, slack), current)

    assert any(
        isinstance(part, TextPart) and part.content == "Answer posted."
        for message in seen[0]
        if isinstance(message, ModelResponse)
        for part in message.parts
    )
    assert workflow_id not in store.run_snapshots


async def test_mid_tool_failure_snapshots_only_its_delta_and_recovers_tool_results() -> None:
    store, slack = FakeStore(), FakeSlack()
    failed = make_event(
        event_id="Ev-dead-tools",
        ts="1700000000.000200",
        thread_ts="1700000000.000100",
        text="<@UBOT> inspect the dead tool run",
    )
    key = SessionKey.from_event(failed)
    await store.save_session(Session(key=key))
    saved_history: list[ModelMessage] = [_user_turn("saved turn"), _answer("saved answer")]
    await store.append_session_messages(
        key,
        run_id="saved-run",
        messages=cast(
            "list[dict[str, Any]]",
            ModelMessagesTypeAdapter.dump_python(saved_history, mode="json"),
        ),
    )
    await store.append_event(failed)
    model_calls = 0

    async def failing_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(parts=[ToolCallPart("lookup", {}, tool_call_id="dead-lookup")])
        raise RuntimeError("model died after the tool returned")

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(failing_model), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    async def lookup() -> str:
        return "resource42"

    await drive(make_runner(agent, store, slack, conversation, runtime=FAST), failed)

    workflow_id = run_workflow_id(key, failed.event_id)
    dead_messages = snapshot_messages(store, workflow_id)
    assert "saved turn" not in "\n".join(user_prompts(dead_messages))
    assert any(
        isinstance(part, ToolReturnPart) and part.content == "resource42"
        for message in dead_messages
        if isinstance(message, ModelRequest)
        for part in message.parts
    )

    slack.thread_messages.clear()
    seen: list[list[ModelMessage]] = []

    async def recovery_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(deepcopy(messages))
        return ModelResponse(parts=[TextPart("Recovered from the tool result.")])

    current = make_event(
        event_id="Ev-after-dead-tools",
        ts="1700000000.000300",
        thread_ts=failed.thread_ts,
        text="<@UBOT> continue",
    )
    await store.append_event(current)
    await drive(build(FunctionModel(recovery_model), store, slack), current)

    assert any(
        isinstance(part, ToolReturnPart) and part.content == "resource42"
        for message in seen[0]
        if isinstance(message, ModelRequest)
        for part in message.parts
    )
    recovery_prompt = user_prompts(seen[0])[-1]
    assert "inspect the dead tool run" not in recovery_prompt
    assert "was interrupted" in recovery_prompt
    assert "failed and its work was lost" not in recovery_prompt


async def test_multi_tool_failure_captures_the_completed_partial_return() -> None:
    store, slack = FakeStore(), FakeSlack()
    event = make_event(event_id="Ev-multi-tool", text="<@UBOT> run both tools")
    key = SessionKey.from_event(event)
    await store.append_event(event)

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                ToolCallPart("first", {}, tool_call_id="c1"),
                ToolCallPart("second", {}, tool_call_id="c2"),
            ]
        )

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    def first() -> str:
        return "first finished"

    @agent.tool_plain
    async def second() -> str:
        await asyncio.sleep(0)
        raise RuntimeError("second failed")

    await drive(make_runner(agent, store, slack, conversation, runtime=FAST), event)

    messages = snapshot_messages(store, run_workflow_id(key, event.event_id))
    returned_ids = {
        part.tool_call_id
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }
    assert "c1" in returned_ids


async def test_interrupted_response_with_dangling_call_reaches_the_model_closed() -> None:
    # Recovery relies on Pydantic AI closing unanswered calls in interrupted responses.
    seen: list[list[ModelMessage]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(deepcopy(messages))
        return ModelResponse(parts=[TextPart("done")])

    agent: Agent[None, str] = Agent(FunctionModel(model_fn))
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart("run the tool")]),
        ModelResponse(
            parts=[ToolCallPart("work", {}, tool_call_id="dangling")],
            state="interrupted",
        ),
    ]
    await agent.run("continue", message_history=history)

    wire = seen[0]
    returns = {
        part.tool_call_id: part
        for message in wire
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }
    assert returns["dangling"].outcome == "interrupted"


async def test_third_recovery_failure_snapshots_own_delta_and_fourth_composes_once() -> None:
    store, slack = FakeStore(), FakeSlack()
    root = "1700000000.000100"
    first = make_event(
        event_id="Ev-snapshot-a",
        ts="1700000000.000200",
        thread_ts=root,
        text="<@UBOT> first interrupted task",
    )
    key = SessionKey.from_event(first)

    async def fail_after_tool(event: Event, result: str) -> str:
        calls = 0

        async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                return ModelResponse(
                    parts=[ToolCallPart("work", {}, tool_call_id=f"{event.event_id}-call")]
                )
            raise RuntimeError("recovery died")

        conversation = make_conversation(slack)
        agent: Agent[None, str] = Agent(
            FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
        )

        @agent.tool_plain
        async def work() -> str:
            return result

        return await drive(make_runner(agent, store, slack, conversation, runtime=FAST), event)

    await store.append_event(first)
    await fail_after_tool(first, "result-a")
    first_workflow = run_workflow_id(key, first.event_id)
    first_snapshot = snapshot_messages(store, first_workflow)

    slack.thread_messages.clear()
    second = make_event(
        event_id="Ev-snapshot-b",
        ts="1700000000.000300",
        thread_ts=root,
        text="<@UBOT> second recovery attempt",
    )
    await store.append_event(second)
    await fail_after_tool(second, "result-b")
    second_workflow = run_workflow_id(key, second.event_id)
    second_snapshot = snapshot_messages(store, second_workflow)

    assert first_workflow in store.run_snapshots
    assert "first interrupted task" not in "\n".join(user_prompts(second_snapshot))
    assert set(snapshot_payload(store, second_workflow)["covers"]) == {second_workflow}

    third = make_event(
        event_id="Ev-snapshot-c",
        ts="1700000000.000400",
        thread_ts=root,
        text="<@UBOT> third recovery attempt",
    )
    await store.append_event(third)
    await fail_after_tool(third, "result-c")
    third_workflow = run_workflow_id(key, third.event_id)
    third_snapshot = snapshot_messages(store, third_workflow)

    assert any("third recovery attempt" in prompt for prompt in user_prompts(third_snapshot[:1]))
    assert "first interrupted task" not in "\n".join(user_prompts(third_snapshot))
    assert "second recovery attempt" not in "\n".join(user_prompts(third_snapshot))
    assert set(snapshot_payload(store, third_workflow)["covers"]) == {third_workflow}

    seen: list[list[ModelMessage]] = []

    async def succeeding_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(deepcopy(messages))
        return ModelResponse(parts=[TextPart("All snapshots adopted.")])

    fourth = make_event(
        event_id="Ev-snapshot-d",
        ts="1700000000.000500",
        thread_ts=root,
        text="<@UBOT> finish recovery",
    )
    await store.append_event(fourth)
    await drive(build(FunctionModel(succeeding_model), store, slack), fourth)

    expected = [
        *first_snapshot,
        *second_snapshot,
        *third_snapshot,
    ]
    expected_parts = [part for message in expected for part in message.parts]
    actual_parts = [part for message in seen[0] for part in message.parts]
    # FunctionModel sees the wire view, where adjacent requests are coalesced.
    # Part order remains the durable composition boundary.
    assert actual_parts[: len(expected_parts)] == expected_parts
    assert len(actual_parts) == len(expected_parts) + 1
    assert "\n".join(user_prompts(seen[0])).count("first interrupted task") == 1
    assert "\n".join(user_prompts(seen[0])).count("second recovery attempt") == 1
    assert "\n".join(user_prompts(seen[0])).count("third recovery attempt") == 1
    assert store.run_snapshots == {}
    assert PendingFailure.CONTEXT_KEY not in store.sessions[key].context

    saved = ModelMessagesTypeAdapter.validate_python(
        [row[3] for row in store.session_messages[key]]
    )
    saved_prompts = "\n".join(user_prompts(saved))
    assert saved_prompts.count("first interrupted task") == 1
    assert saved_prompts.count("second recovery attempt") == 1
    assert saved_prompts.count("third recovery attempt") == 1


async def test_snapshot_budget_adopts_newest_and_demotes_older_failure_to_bare() -> None:
    store, slack = FakeStore(), FakeSlack()
    root = "1700000000.000100"
    oldest = make_event(
        event_id="Ev-budget-old",
        ts="1700000000.000200",
        thread_ts=root,
        text="<@UBOT> oldest failed trigger",
    )
    key = SessionKey.from_event(oldest)
    saved_history: list[ModelMessage] = [_user_turn("saved turn"), _answer("saved answer")]
    await store.save_session(Session(key=key))
    await store.append_session_messages(
        key,
        run_id="saved-run",
        messages=cast(
            "list[dict[str, Any]]",
            ModelMessagesTypeAdapter.dump_python(saved_history, mode="json"),
        ),
    )

    async def fail_after_tool(event: Event, result: str) -> None:
        calls = 0

        async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                return ModelResponse(
                    parts=[ToolCallPart("work", {}, tool_call_id=f"{event.event_id}-call")]
                )
            raise RuntimeError("died after tool")

        conversation = make_conversation(slack)
        agent: Agent[None, str] = Agent(
            FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
        )

        @agent.tool_plain
        async def work() -> str:
            return result

        await drive(make_runner(agent, store, slack, conversation, runtime=FAST), event)

    await store.append_event(oldest)
    await fail_after_tool(oldest, "old snapshot content")

    newest = make_event(
        event_id="Ev-budget-new",
        ts="1700000000.000300",
        thread_ts=root,
        text="<@UBOT> newest failed trigger",
    )
    await store.append_event(newest)
    await fail_after_tool(newest, "new snapshot content")

    seen: list[list[ModelMessage]] = []

    async def succeeding_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(deepcopy(messages))
        return ModelResponse(parts=[TextPart("Recovered within budget.")])

    current = make_event(
        event_id="Ev-budget-current",
        ts="1700000000.000400",
        thread_ts=root,
        text="<@UBOT> recover within budget",
    )
    await store.append_event(current)
    runtime = RuntimeConfig(max_history_messages=2, debounce_period=0)
    await drive(build(FunctionModel(succeeding_model), store, slack, runtime=runtime), current)

    visible_prompts = "\n".join(user_prompts(seen[0]))
    assert visible_prompts.count("oldest failed trigger") == 1
    assert visible_prompts.count("newest failed trigger") == 1
    assert "saved turn" not in visible_prompts
    tool_returns = [
        part.content
        for message in seen[0]
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert "old snapshot content" not in tool_returns
    assert "new snapshot content" in tool_returns
    assert store.run_snapshots == {}
    assert PendingFailure.CONTEXT_KEY not in store.sessions[key].context

    saved_current = ModelMessagesTypeAdapter.validate_python(
        [
            message
            for run_id, _, _, message in store.session_messages[key]
            if run_id == current.event_id
        ]
    )
    saved_returns = [
        part.content
        for message in saved_current
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert saved_returns.count("new snapshot content") == 1
    assert "old snapshot content" not in saved_returns


async def test_snapshot_adoption_orders_by_created_at_not_pending_failed_at() -> None:
    store, slack = FakeStore(), FakeSlack()
    first = make_event(
        event_id="Ev-created-first",
        ts="1700000000.000200",
        thread_ts="1700000000.000100",
        text="<@UBOT> first pending trigger",
    )
    second = make_event(
        event_id="Ev-created-second",
        ts="1700000000.000300",
        thread_ts=first.thread_ts,
        text="<@UBOT> second pending trigger",
    )
    key = SessionKey.from_event(first)
    first_workflow = run_workflow_id(key, first.event_id)
    second_workflow = run_workflow_id(key, second.event_id)
    for event in (first, second):
        await store.append_event(event)
    await store.ensure_session(key)
    await store.merge_context_key(
        key,
        PendingFailure.CONTEXT_KEY,
        {
            **PendingFailure(
                workflow_id=first_workflow,
                event_id=first.event_id,
                reason="failed",
                failed_at="2026-07-17T00:00:02+00:00",
            ).to_patch(),
            **PendingFailure(
                workflow_id=second_workflow,
                event_id=second.event_id,
                reason="failed",
                failed_at="2026-07-17T00:00:01+00:00",
            ).to_patch(),
        },
    )
    dumped_first = cast(
        "list[dict[str, Any]]",
        ModelMessagesTypeAdapter.dump_python(
            [_user_turn("first snapshot"), _answer("first result")], mode="json"
        ),
    )
    dumped_second = cast(
        "list[dict[str, Any]]",
        ModelMessagesTypeAdapter.dump_python(
            [_user_turn("second snapshot"), _answer("second result")], mode="json"
        ),
    )
    await store.save_run_snapshot(
        first_workflow,
        key,
        {
            "messages": dumped_first,
            "covers": [first_workflow],
            "delivered_steer_ids": [],
        },
    )
    await store.save_run_snapshot(
        second_workflow,
        key,
        {
            "messages": dumped_second,
            "covers": [second_workflow],
            "delivered_steer_ids": [],
        },
    )
    first_row = store.run_snapshots[first_workflow]
    second_row = store.run_snapshots[second_workflow]
    store.run_snapshots[first_workflow] = (*first_row[:3], datetime(2026, 7, 17, tzinfo=UTC))
    store.run_snapshots[second_workflow] = (
        *second_row[:3],
        datetime(2026, 7, 17, 0, 0, 1, tzinfo=UTC),
    )
    pending = PendingFailure.list_from_context(store.sessions[key].context)
    assert [failure.workflow_id for failure in pending] == [second_workflow, first_workflow]

    seen: list[list[ModelMessage]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(deepcopy(messages))
        return ModelResponse(parts=[TextPart("Ordered by creation.")])

    current = make_event(
        event_id="Ev-created-current",
        ts="1700000000.000400",
        thread_ts=first.thread_ts,
        text="<@UBOT> recover in execution order",
    )
    await store.append_event(current)
    await drive(build(FunctionModel(model_fn), store, slack), current)

    assert user_prompts(seen[0])[:2] == ["first snapshot", "second snapshot"]


async def test_recorded_snapshot_decision_survives_payload_deletion_on_replay() -> None:
    store, slack = FakeStore(), FakeSlack()
    failed = make_event(
        event_id="Ev-replay-failed",
        ts="1700000000.000200",
        thread_ts="1700000000.000100",
        text="<@UBOT> replay-stable failed trigger",
    )
    key = SessionKey.from_event(failed)
    workflow_id = run_workflow_id(key, failed.event_id)
    await store.append_event(failed)
    await store.ensure_session(key)
    await store.merge_context_key(
        key,
        PendingFailure.CONTEXT_KEY,
        PendingFailure(
            workflow_id=workflow_id,
            event_id=failed.event_id,
            reason="failed",
            failed_at="2026-07-17T00:00:00+00:00",
        ).to_patch(),
    )
    await store.save_run_snapshot(
        workflow_id,
        key,
        {
            "messages": ModelMessagesTypeAdapter.dump_python(
                [_user_turn("replay-stable failed trigger"), _answer("unfinished answer")],
                mode="json",
            ),
            "covers": [workflow_id],
            "delivered_steer_ids": [],
        },
    )
    slack.thread_messages = [
        ChatMessage(author="U07", ref="1700000000.000150", text="must not backfill")
    ]

    original_get = store.get_run_snapshots
    payload_reads = 0

    async def delete_before_plain_read(
        workflow_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        nonlocal payload_reads
        payload_reads += 1
        if payload_reads == 1:
            return await original_get(workflow_ids)
        store.run_snapshots.clear()
        return {}

    store.get_run_snapshots = delete_before_plain_read  # type: ignore[method-assign]
    seen: list[list[str]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Replay completed.")])

    current = make_event(
        event_id="Ev-replay-current",
        ts="1700000000.000300",
        thread_ts=failed.thread_ts,
        text="<@UBOT> continue replay",
    )
    await store.append_event(current)
    await drive(build(FunctionModel(model_fn), store, slack), current)

    assert payload_reads == 2
    prompt = seen[0][-1]
    assert "replay-stable failed trigger" not in prompt
    assert "must not backfill" not in prompt
    assert "includes its unfinished work" in prompt
    assert "work was lost" not in prompt


async def test_empty_snapshot_falls_back_to_its_pending_trigger() -> None:
    store, slack = FakeStore(), FakeSlack()
    failed = make_event(
        event_id="Ev-empty-snapshot",
        ts="1700000000.000200",
        thread_ts="1700000000.000100",
        text="<@UBOT> do not lose this trigger",
    )
    key = SessionKey.from_event(failed)
    workflow_id = run_workflow_id(key, failed.event_id)
    await store.append_event(failed)
    await store.ensure_session(key)
    await store.merge_context_key(
        key,
        PendingFailure.CONTEXT_KEY,
        PendingFailure(
            workflow_id=workflow_id,
            event_id=failed.event_id,
            reason="failed",
            failed_at="2026-07-17T00:00:00+00:00",
        ).to_patch(),
    )
    await store.save_run_snapshot(
        workflow_id,
        key,
        {"messages": [], "covers": [workflow_id], "delivered_steer_ids": []},
    )
    seen: list[list[str]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Recovered the bare trigger.")])

    current = make_event(
        event_id="Ev-after-empty-snapshot",
        ts="1700000000.000300",
        thread_ts=failed.thread_ts,
        text="<@UBOT> retry",
    )
    await store.append_event(current)
    await drive(build(FunctionModel(model_fn), store, slack), current)

    prompt = seen[0][-1]
    assert prompt.count("do not lose this trigger") == 1
    assert "external actions may already have completed" in prompt
    assert "Do not repeat actions" in prompt
    assert workflow_id not in store.run_snapshots
    assert PendingFailure.CONTEXT_KEY not in store.sessions[key].context


async def test_a_saved_history_that_ends_in_a_request_keeps_every_message_at_adoption() -> None:
    # A run that ends with no_reply saves a history that ends in its tool
    # return. pydantic-ai merges it with the adopted snapshot's first request.
    store, slack = FakeStore(), FakeSlack()
    failed = make_event(
        event_id="Ev-seam-failed",
        ts="1700000000.000200",
        thread_ts="1700000000.000100",
        text="<@UBOT> adopted snapshot prompt",
    )
    key = SessionKey.from_event(failed)
    workflow_id = run_workflow_id(key, failed.event_id)
    await store.save_session(Session(key=key))
    await store.append_session_messages(
        key,
        run_id="broken-saved-run",
        messages=cast(
            "list[dict[str, Any]]",
            ModelMessagesTypeAdapter.dump_python([_user_turn("broken saved tail")], mode="json"),
        ),
    )
    await store.append_event(failed)
    await store.merge_context_key(
        key,
        PendingFailure.CONTEXT_KEY,
        PendingFailure(
            workflow_id=workflow_id,
            event_id=failed.event_id,
            reason="failed",
            failed_at="2026-07-17T00:00:00+00:00",
        ).to_patch(),
    )
    await store.save_run_snapshot(
        workflow_id,
        key,
        {
            "messages": ModelMessagesTypeAdapter.dump_python(
                [_user_turn("adopted snapshot prompt"), _answer("adopted response")],
                mode="json",
            ),
            "covers": [workflow_id],
            "delivered_steer_ids": [],
        },
    )
    seen: list[list[str]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Seam recovered.")])

    current = make_event(
        event_id="Ev-seam-current",
        ts="1700000000.000300",
        thread_ts=failed.thread_ts,
        text="<@UBOT> continue after seam",
    )
    await store.append_event(current)
    await drive(build(FunctionModel(model_fn), store, slack), current)

    prompts = "\n".join(seen[0])
    assert prompts.count("broken saved tail") == 1
    assert prompts.count("adopted snapshot prompt") == 1
    stored = ModelMessagesTypeAdapter.validate_python(await store.get_session_messages(key))
    assert [
        part.content
        for message in stored
        for part in message.parts
        if isinstance(part, UserPromptPart | TextPart)
    ] == [
        "broken saved tail",
        "adopted snapshot prompt",
        "adopted response",
        "[runtime] A previous run in this thread was interrupted; the conversation above "
        "includes its unfinished work.\n\n"
        "[2023-11-14 22:13 UTC] jane: @Hondo continue after seam",
        "Seam recovered.",
    ]


async def test_snapshot_covers_bare_failure_rows_rendered_by_a_dying_recovery() -> None:
    store, slack = FakeStore(), FakeSlack()
    root = "1700000000.000100"
    bare = make_event(
        event_id="Ev-bare-a",
        ts="1700000000.000200",
        thread_ts=root,
        text="<@UBOT> bare failure trigger",
    )
    key = SessionKey.from_event(bare)
    await store.append_event(bare)

    async def no_response(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("died before response")

    await drive(build(FunctionModel(no_response), store, slack), bare)
    bare_workflow = run_workflow_id(key, bare.event_id)
    assert bare_workflow not in store.run_snapshots

    slack.thread_messages.clear()
    recovery = make_event(
        event_id="Ev-covered-b",
        ts="1700000000.000300",
        thread_ts=root,
        text="<@UBOT> recovery that also dies",
    )
    await store.append_event(recovery)
    calls = 0

    async def dying_recovery(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(parts=[ToolCallPart("work", {}, tool_call_id="cover-call")])
        raise RuntimeError("recovery died")

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(dying_recovery), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    async def work() -> str:
        return "covered work"

    await drive(make_runner(agent, store, slack, conversation, runtime=FAST), recovery)
    recovery_workflow = run_workflow_id(key, recovery.event_id)
    assert set(snapshot_payload(store, recovery_workflow)["covers"]) == {
        bare_workflow,
        recovery_workflow,
    }

    slack.thread_messages = [
        ChatMessage(author="U07", ref="1700000000.000250", text="backfill must be skipped")
    ]
    seen: list[list[ModelMessage]] = []

    async def final_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(deepcopy(messages))
        return ModelResponse(parts=[TextPart("Recovered.")])

    current = make_event(
        event_id="Ev-covered-c",
        ts="1700000000.000400",
        thread_ts=root,
        text="<@UBOT> continue after both",
    )
    await store.append_event(current)
    await drive(build(FunctionModel(final_model), store, slack), current)

    current_prompt = user_prompts(seen[0])[-1]
    assert "bare failure trigger" not in current_prompt
    assert "backfill must be skipped" not in current_prompt
    assert "Earlier messages in this thread:" not in current_prompt
    assert "\n".join(user_prompts(seen[0])).count("bare failure trigger") == 1


async def test_claimed_but_not_enqueued_steer_is_recovered() -> None:
    store, slack = FakeStore(), FakeSlack()
    failed = make_event(event_id="Ev-undelivered-run", text="<@UBOT> begin work")
    key = SessionKey.from_event(failed)
    steer = make_event(
        event_id="Ev-undelivered-steer",
        kind="message",
        ts="1700000000.000200",
        thread_ts=failed.ts,
        text="claimed but never delivered",
        mentions_bot=False,
    )
    await store.append_event(failed)
    await store.append_event(steer)
    calls = 0

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            mail = await store.insert_mail(key, steer, SteerPriority.ASAP, target_workflow_id=None)
            assert mail is not None
            return ModelResponse(parts=[ToolCallPart("work", {}, tool_call_id="undelivered-call")])
        return ModelResponse(parts=[TextPart("should not reach the model again")])

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    async def work() -> str:
        return "ready"

    runner = make_runner(agent, store, slack, conversation, runtime=FAST)
    original_extend = runner._extend_roster

    async def fail_before_enqueue(
        roster: dict[str, str], authors: list[str | None], texts: list[str], seq: Any
    ) -> None:
        if "claimed but never delivered" in texts:
            raise RuntimeError("died between claim and enqueue")
        await original_extend(roster, authors, texts, seq)

    runner._extend_roster = fail_before_enqueue  # type: ignore[method-assign]
    await drive(runner, failed)

    workflow_id = run_workflow_id(key, failed.event_id)
    mail = next(mail for mail in store.mail.values() if mail.event_id == steer.event_id)
    store.claims[f"{workflow_id}:claim_steers"] = {mail.id}
    assert snapshot_payload(store, workflow_id)["delivered_steer_ids"] == []

    slack.thread_messages.clear()
    seen: list[list[str]] = []

    async def recovery_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Steer recovered.")])

    current = make_event(
        event_id="Ev-undelivered-current",
        ts="1700000000.000300",
        thread_ts=failed.ts,
        text="<@UBOT> retry",
    )
    await store.append_event(current)
    await drive(build(FunctionModel(recovery_model), store, slack), current)

    assert "claimed but never delivered" in seen[0][-1]


async def test_steer_redispatched_as_its_own_run_is_not_duplicated_on_recovery() -> None:
    # Live-test finding (2026-07-16): a stranded steer redispatched as its own
    # run appears twice to the recovering model — once inside that run's
    # snapshot (as its trigger prompt) and once re-rendered from the mail
    # ledger, because delivered_steer_ids only tracks run.enqueue deliveries.
    store, slack = FakeStore(), FakeSlack()
    first = make_event(event_id="Ev-trigger-run", text="<@UBOT> first ask")
    key = SessionKey.from_event(first)
    steer = make_event(
        event_id="Ev-steer-run",
        kind="message",
        ts="1700000000.000200",
        thread_ts=first.ts,
        text="also handle the steer",
        mentions_bot=False,
    )

    async def dies_mid_tool(event: Event) -> None:
        calls = 0

        async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                return ModelResponse(
                    parts=[ToolCallPart("work", {}, tool_call_id=f"{event.event_id}-call")]
                )
            raise RuntimeError("died mid-run")

        conversation = make_conversation(slack)
        agent: Agent[None, str] = Agent(
            FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
        )

        @agent.tool_plain
        async def work() -> str:
            return "partial"

        await drive(make_runner(agent, store, slack, conversation, runtime=FAST), event)

    await store.append_event(first)
    await dies_mid_tool(first)

    # The stranded steer is redispatched as its own run (the reconciler path):
    # the same event is that run's trigger and its claim_mailbox-claimed mail.
    await store.append_event(steer)
    mail = await store.insert_mail(key, steer, SteerPriority.WHEN_IDLE, target_workflow_id=None)
    assert mail is not None
    await dies_mid_tool(steer)
    steer_workflow = run_workflow_id(key, steer.event_id)
    claimed = await store.mail_for_event(steer.event_id)
    assert claimed is not None and claimed.consumed
    # Outside a workflow claim tokens are None, so attribute the claim to the
    # steer's run by hand — live, claim_mailbox stamps this claimed_by itself.
    store.claims[f"{steer_workflow}:claim_mailbox"] = {mail.id}
    assert steer_workflow in store.run_snapshots

    seen: list[list[str]] = []

    async def recovery_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("Done.")])

    current = make_event(
        event_id="Ev-continue",
        ts="1700000000.000300",
        thread_ts=first.ts,
        text="<@UBOT> continue",
    )
    await store.append_event(current)
    await drive(build(FunctionModel(recovery_model), store, slack), current)

    assert "\n".join(seen[0]).count("also handle the steer") == 1
    assert store.run_snapshots == {}


async def test_enqueued_steer_is_recorded_as_delivered_and_not_recovered_twice() -> None:
    store, slack = FakeStore(), FakeSlack()
    failed = make_event(event_id="Ev-delivered-run", text="<@UBOT> begin work")
    key = SessionKey.from_event(failed)
    steer = make_event(
        event_id="Ev-delivered-steer",
        kind="message",
        ts="1700000000.000200",
        thread_ts=failed.ts,
        text="delivered steer detail",
        mentions_bot=False,
    )
    await store.append_event(failed)
    await store.append_event(steer)
    calls = 0

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            mail = await store.insert_mail(key, steer, SteerPriority.ASAP, target_workflow_id=None)
            assert mail is not None
            return ModelResponse(parts=[ToolCallPart("work", {}, tool_call_id="delivered-call")])
        assert any("delivered steer detail" in prompt for prompt in user_prompts(messages))
        raise RuntimeError("died after steer delivery")

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    async def work() -> str:
        return "ready"

    await drive(make_runner(agent, store, slack, conversation, runtime=FAST), failed)

    workflow_id = run_workflow_id(key, failed.event_id)
    mail = next(mail for mail in store.mail.values() if mail.event_id == steer.event_id)
    store.claims[f"{workflow_id}:claim_steers"] = {mail.id}
    assert snapshot_payload(store, workflow_id)["delivered_steer_ids"] == [mail.id]

    slack.thread_messages.clear()
    seen: list[list[str]] = []

    async def recovery_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(user_prompts(messages))
        return ModelResponse(parts=[TextPart("No duplicate steer.")])

    current = make_event(
        event_id="Ev-delivered-current",
        ts="1700000000.000300",
        thread_ts=failed.ts,
        text="<@UBOT> retry",
    )
    await store.append_event(current)
    await drive(build(FunctionModel(recovery_model), store, slack), current)

    assert "delivered steer detail" not in seen[0][-1]
    assert "\n".join(seen[0]).count("delivered steer detail") == 1


async def test_large_tool_returns_remain_value_faithful_through_adoption() -> None:
    store, slack = FakeStore(), FakeSlack()
    root = "1700000000.000100"
    huge_string = "x" * 60_000
    structured = {
        "items": [{"id": index, "payload": "y" * 100} for index in range(1_000)],
        "complete": True,
    }

    async def fail_after_tool(event: Event, result: Any) -> None:
        calls = 0

        async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                return ModelResponse(
                    parts=[ToolCallPart("large", {}, tool_call_id=f"{event.event_id}-call")]
                )
            raise RuntimeError("stop after tool")

        conversation = make_conversation(slack)
        agent: Agent[None, str] = Agent(
            FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
        )

        @agent.tool_plain
        async def large() -> Any:
            return result

        await drive(make_runner(agent, store, slack, conversation, runtime=FAST), event)

    string_event = make_event(
        event_id="Ev-large-string",
        ts="1700000000.000200",
        thread_ts=root,
        text="<@UBOT> preserve the large string",
    )
    structured_event = make_event(
        event_id="Ev-large-structured",
        ts="1700000000.000300",
        thread_ts=root,
        text="<@UBOT> preserve the structured result",
    )
    key = SessionKey.from_event(string_event)
    await store.append_event(string_event)
    await fail_after_tool(string_event, huge_string)
    await store.append_event(structured_event)
    await fail_after_tool(structured_event, structured)

    def tool_return_contents(messages: list[ModelMessage]) -> list[Any]:
        return [
            part.content
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]

    string_workflow = run_workflow_id(key, string_event.event_id)
    structured_workflow = run_workflow_id(key, structured_event.event_id)
    assert tool_return_contents(snapshot_messages(store, string_workflow)) == [huge_string]
    assert tool_return_contents(snapshot_messages(store, structured_workflow)) == [structured]

    seen: list[list[ModelMessage]] = []

    async def recovery_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(deepcopy(messages))
        return ModelResponse(parts=[TextPart("Large values recovered.")])

    current = make_event(
        event_id="Ev-large-current",
        ts="1700000000.000400",
        thread_ts=root,
        text="<@UBOT> continue with both values",
    )
    await store.append_event(current)
    await drive(build(FunctionModel(recovery_model), store, slack), current)

    adopted_returns = tool_return_contents(seen[0])
    assert huge_string in adopted_returns
    assert structured in adopted_returns


async def test_dead_run_guard_skips_posts_claims_save_and_snapshot_deletion() -> None:
    store, slack = FakeStore(), FakeSlack()
    event = make_event(event_id="Ev-zombie", text="<@UBOT> abandoned run")
    key = SessionKey.from_event(event)
    failed_workflow = run_workflow_id(key, "Ev-before-zombie")
    await store.save_run_snapshot(
        failed_workflow,
        key,
        {
            "messages": ModelMessagesTypeAdapter.dump_python(
                [_user_turn("unfinished predecessor"), _answer("partial answer")], mode="json"
            ),
            "covers": [failed_workflow],
            "delivered_steer_ids": [],
        },
    )
    pending = [
        PendingFailure(
            workflow_id=failed_workflow,
            event_id="Ev-before-zombie",
            reason="failed",
            failed_at="2026-07-17T00:00:00+00:00",
        )
    ]
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
    }

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("work", {}, tool_call_id="zombie-call")])

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    async def work() -> str:
        state["dead"] = True
        return "done"

    runner = make_runner(agent, store, slack, conversation, runtime=FAST)

    async def forbidden_claim(key: SessionKey, name: str) -> list[dict[str, Any]]:
        raise AssertionError("dead run claimed mail")

    async def fixed_prompt(*args: Any, **kwargs: Any) -> tuple[str, list[str]]:
        return "prompt", []

    async def forbidden_save(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("dead run saved the session")

    runner._claim_mail = forbidden_claim  # type: ignore[method-assign]
    runner._build_prompt = fixed_prompt  # type: ignore[method-assign]
    runner._save_session = forbidden_save  # type: ignore[method-assign]
    progress = ProgressReporter(slack, channel="C01", thread_ts="1700000000.000100")
    cancelled = False
    try:
        await runner._drive_body(
            key,
            event,
            None,
            progress,
            state,
            {},
            pending,
            None,
        )
    except asyncio.CancelledError:
        cancelled = True

    assert cancelled
    assert slack.posted == []
    assert store.saved_sessions == []
    assert failed_workflow in store.run_snapshots


async def test_stall_watchdog_persists_a_partial_snapshot() -> None:
    store, slack = FakeStore(), FakeSlack()
    event = make_event(event_id="Ev-watchdog-snapshot")
    key = SessionKey.from_event(event)
    await store.append_event(event)

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[ToolCallPart("hang", {}, tool_call_id="watchdog-call")])

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    async def hang() -> str:
        await asyncio.Event().wait()
        return "never"

    runtime = RuntimeConfig(stall_timeout=0.4, run_timeout=0.5)
    await drive(make_runner(agent, store, slack, conversation, runtime=runtime), event)

    assert run_workflow_id(key, event.event_id) in store.run_snapshots
    assert "isn't responding" in slack.posted[-1]["text"]


async def test_stall_watchdog_spares_a_run_that_keeps_working_without_posting() -> None:
    store, slack = FakeStore(), FakeSlack()
    event = make_event(event_id="Ev-working")
    await store.append_event(event)

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        done = sum(
            isinstance(part, ToolReturnPart)
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
        )
        if done < 4:
            return ModelResponse(parts=[ToolCallPart("work", {}, tool_call_id=f"work-{done}")])
        return ModelResponse(parts=[TextPart("Done.")])

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    async def work() -> str:
        await asyncio.sleep(0.2)
        return "ok"

    runtime = RuntimeConfig(stall_timeout=0.4, run_timeout=5)
    await drive(make_runner(agent, store, slack, conversation, runtime=runtime), event)

    assert [m["text"] for m in slack.posted] == ["Done."]


async def test_run_timeout_after_narration_persists_a_partial_snapshot() -> None:
    store, slack = FakeStore(), FakeSlack()
    event = make_event(event_id="Ev-timeout-snapshot")
    key = SessionKey.from_event(event)
    await store.append_event(event)

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(
            parts=[
                TextPart("Started before hanging."),
                ToolCallPart("hang", {}, tool_call_id="timeout-call"),
            ]
        )

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    async def hang() -> str:
        await asyncio.Event().wait()
        return "never"

    runtime = RuntimeConfig(stall_timeout=0.4, run_timeout=0.5)
    await drive(make_runner(agent, store, slack, conversation, runtime=runtime), event)

    assert run_workflow_id(key, event.event_id) in store.run_snapshots
    assert [message["text"] for message in slack.posted[:2]] == [
        "Started before hanging.",
        "⚠️ This run hit its time limit (0s) and was stopped. Mention Hondo again to retry.",
    ]


def test_make_event_defaults_are_a_mention() -> None:
    event = make_event()
    assert event.mentions_bot and not event.is_dm


async def test_successful_run_records_its_usage() -> None:
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("All good.")])

    event = make_event()
    key = SessionKey.from_event(event)
    await drive(build(FunctionModel(model_fn), store, slack), event)

    workflow_id = run_workflow_id(key, event.event_id)
    assert len(store.usage_events) == 1
    ((slot, row),) = store.usage_events.items()
    assert slot[0] == workflow_id
    assert slot[1] == "model_request"
    # FunctionModel responses carry no provider_name; the raw-value fallback applies.
    assert row["provider"] == "unknown"
    assert row["team"] == key.team
    assert row["channel"] == key.channel
    assert row["thread_root"] == key.thread_root
    assert row["quantity_in"] > 0
    assert row["quantity_out"] > 0
    assert row["metadata"] == {"requests": 1}


async def test_usage_counts_only_this_runs_own_responses() -> None:
    """Loaded history from an earlier run must not be re-counted (run_id filter)."""
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Answered.")])

    first = make_event(event_id="Ev-first", ts="1700000000.000100")
    key = SessionKey.from_event(first)
    await drive(build(FunctionModel(model_fn), store, slack), first)

    second = make_event(event_id="Ev-second", ts="1700000000.000200", thread_ts=first.ts)
    await drive(build(FunctionModel(model_fn), store, slack), second)

    # One row per workflow; the second run loaded the first run's response as
    # history but counted only its own single request.
    assert len(store.usage_events) == 2
    for row in store.usage_events.values():
        assert row["metadata"] == {"requests": 1}
    assert {slot[0] for slot in store.usage_events} == {
        run_workflow_id(key, first.event_id),
        run_workflow_id(key, second.event_id),
    }


async def test_failed_run_records_its_partial_usage() -> None:
    """Failed runs still record model usage."""
    store, slack = FakeStore(), FakeSlack()
    event = make_event()
    key = SessionKey.from_event(event)
    await store.append_event(event)
    model_calls = 0

    async def failing_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            return ModelResponse(parts=[ToolCallPart("lookup", {}, tool_call_id="dead-lookup")])
        raise RuntimeError("model died after the tool returned")

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(failing_model), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    async def lookup() -> str:
        return "resource42"

    await drive(make_runner(agent, store, slack, conversation, runtime=FAST), event)

    workflow_id = run_workflow_id(key, event.event_id)
    assert len(store.usage_events) == 1
    ((slot, row),) = store.usage_events.items()
    assert slot[0] == workflow_id
    assert row["metadata"] == {"requests": 1}
    assert row["quantity_in"] > 0


async def test_failure_before_any_response_records_no_usage() -> None:
    store, slack = FakeStore(), FakeSlack()

    async def failing_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        raise RuntimeError("boom")

    event = make_event()
    await store.append_event(event)
    await drive(build(FunctionModel(failing_model), store, slack), event)

    assert store.usage_events == {}


async def test_usage_write_failure_after_save_does_not_fail_the_run() -> None:
    """record_usage dying after save_session must never reach failure handling:
    the messages are already saved, so a failure snapshot would hand the next
    run duplicate history — and the user would see a failure notice for a run
    that delivered its answer."""

    class UsageDownStore(FakeStore):
        async def record_usage(self, rows: Sequence[dict[str, Any]]) -> None:
            raise RuntimeError("usage insert failed")

    store, slack = UsageDownStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("All good.")])

    event = make_event()
    key = SessionKey.from_event(event)
    output = await drive(build(FunctionModel(model_fn), store, slack), event)

    assert output == "All good."
    # The answer stands alone: no failure notice followed it.
    assert [m["text"] for m in slack.posted] == ["All good."]
    session = store.sessions[key]
    assert session.state is SessionState.IDLE
    assert await store.get_session_messages(key)
    assert PendingFailure.CONTEXT_KEY not in session.context
    assert store.run_snapshots == {}
    assert store.usage_events == {}


async def test_resumed_run_success_write_overwrites_the_failure_usage_row() -> None:
    """Failure-then-resume converges on one usage row: the failure path's
    partial write and the resumed run's success write share the same
    (workflow_id, kind, resource) key, so the success numbers replace the
    partial ones — no duplicate row, no summing.

    Outside DBOS drive() is a plain coroutine, so the resume is driving the
    same event again — the same deterministic workflow id, as a hand-resumed
    MAX_RECOVERY workflow re-runs. Recorded-step replay through real DBOS is
    covered by the crash tests in test_tool_boundary.py.
    """
    store, slack = FakeStore(), FakeSlack()
    event = make_event()
    key = SessionKey.from_event(event)
    await store.append_event(event)
    model_calls = 0

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        # Attempt one: a tool call, then death. The resume: a tool call, then the answer.
        if model_calls in (1, 3):
            return ModelResponse(parts=[ToolCallPart("lookup", {}, tool_call_id=f"c{model_calls}")])
        if model_calls == 2:
            raise RuntimeError("model died after the tool returned")
        return ModelResponse(parts=[TextPart("Recovered.")])

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn), capabilities=[ToolBoundary(), conversation]
    )

    @agent.tool_plain
    async def lookup() -> str:
        return "resource42"

    runner = make_runner(agent, store, slack, conversation, runtime=FAST)
    await drive(runner, event)

    workflow_id = run_workflow_id(key, event.event_id)
    ((slot, partial),) = store.usage_events.items()
    assert slot[0] == workflow_id
    assert partial["metadata"] == {"requests": 1}

    await drive(runner, event)

    ((final_slot, final),) = store.usage_events.items()
    assert final_slot == slot
    assert final["metadata"] == {"requests": 2}
    assert final["quantity_in"] > partial["quantity_in"]
    assert final["quantity_out"] >= partial["quantity_out"]


async def test_every_channel_run_starts_the_thread_summary_when_it_ends() -> None:
    store, slack = FakeStore(), FakeSlack()
    started: list[tuple[SessionKey, str]] = []

    async def start_summary(key: SessionKey, event_id: str) -> None:
        started.append((key, event_id))

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        prompt = user_prompts(messages)[-1]
        if "boom" in prompt:
            raise RuntimeError("model exploded")
        return no_reply() if "lunch" in prompt else ModelResponse(parts=[TextPart("Done.")])

    runner = build(FunctionModel(model_fn), store, slack, start_summary=start_summary)
    answered = make_event(event_id="Ev1")
    silent = make_event(
        event_id="Ev2",
        kind="message",
        ts="1700000000.000200",
        thread_ts="1700000000.000100",
        text="lunch?",
        mentions_bot=False,
    )
    failed = make_event(
        event_id="Ev3",
        ts="1700000000.000300",
        thread_ts="1700000000.000100",
        text="<@UBOT> boom",
    )
    direct = make_event(event_id="Ev4", channel="D01", is_dm=True, mentions_bot=False)
    for event in (answered, silent, failed, direct):
        await drive(runner, event)

    key = SessionKey.from_event(answered)
    assert started == [(key, "Ev1"), (key, "Ev2"), (key, "Ev3")]
    assert [m["text"] for m in slack.posted if m["blocks"] is None] == ["Done.", "Done."]


async def test_a_message_that_already_reached_a_run_starts_no_summary() -> None:
    store, slack = FakeStore(), FakeSlack()
    started: list[str] = []

    async def start_summary(key: SessionKey, event_id: str) -> None:
        started.append(event_id)

    runner = build(TestModel(), store, slack, start_summary=start_summary)
    event = make_event()
    mail = await store.insert_mail(
        SessionKey.from_event(event), event, SteerPriority.ASAP, target_workflow_id=None
    )
    assert mail is not None
    await store.consume_mail([mail.id])

    assert await drive(runner, event) == ""
    assert started == []


async def test_a_summary_that_does_not_start_leaves_the_run_answered() -> None:
    store, slack = FakeStore(), FakeSlack()

    async def start_summary(key: SessionKey, event_id: str) -> None:
        raise RuntimeError("workflow table unavailable")

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return ModelResponse(parts=[TextPart("Done.")])

    runner = build(FunctionModel(model_fn), store, slack, start_summary=start_summary)

    assert await drive(runner, make_event()) == "Done."
    assert [m["text"] for m in slack.posted] == ["Done."]


async def wait_until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(2):
        while not condition():
            await asyncio.sleep(0.005)


async def test_mention_run_shows_a_working_status_until_the_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(progress_module, "STATUS_REFRESH_SECONDS", 0.01)
    store, slack = FakeStore(), FakeSlack()
    calls = 0

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            # A mention shows the status before Hondo says anything.
            await wait_until(lambda: bool(slack.statuses))
            todo = {
                "content": "Check the deploy log",
                "status": "in_progress",
                "active_form": "Checking the deploy log",
            }
            return ModelResponse(
                parts=[TextPart("on it"), ToolCallPart("write_todos", {"todos": [todo]})]
            )
        await wait_until(lambda: any(s["text"] and s["posts"] == 2 for s in slack.statuses))
        return ModelResponse(parts=[TextPart("The deploy is healthy.")])

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn),
        capabilities=[ToolBoundary(), conversation, TodosCapability(slack)],
    )
    await drive(make_runner(agent, store, slack, conversation, runtime=FAST), make_event())

    assert [m["text"] for m in slack.posted] == [
        "on it",
        "→ *Checking the deploy log…*",
        "The deploy is healthy.",
    ]
    assert slack.statuses[0] == {
        "channel": "C01",
        "thread_ts": "1700000000.000100",
        "text": "is working…",
        "posts": 0,
    }
    # The task card's post clears the status; it comes back after it.
    assert any(s["text"] and s["posts"] == 2 for s in slack.statuses)
    # Nothing says "working" below the answer, and the run ends with the status cleared.
    assert all(s["posts"] < 3 for s in slack.statuses if s["text"])
    assert slack.statuses[-1]["text"] == ""


async def test_unaddressed_run_shows_the_status_once_it_uses_a_tool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(progress_module, "STATUS_REFRESH_SECONDS", 0.01)
    store, slack = FakeStore(), FakeSlack()
    calls = 0

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls == 1:
            # The task card is the only sign that Hondo took the message.
            todo = {
                "content": "Check the deploy",
                "status": "in_progress",
                "active_form": "Checking the deploy",
            }
            return ModelResponse(parts=[ToolCallPart("write_todos", {"todos": [todo]})])
        if calls == 2:
            return ModelResponse(parts=[ToolCallPart("lookup", {})])
        return ModelResponse(parts=[TextPart("It is.")])

    conversation = make_conversation(slack)
    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn),
        capabilities=[ToolBoundary(), conversation, TodosCapability(slack)],
    )

    @agent.tool_plain
    async def lookup() -> str:
        await wait_until(lambda: any(s["text"] and s["posts"] == 1 for s in slack.statuses))
        return "healthy"

    event = thread_message("Ev-1", "1700000000.000200", "is the deploy ok?")
    await drive(make_runner(agent, store, slack, conversation, runtime=FAST), event)

    assert [m["text"] for m in slack.posted] == ["→ *Checking the deploy…*", "It is."]
    assert all(s["posts"] < 2 for s in slack.statuses if s["text"])
    assert slack.statuses[-1]["text"] == ""


async def test_unaddressed_run_that_stays_silent_shows_no_status() -> None:
    store, slack = FakeStore(), FakeSlack()

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        return no_reply()

    event = thread_message("Ev-1", "1700000000.000200", "lunch at noon?")
    await drive(build(FunctionModel(model_fn), store, slack), event)

    assert slack.posted == []
    assert slack.statuses == []


IMAGE_URL = "https://files.slack.com/files-pri/T01-F01/alert.png"
ALERT = ChatFile("alert.png", IMAGE_URL, "image/png")


async def load_image(url: str) -> tuple[bytes, str] | None:
    return b"alert-bytes", "image/png"


def prompt_images(messages: list[ModelMessage]) -> list[Any]:
    return [
        item
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and not isinstance(part.content, str)
        for item in part.content
        if not isinstance(item, str)
    ]


def build_with_images(model: Any, store: FakeStore, slack: FakeSlack) -> SessionRunner:
    conversation = make_conversation(slack)
    agent = Agent(
        model,
        deps_type=RunModel,
        capabilities=[ToolBoundary(), conversation, MediaContent(load_image)],
    )
    return make_runner(agent, store, slack, conversation, runtime=FAST)


async def test_images_reach_the_model_and_later_runs_but_not_the_saved_history() -> None:
    store, slack = FakeStore(), FakeSlack()
    seen: list[list[Any]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(prompt_images(messages))
        return ModelResponse(parts=[TextPart("checkout-api is down.")])

    runner = build_with_images(FunctionModel(model_fn), store, slack)
    first = make_event(text="<@UBOT> what is this?", files=(ALERT, ChatFile("report.pdf")))
    await drive(runner, first)
    later = make_event(
        event_id="Ev002",
        ts="1700000000.000200",
        thread_ts="1700000000.000100",
        text="<@UBOT> and the pod name?",
    )
    await drive(runner, later)

    image = BinaryContent(data=b"alert-bytes", media_type="image/png", identifier="alert.png")
    assert seen == [[image], [image]]
    rows = [message for _, _, _, message in store.session_messages[SessionKey.from_event(first)]]
    saved = ModelMessagesTypeAdapter.validate_python(rows)
    assert prompt_images(saved) == [
        ImageUrl(IMAGE_URL, media_type="image/png", identifier="alert.png")
    ]
    assert b"alert-bytes".decode() not in str(rows)
    [request, *_] = saved
    assert isinstance(request, ModelRequest)
    [part] = [part for part in request.parts if isinstance(part, UserPromptPart)]
    assert isinstance(part.content, list)
    line = part.content[0]
    assert isinstance(line, str) and line.endswith("what is this? [file: report.pdf]")


async def test_an_image_in_a_mid_run_steer_reaches_the_model() -> None:
    store, slack = FakeStore(), FakeSlack()
    root = make_event()
    key = SessionKey.from_event(root)
    steer = make_event(
        event_id="Ev7",
        ts="1700000000.000700",
        thread_ts=root.ts,
        text="here is the alert",
        files=(ALERT,),
    )
    seen: list[list[Any]] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(prompt_images(messages))
        if any("here is the alert" in str(message) for message in messages):
            return ModelResponse(parts=[TextPart("It is a 503.")])
        await store.insert_mail(key, steer, SteerPriority.ASAP, target_workflow_id=None)
        return ModelResponse(parts=[TextPart("On it."), ToolCallPart(tool_name="lookup", args={})])

    conversation = make_conversation(slack)
    agent = Agent(
        FunctionModel(model_fn),
        deps_type=RunModel,
        capabilities=[ToolBoundary(), conversation, MediaContent(load_image)],
    )

    @agent.tool_plain
    async def lookup() -> str:
        return "resource42"

    runner = make_runner(agent, store, slack, conversation, runtime=FAST)

    assert await drive(runner, root) == "It is a 503."
    image = BinaryContent(data=b"alert-bytes", media_type="image/png", identifier="alert.png")
    assert seen == [[], [image]]
