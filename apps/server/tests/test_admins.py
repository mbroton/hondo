from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from uuid import UUID

import httpx
import pytest
from httpx import ASGITransport

from hondo.api.access import (
    AdminAccess,
    SlackMember,
    SlackMemberCache,
    SlackRateLimited,
    member_from_payload,
)
from hondo.api.admins import WorkspaceAdmins
from hondo.api.store import AdminCaller, ControlStore
from hondo.crypto import TokenCipher
from tests.test_auth import (
    APP_URL,
    CRYPTO_KEY,
    VERIFIED_JANE,
    FakeOIDCProvider,
    FakeSlackDirectory,
    _app,
    _client,
    slack_account,
)
from tests.test_workspaces import _add_admin, _insert_workspace, _sign_in


@pytest.fixture(autouse=True)
async def _isolate_workspaces(control_store: ControlStore) -> AsyncIterator[None]:
    await control_store._connected_pool.execute(
        "TRUNCATE usage_events, sessions, workspaces RESTART IDENTITY CASCADE"
    )
    yield


async def _admin_rows(control_store: ControlStore, workspace_id: UUID) -> list[tuple[str, UUID]]:
    records = await control_store._connected_pool.fetch(
        """
        SELECT slack_user_id, added_by FROM workspace_admins
        WHERE workspace_id = $1 ORDER BY slack_user_id
        """,
        workspace_id,
    )
    return [(record["slack_user_id"], record["added_by"]) for record in records]


async def test_the_admin_list_shows_each_admin_as_slack_sees_them(
    control_store: ControlStore, pg_dsn: str
) -> None:
    directory = FakeSlackDirectory()
    directory.set("U1", name="Jane", avatar_url="https://avatars.example/jane.png")
    directory.set("U2", name="Bob")
    directory.set("U3", name="Gone", active=False)
    directory.remove("U4")
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack, directory=directory)) as client:
        workspace_id = await _insert_workspace(control_store)
        await _add_admin(control_store, workspace_id, "U1")
        await _sign_in(client)
        added = await client.post(
            f"/api/workspaces/{workspace_id}/admins", json={"slack_user_id": "U2"}
        )
        for slack_user_id in ("U3", "U4"):
            await _add_admin(control_store, workspace_id, slack_user_id)
        response = await client.get(f"/api/workspaces/{workspace_id}/admins")

    assert added.status_code == 204
    assert response.status_code == 200
    admins = response.json()["admins"]
    for admin in admins:
        datetime.fromisoformat(admin.pop("created_at"))
    assert admins == [
        {
            "slack_user_id": "U1",
            "name": "Jane",
            "avatar_url": "https://avatars.example/jane.png",
            "status": "active",
            "added_by": None,
        },
        {
            "slack_user_id": "U2",
            "name": "Bob",
            "avatar_url": None,
            "status": "active",
            "added_by": "Jane Doe",
        },
        {
            "slack_user_id": "U3",
            "name": "Gone",
            "avatar_url": None,
            "status": "inactive",
            "added_by": None,
        },
        {
            "slack_user_id": "U4",
            "name": None,
            "avatar_url": None,
            "status": "inactive",
            "added_by": None,
        },
    ]


async def test_a_slack_failure_marks_admins_unknown_without_failing_the_list(
    control_store: ControlStore, pg_dsn: str
) -> None:
    directory = FakeSlackDirectory()
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack, directory=directory)) as client:
        workspace_id = await _insert_workspace(control_store)
        for slack_user_id in ("U1", "U2", "U3"):
            await _add_admin(control_store, workspace_id, slack_user_id)
        await _sign_in(client)
        directory.error = RuntimeError("slack is down")
        calls_before = len(directory.calls)
        response = await client.get(f"/api/workspaces/{workspace_id}/admins")
        calls_during_list = len(directory.calls) - calls_before

    assert response.status_code == 200
    assert [(admin["slack_user_id"], admin["status"]) for admin in response.json()["admins"]] == [
        # U1 signed in moments ago, so Slack's answer about them is still fresh.
        ("U1", "active"),
        ("U2", "unknown"),
        ("U3", "unknown"),
    ]
    assert response.json()["admins"][1]["name"] is None
    # After the first failure the list stops waiting on Slack for each row.
    assert calls_during_list == 1


async def test_slack_members_lists_active_team_members_page_by_page(
    control_store: ControlStore, pg_dsn: str
) -> None:
    directory = FakeSlackDirectory()
    directory.listing = [
        SlackMember(id="U1", name="Jane", admin=False, active=True),
        SlackMember(id="U5", name="Guest", admin=False, active=False),
        SlackMember(id="B1", name="Some bot", admin=False, active=False),
        SlackMember(
            id="U2",
            name="Bob",
            admin=False,
            active=True,
            avatar_url="https://b.png",
            real_name="Robert Smith",
            handle="rsmith",
        ),
        SlackMember(id="U3", name="Kim", admin=False, active=True),
    ]
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack, directory=directory)) as client:
        workspace_id = await _insert_workspace(control_store)
        await _add_admin(control_store, workspace_id, "U1")
        await _sign_in(client)
        path = f"/api/workspaces/{workspace_id}/slack-members"
        pages = [(await client.get(path)).json()]
        while pages[-1]["next_cursor"] is not None:
            pages.append(
                (await client.get(path, params={"cursor": pages[-1]["next_cursor"]})).json()
            )

    assert pages == [
        {
            "members": [
                {
                    "slack_user_id": "U1",
                    "name": "Jane",
                    "real_name": None,
                    "handle": None,
                    "avatar_url": None,
                }
            ],
            "next_cursor": "2",
        },
        {
            "members": [
                {
                    "slack_user_id": "U2",
                    "name": "Bob",
                    "real_name": "Robert Smith",
                    "handle": "rsmith",
                    "avatar_url": "https://b.png",
                }
            ],
            "next_cursor": "4",
        },
        {
            "members": [
                {
                    "slack_user_id": "U3",
                    "name": "Kim",
                    "real_name": None,
                    "handle": None,
                    "avatar_url": None,
                }
            ],
            "next_cursor": None,
        },
    ]
    assert directory.list_calls == [None, "2", "4"]


async def test_slack_members_reports_when_slack_does_not_answer(
    control_store: ControlStore, pg_dsn: str
) -> None:
    directory = FakeSlackDirectory()
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack, directory=directory)) as client:
        workspace_id = await _insert_workspace(control_store)
        await _add_admin(control_store, workspace_id, "U1")
        await _sign_in(client)
        path = f"/api/workspaces/{workspace_id}/slack-members"
        directory.error = SlackRateLimited(30)
        limited = await client.get(path)
        directory.error = RuntimeError("slack is down")
        down = await client.get(path)

    assert (limited.status_code, limited.json()) == (429, {"detail": "rate_limited"})
    assert limited.headers["retry-after"] == "30"
    assert (down.status_code, down.json()) == (503, {"detail": "unavailable"})
    assert "retry-after" not in down.headers


async def test_adding_an_admin(control_store: ControlStore, pg_dsn: str) -> None:
    directory = FakeSlackDirectory()
    directory.set("U5", name="Guest", active=False)
    directory.remove("U6")
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack, directory=directory)) as client:
        workspace_id = await _insert_workspace(control_store)
        await _add_admin(control_store, workspace_id, "U1")
        jane_id = await _sign_in(client)
        path = f"/api/workspaces/{workspace_id}/admins"
        first = await client.post(path, json={"slack_user_id": "U2"})
        again = await client.post(path, json={"slack_user_id": "U2"})
        guest = await client.post(path, json={"slack_user_id": "U5"})
        unknown = await client.post(path, json={"slack_user_id": "U6"})
        malformed = await client.post(path, json={"slack_user_id": "u2"})
        directory.error = RuntimeError("slack is down")
        slack_down = await client.post(path, json={"slack_user_id": "U7"})
        directory.error = None
        rows = await _admin_rows(control_store, workspace_id)
        # The new admin needs no invite: signing in with Slack is enough.
        slack.identity = slack_account("U2")
        await _sign_in(client)
        bob = await client.get("/api/me")

    assert (first.status_code, again.status_code) == (204, 204)
    for response in (guest, unknown):
        assert (response.status_code, response.json()) == (400, {"detail": "member_unavailable"})
    assert malformed.status_code == 422
    assert (slack_down.status_code, slack_down.json()) == (503, {"detail": "unavailable"})
    assert rows == [("U1", None), ("U2", jane_id)]
    assert bob.json()["team"]["is_admin"] is True


async def test_removing_admins(control_store: ControlStore, pg_dsn: str) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        workspace_id = await _insert_workspace(control_store)
        for slack_user_id in ("U1", "U2"):
            await _add_admin(control_store, workspace_id, slack_user_id)
        await _sign_in(client)
        path = f"/api/workspaces/{workspace_id}/admins"
        not_listed = await client.delete(f"{path}/U9")
        removed = await client.delete(f"{path}/U2")
        last = await client.delete(f"{path}/U1")
        rows = await _admin_rows(control_store, workspace_id)

    assert (not_listed.status_code, not_listed.json()) == (404, {"detail": "not_found"})
    assert removed.status_code == 204
    assert (last.status_code, last.json()) == (409, {"detail": "last_admin"})
    assert rows == [("U1", None)]


async def test_removing_yourself_ends_your_access_at_the_next_request(
    control_store: ControlStore, pg_dsn: str
) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        workspace_id = await _insert_workspace(control_store)
        for slack_user_id in ("U1", "U2"):
            await _add_admin(control_store, workspace_id, slack_user_id)
        await _sign_in(client)
        removed = await client.delete(f"/api/workspaces/{workspace_id}/admins/U1")
        after = await client.get(f"/api/workspaces/{workspace_id}/admins")

    assert removed.status_code == 204
    assert (after.status_code, after.json()) == (404, {"detail": "not_found"})


async def test_a_removed_admin_loses_access_at_once(
    control_store: ControlStore, pg_dsn: str
) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    app = _app(control_store, pg_dsn, slack)
    async with _client(app) as jane:
        workspace_id = await _insert_workspace(control_store)
        for slack_user_id in ("U1", "U2"):
            await _add_admin(control_store, workspace_id, slack_user_id)
        await _sign_in(jane)
        async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url=APP_URL) as bob:
            slack.identity = slack_account("U2")
            await _sign_in(bob)
            path = f"/api/workspaces/{workspace_id}/admins"
            before = await bob.get(path)
            removed = await jane.delete(f"{path}/U2")
            after = await bob.get(path)

    assert before.status_code == 200
    assert removed.status_code == 204
    # Slack's answer about Bob is still cached; the admin list is read each time.
    assert after.status_code == 404


async def test_concurrent_removals_keep_one_listed_admin(control_store: ControlStore) -> None:
    workspace_id = await _insert_workspace(control_store)
    for slack_user_id in ("U1", "U2"):
        await _add_admin(control_store, workspace_id, slack_user_id)
    slack_owner = AdminCaller(slack_user_id="U9", slack_admin=True)
    other = ControlStore(control_store.dsn)
    await other.connect(run_migrations=False, pool_min_size=1, pool_max_size=1)
    try:
        results = await asyncio.gather(
            control_store.remove_admin(workspace_id, "U1", slack_owner),
            other.remove_admin(workspace_id, "U2", slack_owner),
        )
    finally:
        await other.aclose()

    assert sorted(results) == ["last_admin", "removed"]
    assert len(await _admin_rows(control_store, workspace_id)) == 1


async def test_two_admins_removing_each_other_at_once_leave_one(
    control_store: ControlStore,
) -> None:
    workspace_id = await _insert_workspace(control_store)
    for slack_user_id in ("U1", "U2", "U3"):
        await _add_admin(control_store, workspace_id, slack_user_id)
    other = ControlStore(control_store.dsn)
    await other.connect(run_migrations=False, pool_min_size=1, pool_max_size=1)
    try:
        results = await asyncio.gather(
            control_store.remove_admin(
                workspace_id, "U2", AdminCaller(slack_user_id="U1", slack_admin=False)
            ),
            other.remove_admin(
                workspace_id, "U1", AdminCaller(slack_user_id="U2", slack_admin=False)
            ),
        )
    finally:
        await other.aclose()

    # Whoever goes second was removed a moment ago and may not act anymore.
    assert sorted(results) == ["no_access", "removed"]
    assert len(await _admin_rows(control_store, workspace_id)) == 2


async def test_only_a_current_admin_can_add_one(control_store: ControlStore) -> None:
    workspace_id = await _insert_workspace(control_store)
    await _add_admin(control_store, workspace_id, "U1")
    caller = await control_store.upsert_user(
        slack_team_id="T1",
        slack_user_id="U2",
        email="u2@example.com",
        display_name="Bob",
        avatar_url=None,
        team_name="Acme",
    )
    removed_admin = AdminCaller(slack_user_id="U2", slack_admin=False)
    slack_owner = AdminCaller(slack_user_id="U9", slack_admin=True)

    refused = await control_store.add_admin(workspace_id, "U5", removed_admin, caller.id)
    accepted = await control_store.add_admin(workspace_id, "U6", slack_owner, caller.id)

    assert (refused, accepted) == (False, True)
    assert [row[0] for row in await _admin_rows(control_store, workspace_id)] == ["U1", "U6"]


def test_slack_profiles_carry_an_avatar() -> None:
    def avatar(profile: dict[str, str]) -> str | None:
        payload = {"id": "U1", "name": "jane", "team_id": "T1", "profile": profile}
        return member_from_payload(payload, "T1").avatar_url

    assert avatar({"image_72": "https://a/72.png", "image_48": "https://a/48.png"}) == (
        "https://a/72.png"
    )
    assert avatar({"image_48": "https://a/48.png"}) == "https://a/48.png"
    assert avatar({}) is None


async def test_an_admin_slack_now_reports_inactive_cannot_change_admins(
    control_store: ControlStore,
) -> None:
    workspace_id = await _insert_workspace(control_store)
    for slack_user_id in ("U1", "U2"):
        await _add_admin(control_store, workspace_id, slack_user_id)
    caller = await control_store.upsert_user(
        slack_team_id="T1",
        slack_user_id="U1",
        email="u1@example.com",
        display_name="Jane",
        avatar_url=None,
        team_name="Acme",
    )
    # Deactivated after the access check; the cached answer expired mid-request.
    directory = FakeSlackDirectory()
    directory.set("U1", active=False)
    access = AdminAccess(control_store, TokenCipher(CRYPTO_KEY), directory, SlackMemberCache())
    team = await control_store.team_workspace("T1")
    assert team is not None
    admins = WorkspaceAdmins(control_store, access)

    added = await admins.add(team, "U5", caller)
    removed = await admins.remove(team, "U2", caller)

    assert (added, removed) == ("no_access", "no_access")
    assert [row[0] for row in await _admin_rows(control_store, workspace_id)] == ["U1", "U2"]
