from typing import Any

import structlog
from pydantic_ai._instructions import AgentInstructions
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.toolsets import FunctionToolset

from hondo.chat import ChatClient
from hondo.context import current_scope
from hondo.models import Todo, TodoStatus
from hondo.runtime.durable import step

logger = structlog.get_logger("hondo.agent.todos")

TODO_RULES = """\
<task_list>
For multi-step work, keep a visible task list with write_todos:

- Use it when a task takes 3+ distinct steps or bundles several requests. \
Skip it for anything you can just do. A single question or one-step change \
needs no list.
- Each call replaces the whole list, and the thread shows it as a card \
people watch. Update it as you work: mark an item completed the moment it \
is done, and don't batch updates.
- Keep exactly one item in_progress at a time, with active_form set to what \
you are doing right now ("Deploying the api service").
- Never mark an item completed while tests fail, work is partial, or an \
error is unresolved. Keep it in_progress, or add an item for what's blocking.
- When a mid-run reply changes the task, rewrite the list to match before \
continuing.
- When you're told to stop, or a step becomes irrelevant, mark it cancelled. \
Don't delete it, and never write an empty list. The card is a record of what \
happened.
</task_list>
"""


def _render_todo_card(todos: list[Todo]) -> str:
    # Plain characters, not emoji — emoji render oversized in Slack and make
    # the card shout. The in-progress line is the one that moves; bold it.
    # No header: the ✓/→/☐ marks say what the message is.
    lines = []
    for todo in todos:
        if todo.status == TodoStatus.COMPLETED:
            lines.append(f"✓ {todo.content}")
        elif todo.status == TodoStatus.IN_PROGRESS:
            label = (todo.active_form or todo.content).rstrip(".…")
            lines.append(f"→ *{label}…*")
        elif todo.status == TodoStatus.CANCELLED:
            lines.append(f"✗ ~{todo.content}~")
        else:
            lines.append(f"☐ {todo.content}")
    return "\n".join(lines)


class TodosCapability(AbstractCapability[Any]):
    def __init__(self, client: ChatClient) -> None:
        self.client = client

    def get_toolset(self) -> FunctionToolset[Any]:
        toolset: FunctionToolset[Any] = FunctionToolset(id="hondo_todos")
        toolset.add_function(self._write_todos, takes_ctx=False, name="write_todos")
        return toolset

    def get_instructions(self) -> AgentInstructions[Any]:
        return [self._todo_rules]

    def _todo_rules(self) -> str:
        return TODO_RULES

    async def _write_todos(self, todos: list[Todo]) -> str:
        """Replace your task list for this work; the thread shows it as a live card.

        Args:
            todos: The full list, in order. Every call replaces the previous list.
        """
        scope = current_scope()
        scope.todos.items = list(todos)
        if not todos:
            # A cleared list would render as a blank card — a glitch, not a
            # record. The rules teach cancelling instead; this guard covers
            # the model that clears anyway.
            return "Cleared the todo list; the card in the thread is unchanged."
        text = _render_todo_card(todos)
        try:
            if scope.todos.card_ref is None:
                scope.todos.card_ref = await step(
                    "todo_card_post",
                    self.client.post_message,
                    channel=scope.channel,
                    text=text,
                    thread_ts=scope.reply_thread_ts,
                )
            else:
                await step(
                    "todo_card_update",
                    self.client.update_message,
                    channel=scope.channel,
                    message_ref=scope.todos.card_ref,
                    text=text,
                )
        except Exception:
            logger.warning("todo card post failed", exc_info=True)

        done = sum(todo.status == TodoStatus.COMPLETED for todo in todos)
        in_progress = sum(todo.status == TodoStatus.IN_PROGRESS for todo in todos)
        pending = sum(todo.status == TodoStatus.PENDING for todo in todos)
        cancelled = sum(todo.status == TodoStatus.CANCELLED for todo in todos)
        counts = f"{done} done, {in_progress} in progress, {pending} pending"
        if cancelled:
            counts += f", {cancelled} cancelled"
        noun = "todo" if len(todos) == 1 else "todos"
        summary = f"Recorded {len(todos)} {noun} ({counts})."
        if not in_progress and not pending:
            summary += " Nothing left to work on, so wrap up and give your final answer."
        return summary
