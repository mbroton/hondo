from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from dbos import DBOS
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models import Model, ModelResolutionContext
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from hondo.agent.conversation import ConversationCapability
from hondo.agent.summaries import SummaryCapability
from hondo.chat import ChannelInfo, ChatApiError, ChatMessage, ChatUser
from hondo.context import RunScope, reset_scope, set_scope
from hondo.models import Event, EventKind, SessionKey
from hondo.runtime.durable import summary_workflow_id
from hondo.runtime.model_selection import RunModel
from hondo.runtime.summarizer import ThreadSummarizer, summary_agent
from hondo.slack import SlackStyle
from hondo.store.base import SummaryView
from hondo.store.postgres import PostgresStore
from hondo.users import UserDirectory
from tests.fakes import FakeSlack

PRIVATE = ChannelInfo(is_channel=True, is_private=True, is_ext_shared=False)
SHARED = ChannelInfo(is_channel=True, is_private=False, is_ext_shared=True)
GROUP_DM = ChannelInfo(is_channel=False, is_private=True, is_ext_shared=False)
ROOT = "1700000000.000100"
KEY = SessionKey(team="T01", channel="C01", thread_root=ROOT)
ELSEWHERE = SummaryView(team="T01", channel="C09", include_public=True)
WORKFLOW_ID = "T01:C01:1700000000.000100/summary#Ev1"


@pytest.fixture
def slack() -> FakeSlack:
    return FakeSlack()


# --- summary_search and summary_read ----------------------------------------------


def scope(channel: str = "C01", ts: str = ROOT) -> RunScope:
    event = Event(
        event_id=f"Ev-{channel}-{ts}",
        kind=EventKind.APP_MENTION,
        team="T01",
        channel=channel,
        ts=ts,
        user="U01",
        text="<@UBOT> hello",
        mentions_bot=True,
        is_dm=channel.startswith("D"),
        bot_user="UBOT",
    )
    return RunScope(key=SessionKey.from_event(event), event=event)


@contextmanager
def running_in(channel: str = "C01") -> Iterator[None]:
    token = set_scope(scope(channel))
    try:
        yield
    finally:
        reset_scope(token)


@pytest.fixture
def summaries(store: PostgresStore, slack: FakeSlack) -> SummaryCapability:
    return SummaryCapability(store, slack, SlackStyle())


async def save(
    store: PostgresStore, text: str, *, channel: str = "C01", private: bool = False
) -> int:
    key = SessionKey(team="T01", channel=channel, thread_root=ROOT)
    await store.save_summary(key, private=private, text=text, covered_ts="1700000100.000100")
    found = await store.thread_summary(key)
    assert found is not None
    return found.id


async def test_summary_is_found_from_another_channel(
    summaries: SummaryCapability, store: PostgresStore
) -> None:
    summary = await save(store, "Payments API timed out. Ana restarted payments-worker.")

    with running_in("C02"):
        page = await summaries._summary_search(["timeouts", "payments"])
        read = await summaries._summary_read(summary)

    assert page["matched"] == 1
    [hit] = page["hits"]
    assert "Payments API timed out" in hit["text"]
    assert (hit["summary"], hit["date"], hit["channel"], hit["link"]) == (
        summary,
        "2023-11-14 22:13 UTC",
        "<#C01>",
        "https://slack.com/archives/C01/p1700000000000100",
    )
    assert isinstance(read, dict)
    assert read["link"] == "https://slack.com/archives/C01/p1700000000000100"
    assert read["text"] == "Payments API timed out. Ana restarted payments-worker."


async def test_private_channel_summaries_stay_in_their_channel(
    summaries: SummaryCapability, store: PostgresStore, slack: FakeSlack
) -> None:
    slack.channels["C02"] = PRIVATE
    summary = await save(store, "secret outage", channel="C02", private=True)
    public = await save(store, "public outage")

    with running_in("C02"):
        assert (await summaries._summary_search(["outage"]))["matched"] == 2
    with running_in("C01"):
        assert [
            hit["summary"] for hit in (await summaries._summary_search(["outage"]))["hits"]
        ] == [public]
        assert await summaries._summary_read(summary) == (
            f"There is no summary {summary} that this conversation can see."
        )


async def test_shared_channel_is_kept_apart_both_ways(
    summaries: SummaryCapability, store: PostgresStore, slack: FakeSlack
) -> None:
    slack.channels["C03"] = SHARED
    await save(store, "internal outage")
    await save(store, "shared outage", channel="C03", private=True)

    with running_in("C03"):
        page = await summaries._summary_search(["outage"])
    assert [hit["text"] for hit in page["hits"]] == ["shared outage"]
    with running_in("C01"):
        page = await summaries._summary_search(["outage"])
    assert [hit["text"] for hit in page["hits"]] == ["internal outage"]


@pytest.mark.parametrize("now", [PRIVATE, SHARED, None])
async def test_a_channel_that_became_private_or_shared_hides_its_summaries_at_once(
    summaries: SummaryCapability, store: PostgresStore, slack: FakeSlack, now: ChannelInfo | None
) -> None:
    summary = await save(store, "ops outage")
    await save(store, "general outage", channel="C02")
    # No new message and no new summary in C01: only Slack knows it changed.
    slack.channels["C01"] = now

    with running_in("C09"):
        page = await summaries._summary_search(["outage"])
        read = await summaries._summary_read(summary)

    assert ([hit["text"] for hit in page["hits"]], page["matched"]) == (["general outage"], 1)
    assert read == f"There is no summary {summary} that this conversation can see."


async def test_direct_messages_read_public_summaries(
    summaries: SummaryCapability, store: PostgresStore
) -> None:
    summary = await save(store, "public outage")

    with running_in("D01"):
        assert (await summaries._summary_search(["outage"]))["matched"] == 1
        read = await summaries._summary_read(summary)
    assert isinstance(read, dict) and read["text"] == "public outage"


async def test_search_needs_a_word_and_returns_1_to_50_hits(
    summaries: SummaryCapability, store: PostgresStore
) -> None:
    for n in range(51):
        thread = SessionKey(team="T01", channel="C01", thread_root=f"{n}.1")
        await store.save_summary(thread, private=False, text="outage", covered_ts="2.1")

    with running_in():
        with pytest.raises(ModelRetry, match="at least one word"):
            await summaries._summary_search([" ", ""])
        assert len((await summaries._summary_search(["outage"], limit=100))["hits"]) == 50
        assert len((await summaries._summary_search(["outage"], limit=-1))["hits"]) == 1


async def test_unknown_conversation_type_is_an_error(
    summaries: SummaryCapability, slack: FakeSlack
) -> None:
    slack.channels["C01"] = None
    with running_in(), pytest.raises(ChatApiError):
        await summaries._summary_search(["outage"])


async def test_summary_rules_render_before_the_session(summaries: SummaryCapability) -> None:
    slack = FakeSlack()
    conversation = ConversationCapability(client=slack, directory=UserDirectory(slack))
    agent = Agent(TestModel(call_tools=[]), capabilities=[summaries, conversation])

    with running_in():
        result = await agent.run("hello")

    first_request = result.all_messages()[0]
    assert isinstance(first_request, ModelRequest)
    instructions = first_request.instructions
    assert instructions is not None
    assert "summary_search" in instructions
    # Static rules come before the per-run tail, so they stay in the cached prefix.
    assert instructions.index("<thread_summaries>") < instructions.index("<current_session>")


# --- the summary job ----------------------------------------------------------------


async def _no_resolve(ctx: ModelResolutionContext[RunModel], model_id: str) -> Model:
    raise AssertionError("each test sets the model")


@pytest.fixture
def agent() -> Iterator[Agent[RunModel, str]]:
    try:
        yield summary_agent(_no_resolve)
    finally:
        DBOS.destroy(destroy_registry=True)


class ScriptedModel:
    """The model as a test sees it: the prompts it got, and its replies."""

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        self.prompts += [
            part.content
            for part in request.parts
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        ]
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return ModelResponse(parts=[TextPart(reply)])


def summarizer(
    agent: Agent[RunModel, str],
    store: PostgresStore,
    slack: FakeSlack,
    model: RunModel | None = RunModel("test"),
    timeout: float = 60,
) -> ThreadSummarizer:
    async def select_model(team: str) -> RunModel | None:
        return model

    return ThreadSummarizer(
        agent,
        store=store,
        summaries=store,
        client=slack,
        style=SlackStyle(),
        directory=UserDirectory(slack),
        select_model=select_model,
        timeout=timeout,
    )


async def summarize(
    agent: Agent[RunModel, str],
    job: ThreadSummarizer,
    model: ScriptedModel,
    key: SessionKey = KEY,
) -> None:
    with agent.override(model=FunctionModel(model)):
        await job.summarize(key, WORKFLOW_ID)


async def test_summary_covers_the_whole_thread(
    agent: Agent[RunModel, str], store: PostgresStore, slack: FakeSlack
) -> None:
    slack.thread_messages = [
        ChatMessage(author="U01", text="<@UBOT> payments API times out", ref=ROOT),
        ChatMessage(author="UBOT", text="Checking the payments logs.", ref="1700000060.000100"),
        # An alert bot's message starts no run.
        ChatMessage(author="B07", text="Resolved: payments-api latency", ref="1700000120.000100"),
        ChatMessage(author="U01", text="", ref="1700000150.000100"),
        ChatMessage(author="U01", text="fixed, it was the pool size", ref="1700000180.000100"),
    ]
    model = ScriptedModel("Payments API timed out. The pool size was the cause.")

    await summarize(agent, summarizer(agent, store, slack), model)

    assert model.prompts == [
        "The thread:\n\n"
        "[2023-11-14 22:13 UTC] jane: @Hondo payments API times out\n"
        "[2023-11-14 22:14 UTC] Hondo: Checking the payments logs.\n"
        "[2023-11-14 22:15 UTC] <@B07>: Resolved: payments-api latency\n"
        "[2023-11-14 22:16 UTC] jane: fixed, it was the pool size"
    ]
    found = await store.thread_summary(KEY)
    assert found is not None
    assert (found.text, found.covered_ts) == (
        "Payments API timed out. The pool size was the cause.",
        "1700000180.000100",
    )
    assert await store.get_summary(ELSEWHERE, found.id) is not None


async def test_summary_is_written_again_only_after_a_new_message(
    agent: Agent[RunModel, str], store: PostgresStore, slack: FakeSlack
) -> None:
    slack.thread_messages = [ChatMessage(author="U01", text="payments down", ref=ROOT)]
    model = ScriptedModel("Payments were down.", "Payments were down. Ana fixed them.")
    job = summarizer(agent, store, slack)

    await summarize(agent, job, model)
    await summarize(agent, job, model)
    assert len(model.prompts) == 1

    slack.thread_messages.append(
        ChatMessage(author="U01", text="fixed by Ana", ref="1700000060.000100")
    )
    await summarize(agent, job, model)

    assert len(model.prompts) == 2
    found = await store.thread_summary(KEY)
    assert found is not None
    assert (found.text, found.covered_ts) == (
        "Payments were down. Ana fixed them.",
        "1700000060.000100",
    )


async def test_no_summary_without_a_model_or_outside_channels(
    agent: Agent[RunModel, str], store: PostgresStore, slack: FakeSlack
) -> None:
    slack.thread_messages = [ChatMessage(author="U01", text="payments down", ref=ROOT)]
    slack.channels["G01"] = GROUP_DM
    slack.channels["C05"] = None
    model = ScriptedModel("Payments were down.")

    await summarize(agent, summarizer(agent, store, slack, model=None), model)
    for channel in ("G01", "C05"):
        key = SessionKey(team="T01", channel=channel, thread_root=ROOT)
        await summarize(agent, summarizer(agent, store, slack), model, key)

    assert model.prompts == []
    assert slack.thread_fetches == []
    assert await store.thread_summary(KEY) is None


@pytest.mark.parametrize("info", [PRIVATE, SHARED])
async def test_summaries_of_private_and_shared_channels_stay_there(
    agent: Agent[RunModel, str], store: PostgresStore, slack: FakeSlack, info: ChannelInfo
) -> None:
    slack.channels["C01"] = info
    slack.thread_messages = [ChatMessage(author="U01", text="payments down", ref=ROOT)]

    await summarize(agent, summarizer(agent, store, slack), ScriptedModel("Payments were down."))

    found = await store.thread_summary(KEY)
    assert found is not None
    assert await store.get_summary(ELSEWHERE, found.id) is None


async def test_long_thread_keeps_the_newest_messages_and_the_earlier_summary(
    agent: Agent[RunModel, str], store: PostgresStore, slack: FakeSlack
) -> None:
    await store.save_summary(
        KEY, private=False, text="Earlier: payments down.", covered_ts="1700000001.000100"
    )
    slack.thread_messages = [
        ChatMessage(author="U01", text=f"message{n} " + "x" * 5_000, ref=f"17000000{n:02d}.000100")
        for n in range(10, 40)
    ]
    model = ScriptedModel("Payments were down for a long time.")
    job = summarizer(agent, store, slack)

    # The step's record holds only the messages that can reach the model.
    read = await job._read_thread(KEY)
    assert read is not None
    _, recent, cut = read
    assert cut and recent[-1].ref == "1700000039.000100"
    assert "message10 " not in {message.text[:10] for message in recent}

    await summarize(agent, job, model)

    [prompt] = model.prompts
    earlier, thread = prompt.split(
        "The newest messages of the thread. Older ones are left out:\n\n"
    )
    assert earlier == "Your earlier summary of this thread:\n\nEarlier: payments down.\n\n"
    assert len(thread) <= 60_000
    assert "message39 " in thread and "message10 " not in thread
    # Each message is cut to 4,000 characters.
    assert thread.endswith("x" * (4_000 - len("message39 ")) + " [...]")


async def test_names_are_looked_up_only_for_messages_that_reach_the_model(
    agent: Agent[RunModel, str], store: PostgresStore
) -> None:
    class CountingSlack(FakeSlack):
        def __init__(self) -> None:
            super().__init__()
            self.lookups: list[str] = []

        async def user_info(self, *, user_id: str) -> ChatUser:
            self.lookups.append(user_id)
            return ChatUser(id=user_id, name=f"person{user_id}", real_name="", is_bot=False)

    slack = CountingSlack()
    slack.thread_messages = [
        ChatMessage(author=f"U{n}", text="x" * 5_000, ref=f"17000000{n:02d}.000100")
        for n in range(10, 40)
    ]
    model = ScriptedModel("A long thread.")

    await summarize(agent, summarizer(agent, store, slack), model)

    [prompt] = model.prompts
    shown = {f"U{n}" for n in range(10, 40) if f"personU{n}:" in prompt}
    assert "U39" in shown and "U10" not in shown
    assert set(slack.lookups) == shown


async def test_short_thread_is_summarized_without_the_earlier_summary(
    agent: Agent[RunModel, str], store: PostgresStore, slack: FakeSlack
) -> None:
    await store.save_summary(
        KEY, private=False, text="Earlier: wrong guess.", covered_ts="1700000001.000100"
    )
    slack.thread_messages = [
        ChatMessage(author="U01", text="payments down", ref=ROOT),
        ChatMessage(author="U01", text="it was the pool", ref="1700000060.000100"),
    ]
    model = ScriptedModel("Payments were down; the pool was the cause.")

    await summarize(agent, summarizer(agent, store, slack), model)

    [prompt] = model.prompts
    assert prompt.startswith("The thread:\n\n") and "wrong guess" not in prompt


async def test_summary_that_is_too_long_is_written_again(
    agent: Agent[RunModel, str], store: PostgresStore, slack: FakeSlack
) -> None:
    slack.thread_messages = [ChatMessage(author="U01", text="payments down", ref=ROOT)]
    model = ScriptedModel("x" * 4_001, "Payments were down.")

    await summarize(agent, summarizer(agent, store, slack), model)

    found = await store.thread_summary(KEY)
    assert found is not None and found.text == "Payments were down."


async def test_summary_records_its_usage(
    agent: Agent[RunModel, str], store: PostgresStore, slack: FakeSlack
) -> None:
    slack.thread_messages = [ChatMessage(author="U01", text="payments down", ref=ROOT)]

    await summarize(agent, summarizer(agent, store, slack), ScriptedModel("Payments were down."))

    rows = await store._connected_pool.fetch(
        "SELECT workflow_id, channel, thread_root FROM usage_events"
    )
    assert [(row["workflow_id"], row["channel"], row["thread_root"]) for row in rows] == [
        (WORKFLOW_ID, "C01", ROOT)
    ]


async def test_a_summary_the_model_never_gets_right_still_records_its_usage(
    agent: Agent[RunModel, str], store: PostgresStore, slack: FakeSlack
) -> None:
    slack.thread_messages = [ChatMessage(author="U01", text="payments down", ref=ROOT)]

    await summarize(agent, summarizer(agent, store, slack), ScriptedModel("x" * 4_001))

    assert await store.thread_summary(KEY) is None
    rows = await store._connected_pool.fetch("SELECT workflow_id, metadata FROM usage_events")
    assert [(row["workflow_id"], json.loads(row["metadata"])["requests"]) for row in rows] == [
        (WORKFLOW_ID, 2)
    ]


async def test_a_summary_stopped_at_the_time_limit_still_records_its_usage(
    agent: Agent[RunModel, str], store: PostgresStore, slack: FakeSlack
) -> None:
    slack.thread_messages = [ChatMessage(author="U01", text="payments down", ref=ROOT)]
    calls = 0

    async def too_long_then_hang(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal calls
        calls += 1
        if calls > 1:
            await asyncio.Event().wait()
        return ModelResponse(parts=[TextPart("x" * 4_001)])

    job = summarizer(agent, store, slack, timeout=0.5)
    with agent.override(model=FunctionModel(too_long_then_hang)):
        await job.summarize(KEY, WORKFLOW_ID)

    assert calls == 2
    assert await store.thread_summary(KEY) is None
    rows = await store._connected_pool.fetch("SELECT metadata FROM usage_events")
    assert [json.loads(row["metadata"])["requests"] for row in rows] == [1]


def test_a_summary_is_under_its_team_but_never_the_live_run() -> None:
    workflow_id = summary_workflow_id(KEY, "Ev1")
    assert workflow_id.startswith("T01:")
    assert not workflow_id.startswith("T01:C01:1700000000.000100#")


async def test_a_failed_summary_is_logged_and_nothing_is_saved(
    agent: Agent[RunModel, str], store: PostgresStore
) -> None:
    class BrokenSlack(FakeSlack):
        async def fetch_thread(
            self, *, channel: str, root: str, limit: int | None = None, oldest: str | None = None
        ) -> list[ChatMessage]:
            raise RuntimeError("channel_not_found")

    slack = BrokenSlack()
    model = ScriptedModel("Payments were down.")

    await summarize(agent, summarizer(agent, store, slack), model)

    assert model.prompts == []
    assert await store.thread_summary(KEY) is None
