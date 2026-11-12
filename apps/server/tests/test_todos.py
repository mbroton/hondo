from typing import Any

import pytest
from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from hondo.agent.todos import TODO_RULES, TodosCapability, _render_todo_card
from hondo.context import RunScope, reset_scope, set_scope
from hondo.models import Event, EventKind, SessionKey, Todo, TodoStatus
from tests.fakes import FakeSlack

EVENT = Event(
    event_id="Ev001",
    kind=EventKind.APP_MENTION,
    team="T01",
    channel="C01",
    ts="1700000000.000100",
    user="U01",
    text="<@UBOT> hello",
    mentions_bot=True,
    bot_user="UBOT",
)

SCOPE = RunScope(key=SessionKey.from_event(EVENT), event=EVENT)


async def test_capability_registers_tool_and_callable_instructions() -> None:
    capability = TodosCapability(FakeSlack())
    toolset = capability.get_toolset()
    assert toolset.id == "hondo_todos"
    assert capability.get_instructions() == [capability._todo_rules]
    assert capability._todo_rules() == TODO_RULES

    model = TestModel(call_tools=[])
    agent = Agent(model, capabilities=[capability])
    await agent.run("hello")

    assert model.last_model_request_parameters is not None
    assert [tool.name for tool in model.last_model_request_parameters.function_tools] == [
        "write_todos"
    ]


async def test_write_todos_posts_then_updates_one_card() -> None:
    slack = FakeSlack()
    capability = TodosCapability(slack)
    scope = RunScope(key=SCOPE.key, event=EVENT)
    first = [
        Todo(content="Prepare release", status=TodoStatus.COMPLETED),
        Todo(
            content="Verify health checks",
            status=TodoStatus.IN_PROGRESS,
            active_form="Verifying health checks.…",
        ),
        Todo(content="Publish release"),
    ]

    token = set_scope(scope)
    try:
        result = await capability._write_todos(first)
        card_ref = slack.posted[0]["ts"]
        update_result = await capability._write_todos(
            [
                Todo(content="Prepare release", status=TodoStatus.COMPLETED),
                Todo(content="Verify health checks", status=TodoStatus.COMPLETED),
                Todo(
                    content="Publish release",
                    status=TodoStatus.IN_PROGRESS,
                    active_form="Publishing release",
                ),
            ]
        )
    finally:
        reset_scope(token)

    assert result == "Recorded 3 todos (1 done, 1 in progress, 1 pending)."
    assert slack.posted == [
        {
            "channel": "C01",
            "text": ("✓ Prepare release\n→ *Verifying health checks…*\n☐ Publish release"),
            "thread_ts": "1700000000.000100",
            "ts": card_ref,
            "blocks": None,
        }
    ]
    assert slack.updates == [
        {
            "channel": "C01",
            "message_ref": card_ref,
            "text": ("✓ Prepare release\n✓ Verify health checks\n→ *Publishing release…*"),
        }
    ]
    assert update_result == "Recorded 3 todos (2 done, 1 in progress, 0 pending)."
    assert scope.todos.items[2].status == TodoStatus.IN_PROGRESS
    assert scope.todos.card_ref == card_ref


def test_render_todo_card_falls_back_to_content_for_active_label() -> None:
    assert (
        _render_todo_card([Todo(content="Check deployment...", status=TodoStatus.IN_PROGRESS)])
        == "→ *Check deployment…*"
    )


def test_render_todo_card_strikes_cancelled_items() -> None:
    assert (
        _render_todo_card(
            [
                Todo(content="Audit configs", status=TodoStatus.COMPLETED),
                Todo(content="Publish release", status=TodoStatus.CANCELLED),
            ]
        )
        == "✓ Audit configs\n✗ ~Publish release~"
    )


async def test_write_todos_all_completed_prompts_wrap_up() -> None:
    capability = TodosCapability(FakeSlack())
    token = set_scope(RunScope(key=SCOPE.key, event=EVENT))
    try:
        result = await capability._write_todos(
            [Todo(content="Ship release", status=TodoStatus.COMPLETED)]
        )
    finally:
        reset_scope(token)

    assert result == (
        "Recorded 1 todo (1 done, 0 in progress, 0 pending). "
        "Nothing left to work on, so wrap up and give your final answer."
    )


async def test_write_todos_stopped_work_ends_cancelled_and_settled() -> None:
    """A stopped run marks leftovers cancelled — and that also settles the list."""
    capability = TodosCapability(FakeSlack())
    token = set_scope(RunScope(key=SCOPE.key, event=EVENT))
    try:
        result = await capability._write_todos(
            [
                Todo(content="Audit configs", status=TodoStatus.COMPLETED),
                Todo(content="Publish release", status=TodoStatus.CANCELLED),
            ]
        )
    finally:
        reset_scope(token)

    assert result == (
        "Recorded 2 todos (1 done, 0 in progress, 0 pending, 1 cancelled). "
        "Nothing left to work on, so wrap up and give your final answer."
    )


async def test_write_todos_empty_list_leaves_the_card_alone() -> None:
    slack = FakeSlack()
    scope = RunScope(key=SCOPE.key, event=EVENT)
    scope.todos.card_ref = "1700000001.000100"
    capability = TodosCapability(slack)

    token = set_scope(scope)
    try:
        result = await capability._write_todos([])
    finally:
        reset_scope(token)

    assert result == "Cleared the todo list; the card in the thread is unchanged."
    assert slack.posted == []
    assert slack.updates == []
    assert scope.todos.items == []


@pytest.mark.parametrize("card_ref", [None, "1700000001.000100"])
async def test_write_todos_chat_failures_are_best_effort(card_ref: str | None) -> None:
    class FailingSlack(FakeSlack):
        async def post_message(self, **kwargs: Any) -> str:
            raise RuntimeError("post failed")

        async def update_message(self, **kwargs: Any) -> None:
            raise RuntimeError("update failed")

    scope = RunScope(key=SCOPE.key, event=EVENT)
    scope.todos.card_ref = card_ref
    capability = TodosCapability(FailingSlack())

    token = set_scope(scope)
    try:
        result = await capability._write_todos([Todo(content="Keep working")])
    finally:
        reset_scope(token)

    assert result == "Recorded 1 todo (0 done, 0 in progress, 1 pending)."
    assert scope.todos.items == [Todo(content="Keep working")]
