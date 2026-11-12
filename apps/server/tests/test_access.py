from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from uuid import uuid4

import pytest
from fastapi import FastAPI

from hondo.api.access import SlackMemberCache, SlackTokenRejected, member_from_payload
from hondo.api.store import ControlStore
from tests.test_auth import (
    VERIFIED_JANE,
    FakeOIDCProvider,
    FakeSlackDirectory,
    _app,
    _client,
    slack_account,
)
from tests.test_workspaces import _add_admin, _insert_workspace, _sign_in, _sign_in_as_admin


@pytest.fixture(autouse=True)
async def _isolate_workspaces(control_store: ControlStore) -> AsyncIterator[None]:
    await control_store._connected_pool.execute(
        "TRUNCATE usage_events, sessions, workspaces RESTART IDENTITY CASCADE"
    )
    yield


def _expire_answers(app: FastAPI) -> None:
    """As if CHECK_MAX_AGE passed: every answer from Slack is too old to reuse."""

    app.state.slack_members = SlackMemberCache()


async def test_slack_is_asked_again_only_when_its_answer_is_old(
    control_store: ControlStore, pg_dsn: str
) -> None:
    directory = FakeSlackDirectory()
    app = _app(control_store, pg_dsn, FakeOIDCProvider(identity=VERIFIED_JANE), directory=directory)
    async with _client(app) as client:
        await _sign_in_as_admin(client, control_store)
        after_sign_in = len(directory.calls)
        for _ in range(3):
            assert (await client.get("/api/workspaces")).json()["workspaces"]
        cached = len(directory.calls)
        _expire_answers(app)
        assert (await client.get("/api/workspaces")).json()["workspaces"]

    assert after_sign_in == 1
    assert directory.calls[0] == ("xoxb-T1", "U1")
    assert cached == 1
    assert len(directory.calls) == 2


@pytest.mark.parametrize(
    "change", ["deactivated", "guest", "removed_from_slack", "slack_admin_demoted"]
)
async def test_slack_changes_end_access_at_the_next_check(
    control_store: ControlStore, pg_dsn: str, change: str
) -> None:
    directory = FakeSlackDirectory()
    listed = change != "slack_admin_demoted"
    directory.set("U1", admin=not listed)
    app = _app(control_store, pg_dsn, FakeOIDCProvider(identity=VERIFIED_JANE), directory=directory)
    async with _client(app) as client:
        workspace_id = await _insert_workspace(control_store)
        if listed:
            await _add_admin(control_store, workspace_id, "U1")
        await _sign_in(client)
        before = await client.get(f"/api/workspaces/{workspace_id}/activity")

        if change == "removed_from_slack":
            directory.remove("U1")
        else:
            directory.set("U1", admin=False, active=change == "slack_admin_demoted")
        still_cached = await client.get(f"/api/workspaces/{workspace_id}/activity")
        _expire_answers(app)
        after = await client.get(f"/api/workspaces/{workspace_id}/activity")
        me = await client.get("/api/me")

    assert before.status_code == 200
    assert still_cached.status_code == 200
    assert after.status_code == 404
    assert after.json() == {"detail": "not_found"}
    assert me.json()["team"]["is_admin"] is False


async def test_a_slack_outage_refuses_access_and_says_why(
    control_store: ControlStore, pg_dsn: str
) -> None:
    directory = FakeSlackDirectory()
    directory.set("U1", admin=True)
    app = _app(control_store, pg_dsn, FakeOIDCProvider(identity=VERIFIED_JANE), directory=directory)
    async with _client(app) as client:
        workspace_id = await _insert_workspace(control_store)
        await _sign_in(client)
        usage = f"/api/workspaces/{workspace_id}/usage"
        assert (await client.get(usage)).status_code == 200
        directory.error = RuntimeError("slack is down")
        recent_answer = await client.get(usage)
        _expire_answers(app)
        during_outage = await client.get(usage)
        me = await client.get("/api/me")
        directory.error = None
        after_outage = await client.get(usage)

    assert recent_answer.status_code == 200
    # No answer from Slack means no access: Hondo keeps no copy of Slack roles.
    assert during_outage.status_code == 404
    assert me.json()["team"] == {
        "id": "T1",
        "name": "Acme",
        "workspace_id": str(workspace_id),
        "is_admin": False,
        "admins": [],
        "slack": "unavailable",
    }
    assert after_outage.status_code == 200


async def test_a_rejected_bot_token_refuses_access_until_slack_accepts_it_again(
    control_store: ControlStore, pg_dsn: str
) -> None:
    directory = FakeSlackDirectory()
    app = _app(control_store, pg_dsn, FakeOIDCProvider(identity=VERIFIED_JANE), directory=directory)
    async with _client(app) as client:
        workspace_id = await _insert_workspace(control_store)
        await _add_admin(control_store, workspace_id, "U1")
        await _sign_in(client)
        usage = f"/api/workspaces/{workspace_id}/usage"
        assert (await client.get(usage)).status_code == 200
        # Hondo was removed from Slack, or Slack's IP rules block this server.
        directory.error = SlackTokenRejected("invalid_auth")
        _expire_answers(app)
        rejected = await client.get(usage)
        me = await client.get("/api/me")
        # Nothing is saved, so access returns as soon as Slack accepts the token.
        directory.error = None
        after = await client.get(usage)
        status = await control_store._connected_pool.fetchval(
            "SELECT status FROM workspaces WHERE id = $1", workspace_id
        )

    assert rejected.status_code == 404
    assert me.json()["team"]["slack"] == "reinstall_required"
    assert me.json()["team"]["admins"] == []
    assert after.status_code == 200
    # The runtime's status is not touched by what the dashboard sees.
    assert status == "active"


async def test_a_sign_in_asks_slack_again(control_store: ControlStore, pg_dsn: str) -> None:
    directory = FakeSlackDirectory()
    slack = FakeOIDCProvider(identity=slack_account("U5"))
    app = _app(control_store, pg_dsn, slack, directory=directory)
    async with _client(app) as client:
        await _insert_workspace(control_store)
        await _sign_in(client)
        before = (await client.get("/api/me")).json()["team"]["is_admin"]
        directory.set("U5", admin=True)
        await _sign_in(client)
        after = (await client.get("/api/me")).json()["team"]["is_admin"]

    assert (before, after) == (False, True)


async def test_slack_owners_and_admins_get_access_without_being_listed(
    control_store: ControlStore, pg_dsn: str
) -> None:
    directory = FakeSlackDirectory()
    directory.set("U5", admin=True)
    slack = FakeOIDCProvider(identity=slack_account("U5"))
    app = _app(control_store, pg_dsn, slack, directory=directory)
    async with _client(app) as client:
        workspace_id = await _insert_workspace(control_store)
        await _add_admin(control_store, workspace_id, "U1")
        await _sign_in(client)
        me = await client.get("/api/me")
        listed = await client.get("/api/workspaces")

    assert me.json()["team"] == {
        "id": "T1",
        "name": "Acme",
        "workspace_id": str(workspace_id),
        "is_admin": True,
        "admins": [],
        "slack": "ok",
    }
    assert [item["id"] for item in listed.json()["workspaces"]] == [str(workspace_id)]


async def test_people_without_access_see_whom_to_ask(
    control_store: ControlStore, pg_dsn: str
) -> None:
    directory = FakeSlackDirectory()
    directory.set("U1", name="Jane")
    directory.set("U3", name="Gone", active=False)
    directory.set("U4", name="Kim")
    slack = FakeOIDCProvider(identity=slack_account("U2"))
    app = _app(control_store, pg_dsn, slack, directory=directory)
    async with _client(app) as client:
        workspace_id = await _insert_workspace(control_store)
        for admin in ("U1", "U3", "U4"):
            await _add_admin(control_store, workspace_id, admin)
        await _sign_in(client)
        me = await client.get("/api/me")
        workspaces = await client.get("/api/workspaces")

    assert me.json()["team"] == {
        "id": "T1",
        "name": "Acme",
        "workspace_id": str(workspace_id),
        "is_admin": False,
        "admins": ["Jane", "Kim"],
        "slack": "ok",
    }
    assert workspaces.json() == {"workspaces": []}


async def test_guests_and_deactivated_people_do_not_see_the_admins(
    control_store: ControlStore, pg_dsn: str
) -> None:
    directory = FakeSlackDirectory()
    directory.set("U6", active=False)
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    app = _app(control_store, pg_dsn, slack, directory=directory)
    async with _client(app) as client:
        workspace_id = await _insert_workspace(control_store)
        await _add_admin(control_store, workspace_id, "U1")
        await _sign_in(client)
        # Slack usually hides the member list from guests; so does Hondo.
        slack.identity = slack_account("U6")
        await _sign_in(client)
        me = await client.get("/api/me")

    assert me.json()["team"]["is_admin"] is False
    assert me.json()["team"]["admins"] == []


async def test_an_admin_in_one_team_has_no_access_to_another_with_the_same_email(
    control_store: ControlStore, pg_dsn: str
) -> None:
    # Another Slack workspace may give any member this email address.
    slack = FakeOIDCProvider(identity=replace(VERIFIED_JANE, team_id="T2", slack_user_id="U1"))
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        await _sign_in(client)
        workspace_id = await _insert_workspace(control_store)
        await _add_admin(control_store, workspace_id, "U1")
        response = await client.get(f"/api/workspaces/{workspace_id}/activity")
        me = await client.get("/api/me")

    assert response.status_code == 404
    assert me.json()["team"]["workspace_id"] is None


async def test_every_workspace_route_is_hidden_from_people_who_are_not_admins(
    control_store: ControlStore, pg_dsn: str
) -> None:
    slack = FakeOIDCProvider(identity=slack_account("U2"))
    app = _app(control_store, pg_dsn, slack)
    routes = [
        (method.upper(), path)
        for path, operations in app.openapi()["paths"].items()
        if "{workspace_id}" in path
        for method in operations
    ]
    async with _client(app) as client:
        workspace_id = await _insert_workspace(control_store)
        await _add_admin(control_store, workspace_id, "U1")
        await _sign_in(client)
        responses = {
            (method, path): await client.request(
                method,
                path.replace("{workspace_id}", str(workspace_id)).replace(
                    "{connection_id}", str(uuid4())
                ),
                json={},
            )
            for method, path in routes
        }

    assert {
        ("GET", "/api/workspaces/{workspace_id}/usage"),
        ("PUT", "/api/workspaces/{workspace_id}/agent/model"),
        ("POST", "/api/workspaces/{workspace_id}/provider-connections"),
        ("DELETE", "/api/workspaces/{workspace_id}/mcp-connections/{connection_id}"),
    } <= set(routes)
    for key, response in responses.items():
        assert (response.status_code, response.json()) == (404, {"detail": "not_found"}), key


def test_slack_profiles_map_to_members() -> None:
    def member(**fields: object) -> tuple[str, bool, bool]:
        parsed = member_from_payload({"id": "U1", "name": "jane", "team_id": "T1", **fields}, "T1")
        return parsed.name, parsed.admin, parsed.active

    assert member() == ("jane", False, True)
    assert member(real_name="Jane Doe", profile={"display_name": "JD"}) == ("JD", False, True)
    assert member(real_name="Jane Doe", profile={"display_name": ""}) == ("Jane Doe", False, True)
    for flag in ("is_admin", "is_owner", "is_primary_owner"):
        assert member(**{flag: True})[1] is True
    for flag in ("deleted", "is_bot", "is_restricted", "is_ultra_restricted"):
        assert member(**{flag: True})[2] is False
    slackbot = {"id": "USLACKBOT", "name": "slackbot", "team_id": "T1"}
    assert member_from_payload(slackbot, "T1").active is False


def test_a_member_keeps_every_name_slack_knows() -> None:
    payload = {
        "id": "U1",
        "team_id": "T1",
        "name": "rgarcia",
        "real_name": "Rosa García",
        "profile": {"display_name": "rosa"},
    }
    parsed = member_from_payload(payload, "T1")
    assert (parsed.name, parsed.real_name, parsed.handle) == ("rosa", "Rosa García", "rgarcia")

    blank = member_from_payload(
        {
            "id": "U2",
            "team_id": "T1",
            "name": "",
            "real_name": " ",
            "profile": {"display_name": " "},
        },
        "T1",
    )
    assert (blank.name, blank.real_name, blank.handle) == ("U2", None, None)


def test_only_members_of_the_team_are_active() -> None:
    # In an Enterprise org, users.info answers for people in other teams too.
    def active(**fields: object) -> bool:
        return member_from_payload({"id": "U1", "name": "jane", **fields}, "T1").active

    assert active(team_id="T1") is True
    assert active(team_id="T2") is False
    assert active(team_id="E1", enterprise_user={"teams": ["T2", "T1"]}) is True
    assert active(team_id="E1", enterprise_user={"teams": ["T2"]}) is False
    assert active() is False
