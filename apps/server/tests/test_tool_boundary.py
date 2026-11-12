from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import ToolDefinition

import hondo.agent.tool_boundary as tool_boundary_mod
from hondo.agent.tool_boundary import ToolBoundary
from hondo.models import SessionKey
from hondo.runtime.durable import run_workflow_id
from hondo.store.postgres import PostgresStore


def _wrap_call(
    boundary: ToolBoundary, handler: Any, *, name: str = "bitbucket_getPullRequests"
) -> Any:
    return boundary.wrap_tool_execute(
        None,  # type: ignore[arg-type]  # ctx is unused by the wrapper
        call=ToolCallPart(tool_name=name, args={}),
        tool_def=ToolDefinition(name=name),
        args={},
        handler=handler,
    )


async def test_tool_exception_returns_error_text_to_model() -> None:
    """A tool can never fail the run: any exception (here the live-observed bare
    McpError protocol 404 that bypasses pydantic-ai's retry conversion) comes
    back to the model as error text."""
    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    boundary = ToolBoundary()

    async def handler(args: Any) -> Any:
        raise McpError(ErrorData(code=-32603, message="Request failed with status code 404"))

    result = await _wrap_call(boundary, handler)
    assert isinstance(result, str)
    assert "404" in result and "bitbucket_getPullRequests" in result


async def test_control_flow_exceptions_pass_through() -> None:
    """ModelRetry & co. are pydantic-ai signaling, not failures — never flattened."""
    boundary = ToolBoundary()

    async def handler(args: Any) -> Any:
        raise ModelRetry("try different arguments")

    with pytest.raises(ModelRetry, match="different arguments"):
        await _wrap_call(boundary, handler)


async def test_hanging_tool_times_out_with_error_text() -> None:
    boundary = ToolBoundary(tool_timeout=0.02)

    async def handler(args: Any) -> Any:
        await asyncio.Event().wait()

    result = await _wrap_call(boundary, handler)
    assert isinstance(result, str)
    assert "timed out" in result


async def test_cancellation_swallowing_tool_is_abandoned(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tool_boundary_mod, "_TOOL_CANCEL_GRACE_SECONDS", 0.01)
    boundary = ToolBoundary(tool_timeout=0.02)
    cleanup_blocked = asyncio.Event()
    release_cleanup = asyncio.Event()
    cleanup_done = asyncio.Event()

    async def handler(args: Any) -> Any:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cleanup_blocked.set()
            try:
                await release_cleanup.wait()
            finally:
                cleanup_done.set()

    result = await _wrap_call(boundary, handler)
    assert isinstance(result, str)
    assert "timed out" in result
    assert [record.msg["event"] for record in caplog.records if isinstance(record.msg, dict)] == [
        "tool cancellation hung; task abandoned as a zombie",
        "tool timed out; error returned to the model",
    ]
    assert cleanup_blocked.is_set()
    assert not cleanup_done.is_set()
    release_cleanup.set()
    await asyncio.wait_for(cleanup_done.wait(), timeout=1)


async def _eventually(check: Callable[[], Awaitable[bool]], *, timeout: float) -> None:
    async def poll() -> None:
        while not await check():
            await asyncio.sleep(0.02)

    await asyncio.wait_for(poll(), timeout=timeout)


@pytest.mark.slow
async def test_crash_recovery_resumes_without_replaying_effects(
    store: PostgresStore, pg_dsn: str
) -> None:
    """SIGKILL mid-model-step; a fresh process recovers and completes exactly once.

    Recorded steps (placeholder post, tool, first model call) must not
    re-execute; the interrupted model step re-runs (at-least-once at the step
    boundary); the final answer updates the original placeholder.
    """
    uid = uuid.uuid4().hex[:12]
    env = {**os.environ, "HONDO_CRASH_DSN": pg_dsn, "HONDO_CRASH_UID": uid}
    child = [sys.executable, "tests/dbos_crash_child.py"]

    async def effects(name: str) -> int:
        return await store._connected_pool.fetchval(
            "SELECT count(*) FROM crash_effects WHERE uid = $1 AND name = $2", uid, name
        )

    first = subprocess.Popen(  # noqa: ASYNC220
        [*child, "first"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:

        async def mid_model_step() -> bool:
            try:
                return await effects("model2") >= 1
            except Exception:
                return False  # table not created yet

        await _eventually(mid_model_step, timeout=45)
    finally:
        first.kill()  # SIGKILL: no cleanup, no lease release — nothing graceful
        first.wait()

    recover = subprocess.Popen([*child, "recover"], env=env, stderr=subprocess.DEVNULL)
    assert recover.wait(timeout=90) == 0

    assert await effects("tool") == 1, "recorded tool step re-executed on recovery"
    assert await effects("model1") == 1, "recorded model step re-called on recovery"
    assert await effects("model2") == 2, "the interrupted model step re-runs once"
    assert await effects("model_selection") == 1, "recovery must replay the saved model choice"

    pool = store._connected_pool
    finals = await pool.fetch(
        "SELECT ts FROM crash_slack WHERE uid = $1 AND kind = 'post' AND text = 'Recovered.'", uid
    )
    assert len(finals) == 1, "the final answer must post exactly once across the crash"
    reactions = await pool.fetch(
        "SELECT ts FROM crash_slack WHERE uid = $1 AND kind = 'reaction'", uid
    )
    assert len(reactions) == 0, "UX v2: nothing reacts; reactions here mean a stray step"


@pytest.mark.slow
async def test_crash_after_usage_commit_converges_to_one_row(
    store: PostgresStore, pg_dsn: str
) -> None:
    """SIGKILL in the record_usage window between the INSERT's commit and the
    step checkpoint; recovery re-executes the step with replayed (recorded)
    model responses onto the same (workflow_id, kind, resource) key — exactly
    one row with the final values, no duplicate and no summing.
    """
    uid = uuid.uuid4().hex[:12]
    env = {**os.environ, "HONDO_CRASH_DSN": pg_dsn, "HONDO_CRASH_UID": uid}
    child = [sys.executable, "tests/dbos_crash_child.py"]
    pool = store._connected_pool
    workflow_id = run_workflow_id(
        SessionKey(team="T01", channel="C01", thread_root=f"1700000000.{uid[:6]}"), f"Ev1-{uid}"
    )

    async def effects(name: str) -> int:
        return await pool.fetchval(
            "SELECT count(*) FROM crash_effects WHERE uid = $1 AND name = $2", uid, name
        )

    first = subprocess.Popen(  # noqa: ASYNC220
        [*child, "usage"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:

        async def usage_committed() -> bool:
            try:
                return await effects("usage_write") >= 1
            except Exception:
                return False  # table not created yet

        await _eventually(usage_committed, timeout=45)
        # The child hangs pre-checkpoint, but the INSERT is already committed.
        committed = await pool.fetch(
            "SELECT * FROM usage_events WHERE workflow_id = $1", workflow_id
        )
        assert len(committed) == 1
    finally:
        first.kill()  # SIGKILL between the step's commit and its checkpoint
        first.wait()

    recover = subprocess.Popen([*child, "recover"], env=env, stderr=subprocess.DEVNULL)
    assert recover.wait(timeout=90) == 0

    assert await effects("usage_write") == 2, "the uncheckpointed step must re-execute once"
    finals = await pool.fetch("SELECT * FROM usage_events WHERE workflow_id = $1", workflow_id)
    assert len(finals) == 1, "the replayed write must land on the same key, not add a row"
    assert json.loads(finals[0]["metadata"]) == {"requests": 2}
    # Deterministic replay: recorded responses re-yield the same numbers, and
    # the upsert replaces — a doubled quantity here would mean summing.
    assert finals[0]["quantity_in"] == committed[0]["quantity_in"]
    assert finals[0]["quantity_out"] == committed[0]["quantity_out"]
    assert finals[0]["provider"] == committed[0]["provider"]
    assert finals[0]["resource"] == committed[0]["resource"]


@pytest.mark.slow
@pytest.mark.parametrize("crash_point", ["between_scripts", "inside_script", "after_tool_timeout"])
async def test_code_mode_recovery_restores_state_without_repeating_completed_tools(
    store: PostgresStore, pg_dsn: str, tmp_path: Path, crash_point: str
) -> None:
    uid = uuid.uuid4().hex[:12]
    env = {
        **os.environ,
        "HONDO_CRASH_DSN": pg_dsn,
        "HONDO_CRASH_UID": uid,
        "HONDO_CRASH_CODE_MODE": crash_point,
    }
    child = [sys.executable, "tests/dbos_crash_child.py"]

    async def effects(name: str) -> int:
        return await store._connected_pool.fetchval(
            "SELECT count(*) FROM crash_effects WHERE uid = $1 AND name = $2", uid, name
        )

    log_path = tmp_path / "child.log"
    with log_path.open("w") as log:
        first = subprocess.Popen([*child, "first"], env=env, stdout=log, stderr=log)  # noqa: ASYNC220
        try:

            async def ready_to_crash() -> bool:
                assert first.poll() is None, log_path.read_text()
                try:
                    return await effects("code_crash_point") == 1
                except Exception:
                    return False  # The child has not created the table yet.

            await _eventually(ready_to_crash, timeout=45)
        finally:
            first.kill()
            first.wait()

        recover = subprocess.Popen([*child, "recover"], env=env, stdout=log, stderr=log)
        try:
            assert await asyncio.to_thread(recover.wait, timeout=60) == 0, log_path.read_text()
        finally:
            if recover.poll() is None:
                recover.kill()
                recover.wait()

    calls = await store._connected_pool.fetch(
        "SELECT name FROM crash_effects WHERE uid = $1 AND name IN ('code_lookup', 'code_pause') "
        "ORDER BY id",
        uid,
    )
    expected = ["code_lookup", "code_pause"]
    if crash_point == "inside_script":
        expected.append("code_pause")
    assert [call["name"] for call in calls] == expected, log_path.read_text()
    workflow_id = run_workflow_id(
        SessionKey(team="T01", channel="C01", thread_root=f"1700000000.{uid[:6]}"), f"Ev1-{uid}"
    )
    steps = await store._connected_pool.fetch(
        "SELECT function_name FROM hondo_dbos.operation_outputs WHERE workflow_uuid = $1 "
        "ORDER BY function_id",
        workflow_id,
    )
    names = [step["function_name"] for step in steps]
    assert "tool:run_code" not in names
    assert [name for name in names if name.startswith("tool:linear_")] == [
        "tool:linear_lookup",
        "tool:linear_pause",
    ], log_path.read_text()
    assert (
        await store._connected_pool.fetchval(
            "SELECT count(*) FROM crash_slack WHERE uid = $1 AND kind = 'post' AND text = 'Recovered.'",
            uid,
        )
        == 1
    ), log_path.read_text()


@pytest.mark.slow
async def test_todo_card_recovery_updates_the_card_posted_before_the_crash(
    store: PostgresStore, pg_dsn: str, tmp_path: Path
) -> None:
    uid = uuid.uuid4().hex[:12]
    env = {
        **os.environ,
        "HONDO_CRASH_DSN": pg_dsn,
        "HONDO_CRASH_UID": uid,
        "HONDO_CRASH_TODOS": "1",
    }
    child = [sys.executable, "tests/dbos_crash_child.py"]
    pool = store._connected_pool

    log_path = tmp_path / "child.log"
    with log_path.open("w") as log:
        first = subprocess.Popen([*child, "first"], env=env, stdout=log, stderr=log)  # noqa: ASYNC220
        try:

            async def ready_to_crash() -> bool:
                assert first.poll() is None, log_path.read_text()
                try:
                    return (
                        await pool.fetchval(
                            "SELECT count(*) FROM crash_effects WHERE uid = $1 "
                            "AND name = 'todos_crash_point'",
                            uid,
                        )
                        == 1
                    )
                except Exception:
                    return False

            await _eventually(ready_to_crash, timeout=45)
        finally:
            first.kill()
            first.wait()

        recover = subprocess.Popen([*child, "recover"], env=env, stdout=log, stderr=log)
        try:
            assert await asyncio.to_thread(recover.wait, timeout=60) == 0, log_path.read_text()
        finally:
            if recover.poll() is None:
                recover.kill()
                recover.wait()

    rows = await pool.fetch(
        "SELECT kind, text, ts FROM crash_slack WHERE uid = $1 AND text LIKE '%Check the deploy%' "
        "ORDER BY id",
        uid,
    )
    assert [(row["kind"], row["text"]) for row in rows] == [
        ("post", "→ *Check the deploy…*"),
        ("update", "✓ Check the deploy"),
    ], log_path.read_text()
    assert rows[0]["ts"] == rows[1]["ts"]


async def test_step_token_names_each_tool_step(pg_dsn: str) -> None:
    from dbos import DBOS, SetWorkflowID, StepOptions

    from hondo.runtime.durable import step_token

    suffix = uuid.uuid4().hex
    DBOS(
        config={
            "name": "step-token-test",
            "system_database_url": pg_dsn,
            "run_admin_server": False,
            "dbos_system_schema": f"step_token_{suffix}",
            "log_level": "WARNING",
        }
    )  # type: ignore[arg-type]

    async def handler() -> str | None:
        return step_token()

    async def tool() -> str | None:
        # ToolBoundary runs each tool in its own task inside the step.
        return await asyncio.ensure_future(handler())

    @DBOS.workflow(name=f"test.step_token.{suffix}")
    async def run() -> list[str | None]:
        return [await DBOS.run_step_async(StepOptions(name="tool:x"), tool) for _ in range(2)]

    try:
        DBOS.launch()
        with SetWorkflowID(f"wf-{suffix}"):
            first, second = await run()
    finally:
        DBOS.destroy(destroy_registry=True)

    assert first is not None and first.startswith(f"wf-{suffix}:")
    assert second is not None and second != first
    assert step_token() is None
