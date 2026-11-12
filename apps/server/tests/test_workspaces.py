from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from hondo.api.store import ControlStore
from hondo.crypto import TokenCipher
from tests.test_auth import (
    CRYPTO_KEY,
    VERIFIED_JANE,
    FakeOIDCProvider,
    _app,
    _client,
    slack_account,
)


@pytest.fixture(autouse=True)
async def _isolate_workspace_tables(
    control_store: ControlStore,
) -> AsyncIterator[None]:
    # control_store only truncates the identity tables, so these three are
    # cleared here — every test in this module asserts on exact row sets.
    await control_store._connected_pool.execute(
        "TRUNCATE usage_events, sessions, workspaces RESTART IDENTITY CASCADE"
    )
    yield


def bot_token(team: str) -> str:
    return TokenCipher(CRYPTO_KEY).encrypt(f"xoxb-{team}")


async def _insert_workspace(
    control_store: ControlStore,
    *,
    team: str = "T1",
    name: str = "Acme",
    status: str = "active",
) -> UUID:
    workspace_id = uuid4()
    await control_store._connected_pool.execute(
        """
        INSERT INTO workspaces (id, slack_team_id, name, status, bot_token_encrypted)
        VALUES ($1, $2, $3, $4, $5)
        """,
        workspace_id,
        team,
        name,
        status,
        bot_token(team),
    )
    return workspace_id


async def _add_admin(control_store: ControlStore, workspace_id: UUID, slack_user_id: str) -> None:
    await control_store._connected_pool.execute(
        "INSERT INTO workspace_admins (workspace_id, slack_user_id) VALUES ($1, $2)",
        workspace_id,
        slack_user_id,
    )


async def _sign_in(client: httpx.AsyncClient) -> UUID:
    """Sign in as the app's fake Slack identity and return the user ID."""

    callback = await client.get("/api/auth/slack/callback?code=x&state=y")
    assert callback.status_code == 302, callback.headers.get("location")
    me = await client.get("/api/me")
    assert me.status_code == 200
    return UUID(me.json()["user"]["id"])


async def _sign_in_as_admin(
    client: httpx.AsyncClient,
    control_store: ControlStore,
    *,
    team: str = "T1",
    name: str = "Acme",
    slack_user_id: str = "U1",
    status: str = "active",
) -> tuple[UUID, UUID]:
    """Install a workspace with the signed-in Slack account as its admin.

    The app's fake identity must belong to `team` as `slack_user_id`.
    """

    workspace_id = await _insert_workspace(control_store, team=team, name=name, status=status)
    await _add_admin(control_store, workspace_id, slack_user_id)
    return await _sign_in(client), workspace_id


async def test_list_workspaces_returns_the_administered_workspace_only(
    control_store: ControlStore,
    pg_dsn: str,
) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store, status="paused")
        await _insert_workspace(control_store, team="T2", name="Other")

        response = await client.get("/api/workspaces")

        slack.identity = slack_account("U2")
        await _sign_in(client)
        not_admin = await client.get("/api/workspaces")

    assert response.status_code == 200
    assert response.json() == {
        "workspaces": [
            {
                "id": str(workspace_id),
                "slack_team_id": "T1",
                "name": "Acme",
                "status": "paused",
            }
        ]
    }
    assert not_admin.status_code == 200
    assert not_admin.json() == {"workspaces": []}


async def test_usage_rates_models_and_filters_the_month(
    control_store: ControlStore,
    pg_dsn: str,
) -> None:
    app = _app(control_store, pg_dsn, FakeOIDCProvider(identity=VERIFIED_JANE))
    async with _client(app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store, name="Usage")
        rows = [
            (
                "model_request",
                "vercel",
                "xai/grok-4.5",
                317_250,
                100_000,
                {"requests": 3},
                "priced-inside",
                datetime(2026, 7, 15, 12, tzinfo=UTC),
            ),
            (
                "model_request",
                "vercel",
                "unpriced-model",
                42,
                7,
                {"requests": 2},
                "unpriced-inside",
                datetime(2026, 7, 20, 12, tzinfo=UTC),
            ),
            (
                "model_request",
                "vercel",
                "xai/grok-4.5",
                9_000_000,
                9_000_000,
                {"requests": 99},
                "outside-window",
                datetime(2026, 8, 1, tzinfo=UTC),
            ),
            (
                "web_search",
                "vercel",
                "xai/grok-4.5",
                9_000_000,
                9_000_000,
                {"requests": 99},
                "wrong-kind",
                datetime(2026, 7, 10, tzinfo=UTC),
            ),
        ]
        for (
            kind,
            provider,
            resource,
            quantity_in,
            quantity_out,
            metadata,
            workflow_id,
            created_at,
        ) in rows:
            await control_store._connected_pool.execute(
                """
                INSERT INTO usage_events
                    (team, kind, provider, resource, quantity_in, quantity_out,
                     metadata, workflow_id, created_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
                """,
                "T1",
                kind,
                provider,
                resource,
                quantity_in,
                quantity_out,
                json.dumps(metadata),
                workflow_id,
                created_at,
            )

        response = await client.get(
            f"/api/workspaces/{workspace_id}/usage",
            params={"month": "2026-07"},
        )
        empty = await client.get(
            f"/api/workspaces/{workspace_id}/usage",
            params={"month": "2026-06"},
        )
        invalid_format = await client.get(
            f"/api/workspaces/{workspace_id}/usage",
            params={"month": "2026/07"},
        )
        invalid_month = await client.get(
            f"/api/workspaces/{workspace_id}/usage",
            params={"month": "2026-13"},
        )
        # The pattern allows any four-digit year; datetime does not.
        unrepresentable_month = await client.get(
            f"/api/workspaces/{workspace_id}/usage",
            params={"month": "9999-12"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "workspace_id": str(workspace_id),
        "month": "2026-07",
        "models": [
            {
                "model": "xai/grok-4.5",
                "provider": "vercel",
                "input_tokens": 317_250,
                "output_tokens": 100_000,
                "requests": 3,
                "cost_usd": "1.2345",
            },
            {
                "model": "unpriced-model",
                "provider": "vercel",
                "input_tokens": 42,
                "output_tokens": 7,
                "requests": 2,
                "cost_usd": None,
            },
        ],
        "total_cost_usd": "1.2345",
        "has_unpriced": True,
    }
    assert empty.status_code == 200
    assert empty.json() == {
        "workspace_id": str(workspace_id),
        "month": "2026-06",
        "models": [],
        "total_cost_usd": "0",
        "has_unpriced": False,
    }
    assert invalid_format.status_code == 422
    assert invalid_month.status_code == 422
    assert unrepresentable_month.status_code == 422


async def test_usage_without_month_reports_the_current_utc_month(
    control_store: ControlStore,
    pg_dsn: str,
) -> None:
    app = _app(control_store, pg_dsn, FakeOIDCProvider(identity=VERIFIED_JANE))
    async with _client(app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store, name="Default")
        # Stamped now, so the row lands in whatever month the default resolves
        # to — including when the suite runs on the 1st.
        await control_store._connected_pool.execute(
            """
            INSERT INTO usage_events
                (team, kind, provider, resource, quantity_in, quantity_out,
                 metadata, workflow_id)
            VALUES ($1, 'model_request', 'vercel', 'xai/grok-4.5', 1000, 500,
                    $2, 'default-month')
            """,
            "T1",
            json.dumps({"requests": 1}),
        )

        response = await client.get(f"/api/workspaces/{workspace_id}/usage")

    assert response.status_code == 200
    body = response.json()
    assert body["month"] == datetime.now(UTC).strftime("%Y-%m")
    assert [(model["model"], model["input_tokens"]) for model in body["models"]] == [
        ("xai/grok-4.5", 1000)
    ]


async def test_activity_is_newest_first_and_validates_limit(
    control_store: ControlStore,
    pg_dsn: str,
) -> None:
    app = _app(control_store, pg_dsn, FakeOIDCProvider(identity=VERIFIED_JANE))
    async with _client(app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store, name="Activity")
        sessions = [
            (
                "T1",
                "C-OLD",
                "1700000001.000001",
                "idle",
                datetime(2026, 7, 1, 8, tzinfo=UTC),
                datetime(2026, 7, 1, 9, tzinfo=UTC),
            ),
            (
                "T1",
                "C-NEW",
                "1700000003.000003",
                "running",
                datetime(2026, 7, 3, 8, tzinfo=UTC),
                datetime(2026, 7, 3, 9, tzinfo=UTC),
            ),
            (
                "T1",
                "C-MIDDLE",
                "1700000002.000002",
                "paused_on_approval",
                datetime(2026, 7, 2, 8, tzinfo=UTC),
                datetime(2026, 7, 2, 9, tzinfo=UTC),
            ),
            (
                "T-OTHER",
                "C-OTHER",
                "1700000004.000004",
                "running",
                datetime(2026, 7, 4, 8, tzinfo=UTC),
                datetime(2026, 7, 4, 9, tzinfo=UTC),
            ),
        ]
        await control_store._connected_pool.executemany(
            """
            INSERT INTO sessions
                (team, channel, thread_root, state, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            sessions,
        )

        response = await client.get(
            f"/api/workspaces/{workspace_id}/activity",
            params={"limit": 2},
        )
        zero = await client.get(
            f"/api/workspaces/{workspace_id}/activity",
            params={"limit": 0},
        )
        too_large = await client.get(
            f"/api/workspaces/{workspace_id}/activity",
            params={"limit": 201},
        )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "channel": "C-NEW",
                "thread_root": "1700000003.000003",
                "state": "running",
                "created_at": "2026-07-03T08:00:00Z",
                "updated_at": "2026-07-03T09:00:00Z",
            },
            {
                "channel": "C-MIDDLE",
                "thread_root": "1700000002.000002",
                "state": "paused_on_approval",
                "created_at": "2026-07-02T08:00:00Z",
                "updated_at": "2026-07-02T09:00:00Z",
            },
        ]
    }
    assert zero.status_code == 422
    assert too_large.status_code == 422


async def test_workspace_routes_hide_existence_and_require_authentication(
    control_store: ControlStore,
    pg_dsn: str,
) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        _, own_workspace_id = await _sign_in_as_admin(client, control_store)
        other_workspace_id = await _insert_workspace(control_store, team="T2", name="Other")
        await _add_admin(control_store, other_workspace_id, "U1")

        for workspace_id in (other_workspace_id, uuid4()):
            usage = await client.get(
                f"/api/workspaces/{workspace_id}/usage", params={"month": "2026-07"}
            )
            activity = await client.get(f"/api/workspaces/{workspace_id}/activity")
            assert usage.status_code == 404
            assert usage.json() == {"detail": "not_found"}
            assert activity.status_code == 404
            assert activity.json() == {"detail": "not_found"}

        slack.identity = slack_account("U2")
        await _sign_in(client)
        not_admin = await client.get(f"/api/workspaces/{own_workspace_id}/activity")

        client.cookies.clear()
        workspaces = await client.get("/api/workspaces")
        usage = await client.get(
            f"/api/workspaces/{own_workspace_id}/usage", params={"month": "2026-07"}
        )
        activity = await client.get(f"/api/workspaces/{own_workspace_id}/activity")

    assert not_admin.status_code == 404
    assert not_admin.json() == {"detail": "not_found"}
    assert workspaces.status_code == 401
    assert workspaces.json() == {"detail": "unauthorized"}
    assert usage.status_code == 401
    assert usage.json() == {"detail": "unauthorized"}
    assert activity.status_code == 401
    assert activity.json() == {"detail": "unauthorized"}
