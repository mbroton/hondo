from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
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
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness import CodeMode

import hondo.mcp.approvals as approvals_mod
from hondo.agent.tool_boundary import ToolBoundary
from hondo.chat import ChatUser
from hondo.config import EngagePolicy, RuntimeConfig
from hondo.mcp.approvals import ToolApprovals, describe
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
from tests.fakes import FakeSlack


class ApprovalSlack(FakeSlack):
    def __init__(self, members: set[str]) -> None:
        super().__init__()
        self.members = members
        self.users["U02"] = ChatUser(id="U02", name="Ana", real_name="Ana", is_bot=False)
        self.requests: list[dict[str, Any]] = []
        self.closed: list[dict[str, Any]] = []

    async def post_approval(
        self, *, channel: str, thread_ts: str, approval_id: str, text: str
    ) -> str:
        ts = f"8000000{len(self.requests):03d}.000000"
        self.requests.append(
            {"channel": channel, "thread_ts": thread_ts, "id": approval_id, "text": text, "ts": ts}
        )
        return ts

    async def close_approval(
        self, *, channel: str, message_ref: str, text: str, status: str
    ) -> None:
        self.closed.append({"message_ref": message_ref, "text": text, "status": status})

    async def is_member(self, *, channel: str, user_id: str) -> bool:
        return channel == "C01" and user_id in self.members


def click(
    approval_id: str,
    user: str,
    action: str = "approval_approve",
    *,
    channel: str = "C01",
    message_ts: str = "8000000000.000000",
) -> Event:
    event = normalize_action(
        {
            "type": "block_actions",
            "team": {"id": "T01"},
            "user": {"id": user},
            "channel": {"id": channel},
            "container": {"type": "message", "message_ts": message_ts, "channel_id": channel},
            "actions": [
                {"action_id": action, "value": approval_id, "action_ts": "1700000100.000001"}
            ],
        }
    )
    assert event is not None
    return event


def test_click_payload_gives_only_the_approval_and_the_choice() -> None:
    approve = click("a1", "U02")
    assert approve.kind is EventKind.INTERACTION
    assert approve.user == "U02"
    assert approve.payload == {
        "approval_id": "a1",
        "approved": True,
        "message_ts": "8000000000.000000",
    }
    assert click("a1", "U02", "approval_reject").payload["approved"] is False
    assert normalize_action({"type": "block_actions", "actions": []}) is None
    assert (
        normalize_action(
            {
                "type": "block_actions",
                "team": {"id": "T01"},
                "user": {"id": "U02"},
                "channel": {"id": "C01"},
                "actions": [{"action_id": "other", "value": "a1", "action_ts": "1.0"}],
            }
        )
        is None
    )


def test_request_shows_the_tool_and_short_arguments() -> None:
    assert describe("GitHub: create_issue", {"repo": "acme/api", "labels": ["bug"]}) == (
        '*Run GitHub: create_issue?*\n```repo: acme/api\nlabels: ["bug"]```'
    )
    assert describe("Jira: <edit>", {}) == "*Run Jira: &lt;edit&gt;?*\n```No arguments```"
    long = describe("T: t", {"body": "x" * 500})
    assert "x" * 199 + "…" in long and "x" * 200 not in long


@pytest.fixture
async def mcp_store(pg_dsn: str) -> AsyncIterator[MCPStore]:
    store = MCPStore(pg_dsn)
    await store.connect(run_migrations=False, pool_min_size=1, pool_max_size=5)
    await store._connected_pool.execute("TRUNCATE tool_approvals")
    yield store
    await store.aclose()


async def test_only_a_channel_member_decides_once_before_expiry(
    mcp_store: MCPStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    woken: list[tuple[str, str]] = []

    async def notify(workflow_id: str, topic: str) -> None:
        woken.append((workflow_id, topic))

    monkeypatch.setattr(approvals_mod, "notify", notify)
    slack = ApprovalSlack(members={"U02", "U03"})
    approvals = ToolApprovals(mcp_store, slack, UserDirectory(slack))
    await mcp_store.open_approval("a1", "wf-1", "T01", "C01", 1800)
    await mcp_store.set_approval_message("a1", "8000000000.000000")

    await approvals.decide(click("a1", "U02", message_ts="8000000999.000000"))
    await approvals.decide(click("a1", "U02", channel="C02"))
    assert (await mcp_store.get_approval("a1")).status == "pending"  # type: ignore[union-attr]
    await approvals.decide(click("a1", "U09"))
    assert (await mcp_store.get_approval("a1")).status == "pending"  # type: ignore[union-attr]
    other_team = click("a1", "U02").model_copy(update={"team": "T02"})
    await approvals.decide(other_team)
    assert (await mcp_store.get_approval("a1")).status == "pending"  # type: ignore[union-attr]
    assert woken == []

    await approvals.decide(click("a1", "U02", "approval_reject"))
    await approvals.decide(click("a1", "U03"))
    decided = await mcp_store.get_approval("a1")
    assert decided is not None
    assert (decided.status, decided.decided_by) == ("rejected", "U02")
    assert woken == [("wf-1", "approval:a1")]

    await mcp_store.open_approval("a2", "wf-2", "T01", "C01", 1800)
    await mcp_store.set_approval_message("a2", "8000000000.000000")
    await mcp_store._connected_pool.execute(
        "UPDATE tool_approvals SET expires_at = now() - interval '1 second' WHERE id = 'a2'"
    )
    await approvals.decide(click("a2", "U02"))
    assert (await mcp_store.get_approval("a2")).status == "pending"  # type: ignore[union-attr]
    assert (await mcp_store.settle_approval("a2")).status == "expired"
    assert woken == [("wf-1", "approval:a1")]


def tool_feedback(messages: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


class Runtime:
    def __init__(
        self, store: PostgresStore, mcp_store: MCPStore, slack: ApprovalSlack, *, code: bool
    ) -> None:
        self.store = store
        self.slack = slack
        self.changes: list[str] = []
        self.code = code
        model = FunctionModel(self.model)
        toolset: FunctionToolset[RunModel] = FunctionToolset()

        @toolset.tool_plain(requires_approval=True)
        async def linear_change(title: str) -> str:
            """Change a record."""
            self.changes.append(title)
            return f"changed {title}"

        self.approvals = ToolApprovals(mcp_store, slack, UserDirectory(slack), timeout=1800)
        self.agent: Agent[RunModel, str] = Agent(
            model,
            deps_type=RunModel,
            toolsets=[toolset.prepared(prepare_sequential_tools)],
            capabilities=[
                ToolBoundary(),
                self.approvals,
                DBOSDurability(
                    name="hondo_agent",
                    parallel_execution_mode="sequential",
                    models={"test": model},
                ),
                *([CodeMode(tools=lambda ctx, td: td.name.startswith("linear_"))] if code else []),
            ],
        )

    async def model(self, messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        feedback = tool_feedback(messages)
        if feedback:
            return ModelResponse(parts=[TextPart(f"Result: {feedback[-1]}")])
        if self.code:
            call = ToolCallPart("run_code", {"code": "linear_change(title='Fix login')"})
        else:
            call = ToolCallPart("linear_change", {"title": "Fix login"})
        return ModelResponse(parts=[call])

    async def start(self, pg_dsn: str, runtime: RuntimeConfig) -> None:
        DBOS(
            config={
                "name": "hondo-test",
                "system_database_url": pg_dsn,
                "run_admin_server": False,
                "dbos_system_schema": "hondo_dbos",
                "log_level": "WARNING",
                "notification_listener_polling_interval_sec": 0.1,
            }  # type: ignore[typeddict-unknown-key]
        )
        queue = Queue(
            "hondo_sessions", concurrency=1, partition_queue=True, polling_interval_sec=0.1
        )

        async def select_model(team: str, channel: str) -> RunModel:
            return RunModel("test")

        runner = SessionRunner(
            self.agent,
            store=self.store,
            client=self.slack,
            style=SlackStyle(),
            directory=UserDirectory(self.slack),
            runtime=runtime,
            model_selector=select_model,
        )

        @DBOS.workflow(name="hondo.drive_session", max_recovery_attempts=3)
        async def drive_session(key_json: dict[str, Any], event_json: dict[str, Any]) -> str:
            return await runner.drive(key_json, event_json)

        self.backend = DBOSBackend(queue, drive_session)
        self.dispatcher = Dispatcher(self.store, EngagePolicy(), self.backend, client=self.slack)
        DBOS.launch()

    async def mention(self, uid: str) -> str:
        root = f"1700000000.{uid[:6]}"
        event = Event(
            event_id=f"Ev1-{uid}",
            kind=EventKind.APP_MENTION,
            team="T01",
            channel="C01",
            ts=root,
            user="U01",
            text="<@B01> fix the login record",
            mentions_bot=True,
        )
        await self.store.append_event(event)
        await self.dispatcher.dispatch(event)
        return run_workflow_id(SessionKey.from_event(event), event.event_id)


async def eventually(check: Callable[[], Awaitable[bool] | bool], *, timeout: float = 20) -> None:
    async def poll() -> None:
        while True:
            result = check()
            if await result if isinstance(result, Awaitable) else result:
                return
            await asyncio.sleep(0.05)

    await asyncio.wait_for(poll(), timeout=timeout)


async def finished(workflow_id: str) -> bool:
    statuses = await DBOS.list_workflows_async(workflow_ids=[workflow_id])
    return bool(statuses) and statuses[0].status == "SUCCESS"


@pytest.mark.parametrize("code", [True, False], ids=["run_code", "direct"])
async def test_approved_call_runs_after_a_wait_longer_than_the_run_limits(
    store: PostgresStore, mcp_store: MCPStore, pg_dsn: str, code: bool
) -> None:
    slack = ApprovalSlack(members={"U02"})
    runtime = Runtime(store, mcp_store, slack, code=code)
    limits = RuntimeConfig(
        queue_polling_interval=0.1, debounce_period=0, run_timeout=2, stall_timeout=1
    )
    await runtime.start(pg_dsn, limits)
    try:
        uid = uuid.uuid4().hex
        workflow_id = await runtime.mention(uid)
        await eventually(lambda: len(slack.requests) == 1)
        request = slack.requests[0]
        assert request["text"] == "*Run linear_change?*\n```title: Fix login```"
        assert request["thread_ts"] == f"1700000000.{uid[:6]}" and request["channel"] == "C01"
        await asyncio.sleep(3)
        assert runtime.changes == []
        before = datetime.now(UTC)
        await runtime.approvals.decide(click(request["id"], "U02", message_ts=request["ts"]))
        await eventually(lambda: finished(workflow_id))
    finally:
        DBOS.destroy(destroy_registry=True)

    assert runtime.changes == ["Fix login"]
    assert slack.posted[-1]["text"] == "Result: changed Fix login"
    assert len(slack.closed) == 1
    status = slack.closed[0]["status"]
    assert slack.closed[0]["message_ref"] == request["ts"]
    assert status.startswith("Approved by <@U02> · <!date^")
    epoch = int(status.split("^")[1])
    assert abs(epoch - before.timestamp()) < 5


async def test_rejected_call_does_not_run_and_the_model_hears_who(
    store: PostgresStore, mcp_store: MCPStore, pg_dsn: str
) -> None:
    slack = ApprovalSlack(members={"U02"})
    runtime = Runtime(store, mcp_store, slack, code=True)
    await runtime.start(pg_dsn, RuntimeConfig(queue_polling_interval=0.1, debounce_period=0))
    try:
        workflow_id = await runtime.mention(uuid.uuid4().hex)
        await eventually(lambda: len(slack.requests) == 1)
        await runtime.approvals.decide(
            click(slack.requests[0]["id"], "U09", message_ts=slack.requests[0]["ts"])
        )
        await runtime.approvals.decide(
            click(
                slack.requests[0]["id"],
                "U02",
                "approval_reject",
                message_ts=slack.requests[0]["ts"],
            )
        )
        await eventually(lambda: finished(workflow_id))
    finally:
        DBOS.destroy(destroy_registry=True)

    assert runtime.changes == []
    assert "Rejected by Ana" in slack.posted[-1]["text"]
    assert slack.closed[0]["status"].startswith("Rejected by <@U02> · ")


async def test_unanswered_call_expires_and_does_not_run(
    store: PostgresStore, mcp_store: MCPStore, pg_dsn: str
) -> None:
    slack = ApprovalSlack(members={"U02"})
    runtime = Runtime(store, mcp_store, slack, code=True)
    runtime.approvals.timeout = 1.0
    await runtime.start(pg_dsn, RuntimeConfig(queue_polling_interval=0.1, debounce_period=0))
    try:
        workflow_id = await runtime.mention(uuid.uuid4().hex)
        await eventually(lambda: finished(workflow_id))
        await runtime.approvals.decide(
            click(slack.requests[0]["id"], "U02", message_ts=slack.requests[0]["ts"])
        )
    finally:
        DBOS.destroy(destroy_registry=True)

    assert runtime.changes == []
    assert "No one approved this call" in slack.posted[-1]["text"]
    assert slack.closed[0]["status"].startswith("Expired")
    approval = await mcp_store.get_approval(slack.requests[0]["id"])
    assert approval is not None and approval.status == "expired"


class BrokenSlack(ApprovalSlack):
    async def post_approval(
        self, *, channel: str, thread_ts: str, approval_id: str, text: str
    ) -> str:
        raise RuntimeError("channel_not_found")


async def test_call_does_not_run_when_the_request_cannot_be_posted(
    store: PostgresStore, mcp_store: MCPStore, pg_dsn: str
) -> None:
    slack = BrokenSlack(members={"U02"})
    runtime = Runtime(store, mcp_store, slack, code=True)
    await runtime.start(pg_dsn, RuntimeConfig(queue_polling_interval=0.1, debounce_period=0))
    try:
        workflow_id = await runtime.mention(uuid.uuid4().hex)
        await eventually(lambda: finished(workflow_id))
    finally:
        DBOS.destroy(destroy_registry=True)

    assert runtime.changes == []
    assert "could not ask for approval" in slack.posted[-1]["text"]
    assert slack.closed == []


@pytest.mark.slow
async def test_wait_for_approval_survives_a_crash(store: PostgresStore, pg_dsn: str) -> None:
    """SIGKILL while the call waits; a new process gets the click and runs the call once."""
    uid = uuid.uuid4().hex[:12]
    env = {
        **os.environ,
        "HONDO_CRASH_DSN": pg_dsn,
        "HONDO_CRASH_UID": uid,
        "HONDO_CRASH_CODE_MODE": "approval",
    }
    child = [sys.executable, "tests/dbos_crash_child.py"]
    pool = store._connected_pool

    async def count(kind: str) -> int:
        try:
            return await pool.fetchval(
                "SELECT count(*) FROM crash_slack WHERE uid = $1 AND kind = $2", uid, kind
            )
        except Exception:
            return 0

    async def posted() -> bool:
        return await count("approval") > 0

    first = subprocess.Popen(  # noqa: ASYNC220
        [*child, "first"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        await eventually(lambda: posted(), timeout=45)
        await asyncio.sleep(0.5)
    finally:
        first.kill()
        first.wait()

    recovered = subprocess.run(  # noqa: ASYNC221
        [*child, "recover"], env=env, capture_output=True, text=True, timeout=90
    )
    assert recovered.returncode == 0, recovered.stderr[-3000:]
    assert "SUCCESS" in recovered.stdout
    assert await count("approval") == 1
    assert (
        await pool.fetchval(
            "SELECT count(*) FROM crash_effects WHERE uid = $1 AND name = 'code_change'", uid
        )
        == 1
    )
    answers = await pool.fetch(
        "SELECT text FROM crash_slack WHERE uid = $1 AND kind = 'post' ORDER BY id", uid
    )
    assert [row["text"] for row in answers] == ["Recovered."]
