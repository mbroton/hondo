from __future__ import annotations

from collections.abc import AsyncIterator
from uuid import UUID, uuid4

import httpx
import pytest

from hondo.api.store import ControlStore
from hondo.crypto import TokenCipher
from tests.test_auth import CRYPTO_KEY, VERIFIED_JANE, FakeOIDCProvider, _app, _client
from tests.test_workspaces import _sign_in_as_admin

NOTHING_DONE = {
    "provider_connected": False,
    "model_selected": False,
    "tool_connected": False,
    "answered_in_channel": False,
    "complete": False,
}


@pytest.fixture(autouse=True)
async def _isolate_workspaces(control_store: ControlStore) -> AsyncIterator[None]:
    await control_store._connected_pool.execute(
        "TRUNCATE session_messages, sessions, workspaces RESTART IDENTITY CASCADE"
    )
    yield


async def _setup(client: httpx.AsyncClient, workspace_id: UUID) -> dict[str, bool]:
    response = await client.get(f"/api/workspaces/{workspace_id}/setup")
    assert response.status_code == 200, response.text
    return response.json()


async def _select_model(control_store: ControlStore, workspace_id: UUID) -> UUID:
    connection = await control_store.create_provider_connection(
        workspace_id, "openai", "Production", TokenCipher(CRYPTO_KEY).encrypt("sk-test")
    )
    assert await control_store.set_agent_model(
        workspace_id, connection.id, "gpt-4.1", reads_images=True
    )
    return connection.id


async def _add_tool(
    control_store: ControlStore, workspace_id: UUID, name: str, *, enabled: bool
) -> None:
    await control_store._connected_pool.execute(
        """
        INSERT INTO mcp_connections (id, workspace_id, name, url, enabled)
        VALUES ($1, $2, $3, 'https://mcp.example/mcp', $4)
        """,
        uuid4(),
        workspace_id,
        name,
        enabled,
    )


async def _add_session(control_store: ControlStore, team: str, channel: str) -> None:
    await control_store._connected_pool.execute(
        "INSERT INTO sessions (team, channel, thread_root, state) VALUES ($1, $2, $3, 'idle')",
        team,
        channel,
        "1700000000.000001",
    )


async def _add_answer(control_store: ControlStore, team: str, channel: str) -> None:
    await control_store._connected_pool.execute(
        """
        INSERT INTO session_messages
            (team, channel, thread_root, run_id, seq, history_version, message)
        VALUES ($1, $2, '1700000000.000001', $3, 0, 1, '{}'::jsonb)
        """,
        team,
        channel,
        str(uuid4()),
    )


async def test_a_new_workspace_has_every_step_open(
    control_store: ControlStore, pg_dsn: str
) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        setup = await _setup(client, workspace_id)

    assert setup == NOTHING_DONE


async def test_every_step_is_done_when_hondo_has_recorded_it(
    control_store: ControlStore, pg_dsn: str
) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        await _select_model(control_store, workspace_id)
        await _add_tool(control_store, workspace_id, "Linear", enabled=True)
        await _add_answer(control_store, "T1", "C01")
        setup = await _setup(client, workspace_id)

    assert setup == {key: True for key in NOTHING_DONE}


async def test_records_that_do_not_finish_a_step_leave_it_open(
    control_store: ControlStore, pg_dsn: str
) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        connection_id = await _select_model(control_store, workspace_id)
        connection = await control_store.get_provider_connection(workspace_id, connection_id)
        assert connection is not None
        await control_store.record_provider_check(workspace_id, connection, "rejected")
        await _add_tool(control_store, workspace_id, "Linear", enabled=False)
        # A mention that got the "choose a model" notice, or a failed run,
        # leaves a session but saves no messages.
        await _add_session(control_store, "T1", "C01")
        await _add_answer(control_store, "T1", "D01")
        await _add_answer(control_store, "T2", "C01")
        setup = await _setup(client, workspace_id)

    assert setup == NOTHING_DONE
