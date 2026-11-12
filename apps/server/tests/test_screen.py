from __future__ import annotations

import asyncio
import inspect

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelResolutionContext
from pydantic_ai.models.function import AgentInfo, FunctionModel

from hondo.chat import ChatApiError, ChatFile, ChatMessage
from hondo.models import Event, Session, SessionKey
from hondo.runtime.model_selection import RunModel
from hondo.runtime.screen import ChannelScreen, screen_agent
from hondo.slack import SlackStyle
from tests.fakes import FakeSlack, FakeStore, make_event

ROOT = "1700000000.000100"
GOAL = "Answer questions about deploys. Not social chat."


async def _no_resolve(ctx: ModelResolutionContext[RunModel], model_id: str) -> Model:
    raise AssertionError("each test sets the model")


class ScriptedScreen:
    """The screen model as a test sees it: the prompts it got, and its answer."""

    def __init__(self, answer: bool | Exception, *, delay: float = 0) -> None:
        self.answer = answer
        self.delay = delay
        self.prompts: list[str] = []

    async def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        request = messages[-1]
        assert isinstance(request, ModelRequest)
        self.prompts += [
            part.content
            for part in request.parts
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        ]
        await asyncio.sleep(self.delay)
        if isinstance(self.answer, Exception):
            raise self.answer
        tool = info.output_tools[0].name
        return ModelResponse(parts=[ToolCallPart(tool, {"response": self.answer})])


class BrokenThreadSlack(FakeSlack):
    async def fetch_thread(
        self, *, channel: str, root: str, limit: int | None = None, oldest: str | None = None
    ) -> list[ChatMessage]:
        raise ChatApiError("ratelimited")


class BrokenStore(FakeStore):
    async def get_session(self, key: SessionKey) -> Session | None:
        raise ConnectionError("database is gone")


class BrokenGoalStore(FakeStore):
    async def listening_goal(self, team: str, channel: str) -> str | None:
        raise ConnectionError("database is gone")


@pytest.fixture
def store() -> FakeStore:
    store = FakeStore()
    store.listening_goals[("T01", "C01")] = GOAL
    return store


def unaddressed(**overrides: object) -> Event:
    return make_event(
        **{"kind": "message", "mentions_bot": False, "text": "how do I roll back a deploy?"}
        | overrides
    )


def reply(ts: str = "1700000060.000100", text: str = "how do I roll back a deploy?") -> Event:
    return unaddressed(ts=ts, thread_ts=ROOT, text=text)


def make_screen(
    store: FakeStore,
    slack: FakeSlack | None = None,
    selected: RunModel | None = RunModel("test"),
    timeout: float = 15.0,
) -> ChannelScreen:
    async def select_model(team: str) -> RunModel | None:
        return selected

    return ChannelScreen(
        screen_agent(_no_resolve),
        store=store,
        client=slack or FakeSlack(),
        style=SlackStyle(),
        select_model=select_model,
        timeout=timeout,
    )


async def screen(job: ChannelScreen, event: Event, model: ScriptedScreen) -> bool:
    with job._agent.override(model=FunctionModel(model)):
        return await job.decide(SessionKey.from_event(event), event)


@pytest.mark.parametrize("answer", [True, False])
async def test_the_model_decides_for_a_top_level_message(store: FakeStore, answer: bool) -> None:
    model = ScriptedScreen(answer)

    assert await screen(make_screen(store), unaddressed(), model) is answer
    assert model.prompts == [
        f"<goal>\n{GOAL}\n</goal>\n"
        "<new_message>\n"
        "[2023-11-14 22:13 UTC] <@U01>: how do I roll back a deploy?\n"
        "</new_message>"
    ]


async def test_the_screen_records_its_usage_apart_from_the_run(store: FakeStore) -> None:
    await screen(make_screen(store), unaddressed(), ScriptedScreen(True))

    assert [row["workflow_id"] for row in store.usage_events.values()] == [
        "T01:C01:1700000000.000100/screen#Ev001"
    ]


async def test_a_thread_reply_comes_with_the_earlier_thread_only(store: FakeStore) -> None:
    slack = FakeSlack()
    slack.thread_messages = [
        ChatMessage(author="U02", text="deploys fail since noon", ref=ROOT),
        ChatMessage(author="U01", text="how do I roll back a deploy?", ref="1700000060.000100"),
        # Posted while the screen ran: it is not context for the message before it.
        ChatMessage(author="U03", text="me too", ref="1700000120.000100"),
    ]
    model = ScriptedScreen(True)

    assert await screen(make_screen(store, slack), reply(), model)
    assert model.prompts == [
        f"<goal>\n{GOAL}\n</goal>\n"
        "<thread>\n[2023-11-14 22:13 UTC] <@U02>: deploys fail since noon\n</thread>\n"
        "<new_message>\n"
        "[2023-11-14 22:14 UTC] <@U01>: how do I roll back a deploy?\n"
        "</new_message>"
    ]


async def test_a_long_thread_gives_the_ten_messages_before_the_reply(store: FakeStore) -> None:
    slack = FakeSlack()
    slack.thread_messages = [
        ChatMessage(author="U02", text=f"message {n}", ref=f"{1700000000 + n}.000100")
        for n in range(300)
    ]
    event = reply(ts="1700000300.000100", text="and the rollback?")
    model = ScriptedScreen(True)

    assert await screen(make_screen(store, slack), event, model)
    thread = model.prompts[0].split("<thread>\n")[1].split("\n</thread>")[0].splitlines()
    assert [line.split(": ", 1)[1] for line in thread] == [f"message {n}" for n in range(290, 300)]


async def test_long_messages_are_cut(store: FakeStore) -> None:
    model = ScriptedScreen(True)

    await screen(make_screen(store), unaddressed(text="x" * 600), model)

    assert f"<@U01>: {'x' * 500}\n</new_message>" in model.prompts[0]


async def test_a_file_shows_as_a_note(store: FakeStore) -> None:
    model = ScriptedScreen(True)
    event = unaddressed(text="", files=(ChatFile(name="deploy.log"),))

    await screen(make_screen(store), event, model)

    assert "<@U01>: [file: deploy.log]\n</new_message>" in model.prompts[0]


@pytest.mark.parametrize(
    "case",
    ["model error", "timeout", "thread read error", "goal read error", "no model"],
)
async def test_a_screen_that_cannot_decide_drops_the_message(store: FakeStore, case: str) -> None:
    model = ScriptedScreen(True)
    job = make_screen(store)
    event = unaddressed()
    if case == "model error":
        model = ScriptedScreen(RuntimeError("provider down"))
    elif case == "timeout":
        model = ScriptedScreen(True, delay=5)
        job = make_screen(store, timeout=0.05)
    elif case == "thread read error":
        job = make_screen(store, BrokenThreadSlack())
        event = reply()
    elif case == "goal read error":
        job = make_screen(BrokenGoalStore())
    elif case == "no model":
        job = make_screen(store, selected=None)

    assert await screen(job, event, model) is False


async def test_a_store_error_on_the_session_check_is_not_a_drop(store: FakeStore) -> None:
    # A reply in a joined thread must fail loudly, so the failed-run sweep
    # gives it a notice and the next run recovers it.
    with pytest.raises(ConnectionError):
        await screen(make_screen(BrokenStore()), reply(), ScriptedScreen(False))


class JoinedDuringScreen:
    """The screen model, while a mention joins the thread."""

    def __init__(self, store: FakeStore, key: SessionKey) -> None:
        self.store = store
        self.key = key

    async def __call__(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        await self.store.ensure_session(self.key)
        return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"response": False})])


async def test_a_thread_joined_during_the_screen_goes_on(store: FakeStore) -> None:
    event = reply()
    job = make_screen(store)

    with job._agent.override(
        model=FunctionModel(JoinedDuringScreen(store, SessionKey.from_event(event)))
    ):
        assert await job.decide(SessionKey.from_event(event), event)


@pytest.mark.parametrize("event", [make_event(), make_event(channel="D01", is_dm=True)])
async def test_messages_for_hondo_are_not_screened(store: FakeStore, event: Event) -> None:
    model = ScriptedScreen(False)

    assert await screen(make_screen(BrokenStore()), event, model)
    assert model.prompts == []


async def test_a_thread_that_hondo_joined_is_not_screened(store: FakeStore) -> None:
    event = reply()
    await store.save_session(Session(key=SessionKey.from_event(event)))
    model = ScriptedScreen(False)

    assert await screen(make_screen(store), event, model)
    assert model.prompts == []


async def test_a_channel_that_stopped_listening_drops_the_message(store: FakeStore) -> None:
    store.listening_goals.clear()
    model = ScriptedScreen(True)

    assert not await screen(make_screen(store), unaddressed(), model)
    assert model.prompts == []


def test_the_decision_is_a_coroutine_function_for_dbos(store: FakeStore) -> None:
    # DBOS runs a step as a coroutine only when the function is one; an object
    # with an async __call__ is not, and its coroutine fails to serialize.
    assert inspect.iscoroutinefunction(make_screen(store).decide)
