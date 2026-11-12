from __future__ import annotations

from datetime import timedelta
from uuid import UUID

import asyncpg
import pytest

from hondo.api.store import ControlStore, User
from hondo.crypto import TokenCipher, generate_key


async def _user(control_store: ControlStore, slack_user_id: str = "U1", **hints: str) -> User:
    return await control_store.upsert_user(
        slack_team_id=hints.get("team", "T1"),
        slack_user_id=slack_user_id,
        email=hints.get("email", "user@example.com"),
        display_name=hints.get("name", ""),
        avatar_url=hints.get("avatar"),
        team_name=hints.get("team_name", ""),
    )


async def _workspace(control_store: ControlStore, team: str) -> UUID:
    return await control_store._connected_pool.fetchval(
        """
        INSERT INTO workspaces (slack_team_id, name, bot_token_encrypted)
        VALUES ($1, $2, 'ciphertext') RETURNING id
        """,
        team,
        f"Workspace {team}",
    )


async def test_upsert_user_refreshes_hints_and_keeps_values_slack_left_out(
    control_store: ControlStore,
) -> None:
    user = await _user(control_store, email="USER@example.com")
    assert user.email == "user@example.com"
    assert (user.display_name, user.avatar_url, user.slack_team_name) == ("", None, "")

    hinted = await _user(
        control_store,
        email="new@example.com",
        name="Slack Name",
        avatar="https://example.com/a.png",
        team_name="Acme",
    )
    assert hinted.id == user.id
    assert hinted.email == "new@example.com"
    assert hinted.display_name == "Slack Name"
    assert hinted.avatar_url == "https://example.com/a.png"
    assert hinted.slack_team_name == "Acme"

    hintless = await _user(control_store)
    assert hintless.display_name == "Slack Name"
    assert hintless.avatar_url == "https://example.com/a.png"
    assert hintless.slack_team_name == "Acme"


async def test_one_slack_account_is_one_user(control_store: ControlStore) -> None:
    first = await _user(control_store, "U1", email="same@example.com")
    second = await _user(control_store, "U2", email="same@example.com")
    other_team = await _user(control_store, "U1", team="T2", email="same@example.com")

    assert len({first.id, second.id, other_team.id}) == 3


async def test_auth_session_lifecycle_and_lazy_expiry_cleanup(
    control_store: ControlStore,
) -> None:
    user = await _user(control_store)
    await control_store.create_auth_session("valid", user.id, timedelta(hours=1))

    session_user = await control_store.get_session_user("valid")
    assert session_user is not None
    assert session_user.id == user.id

    await control_store.create_auth_session("expired", user.id, timedelta(seconds=-1))
    assert await control_store.get_session_user("expired") is None

    await control_store.create_auth_session("another-valid", user.id, timedelta(hours=1))
    expired_count = await control_store._connected_pool.fetchval(
        "SELECT count(*) FROM auth_sessions WHERE token_hash = 'expired'"
    )
    assert expired_count == 0

    await control_store.delete_auth_session("valid")
    assert await control_store.get_session_user("valid") is None


async def test_connections_are_scoped_to_their_workspace(control_store: ControlStore) -> None:
    await control_store._connected_pool.execute("TRUNCATE workspaces CASCADE")
    first = await _workspace(control_store, "T1")
    second = await _workspace(control_store, "T2")
    cipher = TokenCipher(generate_key())
    connection = await control_store.create_provider_connection(
        first, "vercel", "Production", cipher.encrypt("key")
    )
    # Names are unique per workspace, not across workspaces.
    await control_store.create_provider_connection(
        second, "vercel", "production", cipher.encrypt("key")
    )

    assert await control_store.set_agent_model(
        first, connection.id, "openai/gpt-4.1", reads_images=True
    )
    assert not await control_store.set_agent_model(
        second, connection.id, "openai/gpt-4.1", reads_images=True
    )
    # The database enforces the scope too, not only the queries.
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await control_store._connected_pool.execute(
            """
            INSERT INTO agents (workspace_id, name, provider_connection_id, model_name)
            VALUES ($1, 'other', $2, 'openai/gpt-4.1')
            """,
            second,
            connection.id,
        )
    assert await control_store.get_provider_connection(second, connection.id) is None
    assert not await control_store.delete_provider_connection(first, connection.id)

    await control_store._connected_pool.execute("DELETE FROM workspaces WHERE id = $1", first)
    assert await control_store.list_provider_connections(first) == []
    assert len(await control_store.list_provider_connections(second)) == 1
