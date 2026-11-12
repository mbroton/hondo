"""Crash-recovery child: the real engine (store + runner + dispatcher + DBOS)
without Socket Mode. Slack calls and model/tool executions are recorded in
Postgres so the parent test can assert exactly-once effects across a SIGKILL.

Modes:  first   — ingress one mention; the model blocks mid-step until killed
        usage   — ingress one mention; the run completes up to record_usage,
                  which blocks after its INSERT commits until killed
        recover — launch only; DBOS recovery resumes the pending run

With HONDO_CRASH_CODE_MODE=approval, the script calls a tool that needs
approval. The first process waits for the click until killed; the recover
process clicks Approve.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections.abc import Sequence
from typing import Any

import asyncpg
import structlog
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
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness import CodeMode

from hondo.agent.conversation import ConversationCapability
from hondo.agent.todos import TodosCapability
from hondo.agent.tool_boundary import ToolBoundary
from hondo.chat import ChannelInfo, ChatApiError, ChatHistoryPage, ChatMessage, ChatUser
from hondo.config import EngagePolicy, RuntimeConfig
from hondo.mcp.approvals import ToolApprovals
from hondo.mcp.store import MCPStore
from hondo.models import Event, EventKind, SessionKey
from hondo.runtime.dispatcher import Dispatcher
from hondo.runtime.durable import DBOSBackend, run_workflow_id
from hondo.runtime.gateway import normalize_action
from hondo.runtime.model_selection import RunModel
from hondo.runtime.runner import SessionRunner
from hondo.slack import SlackStyle
from hondo.store.postgres import PostgresStore
from hondo.toolsets import prepare_sequential_tools
from hondo.users import UserDirectory

DSN = os.environ["HONDO_CRASH_DSN"]
UID = os.environ["HONDO_CRASH_UID"]
MODE = sys.argv[1]
CODE_MODE = os.environ.get("HONDO_CRASH_CODE_MODE")
TODOS = os.environ.get("HONDO_CRASH_TODOS") == "1"
ROOT = f"1700000000.{UID[:6]}"

DDL = """
CREATE TABLE IF NOT EXISTS crash_effects (
    id serial PRIMARY KEY, uid text NOT NULL, name text NOT NULL
);
CREATE TABLE IF NOT EXISTS crash_slack (
    id serial PRIMARY KEY, uid text NOT NULL, kind text NOT NULL,
    text text NOT NULL, ts text NOT NULL DEFAULT ''
);
"""


async def record(name: str) -> None:
    """One row per actual execution; crash-recovery replays add none."""
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute("INSERT INTO crash_effects (uid, name) VALUES ($1, $2)", UID, name)
    finally:
        await conn.close()


class PGSlack:
    """Slack double recording every call in Postgres (visible across processes)."""

    async def post_message(self, *, channel: str, text: str, thread_ts: str | None = None) -> str:
        conn = await asyncpg.connect(DSN)
        try:
            row_id = await conn.fetchval(
                "INSERT INTO crash_slack (uid, kind, text) VALUES ($1, 'post', $2) RETURNING id",
                UID,
                text,
            )
            ts = f"90000{row_id:05d}.000000"
            await conn.execute("UPDATE crash_slack SET ts = $2 WHERE id = $1", row_id, ts)
        finally:
            await conn.close()
        return ts

    async def post_notice(self, *, channel: str, text: str, thread_ts: str | None = None) -> str:
        return await self.post_message(channel=channel, text=text, thread_ts=thread_ts)

    async def update_message(self, *, channel: str, message_ref: str, text: str) -> None:
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "INSERT INTO crash_slack (uid, kind, text, ts) VALUES ($1, 'update', $2, $3)",
                UID,
                text,
                message_ref,
            )
        finally:
            await conn.close()

    async def add_reaction(self, *, channel: str, message_ref: str, name: str) -> None:
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "INSERT INTO crash_slack (uid, kind, text, ts) VALUES ($1, 'reaction', $2, $3)",
                UID,
                name,
                message_ref,
            )
        finally:
            await conn.close()

    async def set_status(self, *, channel: str, thread_ts: str, text: str) -> None:
        pass  # Transient: the crash tests check only durable posts.

    async def post_approval(
        self, *, channel: str, thread_ts: str, approval_id: str, text: str
    ) -> str:
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute(
                "INSERT INTO crash_slack (uid, kind, text, ts) VALUES ($1, 'approval', $2, $3)",
                UID,
                text,
                approval_id,
            )
        finally:
            await conn.close()
        return "80000000001.000000"

    async def close_approval(
        self, *, channel: str, message_ref: str, text: str, status: str
    ) -> None:
        await self.update_message(channel=channel, message_ref=message_ref, text=status)

    async def is_member(self, *, channel: str, user_id: str) -> bool:
        return True

    async def fetch_thread(
        self, *, channel: str, root: str, limit: int | None = None, oldest: str | None = None
    ) -> list[ChatMessage]:
        return []

    async def fetch_history(
        self,
        *,
        channel: str,
        limit: int | None = None,
        oldest: str | None = None,
        latest: str | None = None,
    ) -> ChatHistoryPage:
        return ChatHistoryPage(messages=[], has_more=False)

    async def user_info(self, *, user_id: str) -> ChatUser:
        raise ChatApiError(f"user_info: no such user {user_id}")

    async def channel_info(self, channel: str) -> ChannelInfo | None:
        return None


class CrashPointStore(PostgresStore):
    """PostgresStore whose record_usage hangs after the INSERT commits: in
    "usage" mode the parent SIGKILLs us in the window between the step's
    database effect and its DBOS checkpoint, forcing recovery to re-execute
    the step onto the already-written conflict key."""

    async def record_usage(self, rows: Sequence[dict[str, Any]]) -> None:
        await super().record_usage(rows)
        await record("usage_write")  # one row per actual execution of the step
        if MODE == "usage":
            await asyncio.sleep(3600)  # the parent SIGKILLs us inside this window


def has_tool_return(messages: list[ModelMessage]) -> bool:
    return any(
        isinstance(part, ToolReturnPart)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
    )


async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    if not has_tool_return(messages):
        await record("model1")
        return ModelResponse(parts=[ToolCallPart(tool_name="lookup", args={})])
    await record("model2")
    if MODE == "first":
        await asyncio.sleep(3600)  # the parent SIGKILLs us inside this model step
    return ModelResponse(parts=[TextPart("Recovered.")])


async def changed_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    raise AssertionError("recovery must retain the model selected before the crash")


async def code_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    returns = [
        part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "run_code"
    ]
    if CODE_MODE == "approval":
        if not returns:
            return ModelResponse(
                parts=[ToolCallPart("run_code", {"code": "linear_change(title='Fix login')"})]
            )
        assert returns == ["changed Fix login"], returns
        return ModelResponse(parts=[TextPart("Recovered.")])
    if not returns:
        return ModelResponse(
            parts=[
                ToolCallPart(
                    "run_code",
                    {"code": "answer = linear_lookup()\npause_result = linear_pause()\nanswer"},
                )
            ]
        )
    assert returns[0] == "resource42", returns
    if len(returns) == 1:
        if CODE_MODE != "inside_script" and MODE == "first":
            await record("code_crash_point")
            await asyncio.Event().wait()
        code = "answer + ' ready'"
        if CODE_MODE == "after_tool_timeout":
            code = "assert 'timed out' in pause_result\n" + code
        return ModelResponse(parts=[ToolCallPart("run_code", {"code": code})])
    assert returns == ["resource42", "resource42 ready"], returns
    return ModelResponse(parts=[TextPart("Recovered.")])


async def todos_model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    returns = [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart) and part.tool_name == "write_todos"
    ]
    if not returns:
        todo = {"content": "Check the deploy", "status": "in_progress"}
        return ModelResponse(parts=[ToolCallPart("write_todos", {"todos": [todo]})])
    if len(returns) == 1:
        if MODE == "first":
            await record("todos_crash_point")
            await asyncio.Event().wait()
        todo = {"content": "Check the deploy", "status": "completed"}
        return ModelResponse(parts=[ToolCallPart("write_todos", {"todos": [todo]})])
    return ModelResponse(parts=[TextPart("Recovered.")])


async def select_model(team: str, channel: str) -> RunModel:
    await record("model_selection")
    return RunModel("changed" if MODE == "recover" else "original")


async def main() -> None:
    store = CrashPointStore(DSN)
    await store.connect(run_migrations=False, pool_min_size=1, pool_max_size=1)
    conn = await asyncpg.connect(DSN)
    try:
        await conn.execute(DDL)
    finally:
        await conn.close()

    client = PGSlack()
    style = SlackStyle()
    directory = UserDirectory(client)
    mcp_store = MCPStore(DSN)
    await mcp_store.connect(run_migrations=False, pool_min_size=1, pool_max_size=2)
    approvals = ToolApprovals(mcp_store, client, directory)
    conversation = ConversationCapability(
        client=client,
        directory=directory,
        style=style,
    )
    original = FunctionModel(todos_model if TODOS else code_model if CODE_MODE else model_fn)
    code_tools: FunctionToolset[RunModel] = FunctionToolset()
    agent: Agent[RunModel, str] = Agent(
        original,
        deps_type=RunModel,
        toolsets=[code_tools.prepared(prepare_sequential_tools)],
        capabilities=[
            ToolBoundary(tool_timeout=0.1 if CODE_MODE == "after_tool_timeout" else 60),
            approvals,
            DBOSDurability(
                name="hondo_agent",
                parallel_execution_mode="sequential",
                models={"original": original, "changed": FunctionModel(changed_model)},
            ),
            conversation,
            *([TodosCapability(client)] if TODOS else []),
            *([CodeMode(tools=lambda ctx, td: td.name.startswith("linear_"))] if CODE_MODE else []),
        ],
    )

    @agent.tool_plain
    async def lookup() -> str:
        await record("tool")
        return "resource42"

    if CODE_MODE:

        @code_tools.tool_plain
        async def linear_lookup() -> str:
            """Read the resource."""
            await record("code_lookup")
            return "resource42"

        @code_tools.tool_plain(requires_approval=True)
        async def linear_change(title: str) -> str:
            """Change a record."""
            await record("code_change")
            return f"changed {title}"

        @code_tools.tool_plain
        async def linear_pause() -> str:
            """Read another resource."""
            await record("code_pause")
            if CODE_MODE == "after_tool_timeout":
                await asyncio.Event().wait()
            if CODE_MODE == "inside_script" and MODE == "first":
                await record("code_crash_point")
                await asyncio.Event().wait()
            return "ready"

    DBOS(
        config={
            "name": "hondo-test",
            "system_database_url": DSN,
            "run_admin_server": False,
            "dbos_system_schema": "hondo_dbos",
            "log_level": "WARNING",
        }
    )
    queue = Queue("hondo_sessions", concurrency=1, partition_queue=True, polling_interval_sec=0.1)
    runner = SessionRunner(
        agent,
        store=store,
        client=client,
        style=style,
        directory=directory,
        runtime=RuntimeConfig(queue_polling_interval=0.1),
        model_selector=select_model,
    )

    @DBOS.workflow(name="hondo.drive_session", max_recovery_attempts=3)
    async def drive_session(key_json: dict[str, Any], event_json: dict[str, Any]) -> str:
        return await runner.drive(key_json, event_json)

    backend = DBOSBackend(queue, drive_session)
    dispatcher = Dispatcher(store, EngagePolicy(), backend)
    DBOS.launch()  # in recover mode this alone resumes the pending workflow

    key = SessionKey(team="T01", channel="C01", thread_root=ROOT)
    event_id = f"Ev1-{UID}"
    if MODE in ("first", "usage"):
        event = Event(
            event_id=event_id,
            kind=EventKind.APP_MENTION,
            team="T01",
            channel="C01",
            ts=ROOT,
            user="U01",
            text="<@B01> what is the resource?",
            mentions_bot=True,
        )
        await store.append_event(event)
        await dispatcher.dispatch(event)

    workflow_id = run_workflow_id(key, event_id)
    if MODE == "recover" and CODE_MODE == "approval":
        row = await mcp_store._connected_pool.fetchrow(
            "SELECT id, message_ts FROM tool_approvals WHERE workflow_id = $1", workflow_id
        )
        assert row is not None
        approval_id, message_ts = row["id"], row["message_ts"]
        clicked = normalize_action(
            {
                "type": "block_actions",
                "team": {"id": "T01"},
                "user": {"id": "U02"},
                "channel": {"id": "C01"},
                "container": {"type": "message", "message_ts": message_ts},
                "actions": [
                    {"action_id": "approval_approve", "value": approval_id, "action_ts": "1.0"}
                ],
            }
        )
        assert clicked is not None
        await approvals.decide(clicked)
    while True:
        statuses = await DBOS.list_workflows_async(workflow_ids=[workflow_id])
        if statuses and statuses[0].status == "SUCCESS":
            break
        if statuses and statuses[0].status == "ERROR":
            raise RuntimeError(f"workflow failed: {statuses[0].error}")
        await asyncio.sleep(0.1)
    print("SUCCESS", flush=True)
    DBOS.destroy()
    await mcp_store.aclose()
    await store.aclose()


if __name__ == "__main__":
    structlog.configure(
        processors=[structlog.processors.format_exc_info, structlog.processors.JSONRenderer()]
    )
    asyncio.run(main())
