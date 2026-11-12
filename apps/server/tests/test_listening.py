"""Listening channels: admins save a goal per channel in the API, the runtime reads it."""

from __future__ import annotations

from uuid import uuid4

from hondo.api.store import ControlStore
from hondo.store.postgres import PostgresStore
from tests.test_auth import VERIFIED_JANE, FakeOIDCProvider, _app, _client
from tests.test_workspaces import _sign_in_as_admin


async def test_admin_saves_listening_channels_and_the_runtime_reads_them(
    control_store: ControlStore, store: PostgresStore, pg_dsn: str
) -> None:
    async with _client(_app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE))) as client:
        _, workspace = await _sign_in_as_admin(client, control_store, team="T1")
        path = f"/api/workspaces/{workspace}/agent/listening"

        initial = await client.get(path)
        assert initial.status_code == 200
        assert initial.json() == {"channels": []}
        assert await store.listening_goal("T1", "C02") is None

        saved = await client.put(
            path,
            json={
                "channels": [
                    {"channel": "C02", "goal": "  Answer deploy questions.  "},
                    {"channel": "C01", "goal": "Help with on-call alerts."},
                ]
            },
        )
        assert saved.status_code == 204
        assert (await client.get(path)).json() == {
            "channels": [
                {"channel": "C01", "goal": "Help with on-call alerts."},
                {"channel": "C02", "goal": "Answer deploy questions."},
            ]
        }
        assert await store.listening_goal("T1", "C02") == "Answer deploy questions."
        assert await store.listening_goal("T1", "C03") is None
        assert await store.listening_goal("T2", "C02") is None

        cleared = await client.put(path, json={"channels": []})
        assert cleared.status_code == 204
        assert (await client.get(path)).json() == {"channels": []}
        assert await store.listening_goal("T1", "C02") is None


async def test_listening_channels_reject_bad_input_and_other_workspaces(
    control_store: ControlStore, pg_dsn: str
) -> None:
    async with _client(_app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE))) as client:
        _, workspace = await _sign_in_as_admin(client, control_store)
        other = await control_store._connected_pool.fetchval(
            """INSERT INTO workspaces (slack_team_id, name, status, bot_token_encrypted)
            VALUES ('T2', 'Other', 'active', 'ciphertext') RETURNING id"""
        )
        path = f"/api/workspaces/{workspace}/agent/listening"

        for body in [
            {"channels": [{"channel": "general", "goal": "Help."}]},
            {"channels": [{"channel": "C01", "goal": "   "}]},
            {"channels": [{"channel": "C01", "goal": "x" * 1001}]},
            {
                "channels": [
                    {"channel": "C01", "goal": "Help."},
                    {"channel": "C01", "goal": "Help more."},
                ]
            },
        ]:
            assert (await client.put(path, json=body)).status_code == 422

        for response in [
            await client.get(f"/api/workspaces/{other}/agent/listening"),
            await client.put(
                f"/api/workspaces/{other}/agent/listening",
                json={"channels": [{"channel": "C01", "goal": "Help."}]},
            ),
        ]:
            assert response.status_code == 404
            assert response.json() == {"detail": "not_found"}
        assert (
            await control_store._connected_pool.fetchval(
                "SELECT listening_channels FROM workspaces WHERE id = $1", other
            )
            == "{}"
        )


async def test_only_admins_of_the_workspace_see_or_change_listening_channels(
    control_store: ControlStore, pg_dsn: str
) -> None:
    async with _client(_app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE))) as client:
        signed_out = await client.get(f"/api/workspaces/{uuid4()}/agent/listening")
        _, workspace = await _sign_in_as_admin(client, control_store)
        path = f"/api/workspaces/{workspace}/agent/listening"
        body = {"channels": [{"channel": "C01", "goal": "Help."}]}
        assert (await client.put(path, json=body)).status_code == 204
        await control_store._connected_pool.execute(
            "DELETE FROM workspace_admins WHERE workspace_id = $1", workspace
        )
        read = await client.get(path)
        written = await client.put(path, json={"channels": []})
        kept = await control_store.get_listening(workspace)

    assert signed_out.status_code == 401
    for response in (read, written):
        assert response.status_code == 404
        assert response.json() == {"detail": "not_found"}
    assert kept == {"C01": "Help."}


async def test_a_channel_cannot_be_mention_only_and_listening(
    control_store: ControlStore, store: PostgresStore, pg_dsn: str
) -> None:
    async with _client(_app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE))) as client:
        _, workspace = await _sign_in_as_admin(client, control_store, team="T1")
        listening = f"/api/workspaces/{workspace}/agent/listening"
        mention_only = f"/api/workspaces/{workspace}/agent/mention-only"
        goal = {"channel": "C01", "goal": "Answer deploy questions."}

        assert (await client.put(mention_only, json={"channels": ["C01"]})).status_code == 204
        refused = await client.put(listening, json={"channels": [goal]})
        assert refused.status_code == 409
        assert refused.json() == {"detail": "channel_conflict"}
        assert await store.listening_goal("T1", "C01") is None

        assert (await client.put(mention_only, json={"channels": ["C02"]})).status_code == 204
        assert (await client.put(listening, json={"channels": [goal]})).status_code == 204
        refused = await client.put(mention_only, json={"channels": ["C02", "C01"]})
        assert refused.status_code == 409
        assert refused.json() == {"detail": "channel_conflict"}
        assert (await client.get(mention_only)).json() == {"channels": ["C02"]}
        assert await store.listening_goal("T1", "C01") == "Answer deploy questions."
