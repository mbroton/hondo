"""Mention-only channels: admins save them in the API, the runtime reads them."""

from __future__ import annotations

from hondo.api.store import ControlStore
from hondo.store.postgres import PostgresStore
from tests.test_auth import VERIFIED_JANE, FakeOIDCProvider, _app, _client
from tests.test_workspaces import _sign_in_as_admin


async def test_admin_saves_mention_only_channels_and_the_runtime_reads_them(
    control_store: ControlStore, store: PostgresStore, pg_dsn: str
) -> None:
    async with _client(_app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE))) as client:
        _, workspace = await _sign_in_as_admin(client, control_store, team="T1")
        path = f"/api/workspaces/{workspace}/agent/mention-only"

        initial = await client.get(path)
        assert initial.status_code == 200
        assert initial.json() == {"channels": []}
        assert not await store.mention_only_channel("T1", "C02")

        saved = await client.put(path, json={"channels": ["C02", "C01", "C02"]})
        assert saved.status_code == 204
        assert (await client.get(path)).json() == {"channels": ["C01", "C02"]}
        assert await store.mention_only_channel("T1", "C02")
        assert not await store.mention_only_channel("T1", "C03")
        assert not await store.mention_only_channel("T2", "C02")

        cleared = await client.put(path, json={"channels": []})
        assert cleared.status_code == 204
        assert (await client.get(path)).json() == {"channels": []}
        assert not await store.mention_only_channel("T1", "C02")


async def test_mention_only_channels_reject_bad_input_and_other_workspaces(
    control_store: ControlStore, pg_dsn: str
) -> None:
    async with _client(_app(control_store, pg_dsn, FakeOIDCProvider(VERIFIED_JANE))) as client:
        _, workspace = await _sign_in_as_admin(client, control_store)
        other = await control_store._connected_pool.fetchval(
            """INSERT INTO workspaces (slack_team_id, name, status, bot_token_encrypted)
            VALUES ('T2', 'Other', 'active', 'ciphertext') RETURNING id"""
        )

        bad = await client.put(
            f"/api/workspaces/{workspace}/agent/mention-only",
            json={"channels": ["general"]},
        )
        assert bad.status_code == 422

        for response in [
            await client.get(f"/api/workspaces/{other}/agent/mention-only"),
            await client.put(
                f"/api/workspaces/{other}/agent/mention-only",
                json={"channels": ["C01"]},
            ),
        ]:
            assert response.status_code == 404
            assert response.json() == {"detail": "not_found"}
        assert (
            await control_store._connected_pool.fetchval(
                "SELECT mention_only_channels FROM workspaces WHERE id = $1", other
            )
            == []
        )
