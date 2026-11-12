from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest
from fastapi import FastAPI

from hondo.api import slack_oauth
from hondo.api.app import create_app
from hondo.api.slack_oauth import (
    BOT_SCOPES,
    SlackInstallation,
    SlackInstallError,
    SlackOAuthInstaller,
)
from hondo.api.store import ControlStore
from hondo.config import Config
from hondo.crypto import TokenCipher, generate_key
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
from tests.test_workspaces import _add_admin, _insert_workspace, _sign_in, bot_token

CALLBACK_URI = f"{APP_URL}/api/slack/install/callback"


@pytest.fixture(autouse=True)
async def _isolate_workspaces(control_store: ControlStore) -> AsyncIterator[None]:
    await control_store._connected_pool.execute(
        "TRUNCATE usage_events, sessions, workspaces RESTART IDENTITY CASCADE"
    )
    yield


class FakeInstaller:
    def __init__(
        self,
        installation: SlackInstallation | None = None,
        error: Exception | None = None,
    ) -> None:
        self.installation = installation
        self.error = error
        self.authorize_calls: list[tuple[str, str]] = []
        self.exchange_calls: list[tuple[str, str]] = []

    def authorize_url(self, *, redirect_uri: str, state: str) -> str:
        self.authorize_calls.append((redirect_uri, state))
        return f"https://slack.example/oauth?state={state}"

    async def exchange(self, *, code: str, redirect_uri: str) -> SlackInstallation:
        self.exchange_calls.append((code, redirect_uri))
        if self.error is not None:
            raise self.error
        assert self.installation is not None
        return self.installation


def _cipher() -> TokenCipher:
    return TokenCipher(generate_key())


def _installation(
    *, team: str = "T1", name: str = "Acme Slack", installer: str = "U1", token: str = "granted"
) -> SlackInstallation:
    return SlackInstallation(
        team_id=team,
        team_name=name,
        bot_token_encrypted=TokenCipher(CRYPTO_KEY).encrypt(f"xoxb-{token}"),
        installer_user_id=installer,
    )


def _install_app(
    control_store: ControlStore,
    pg_dsn: str,
    installer: FakeInstaller,
    *,
    sign_in: FakeOIDCProvider | None = None,
    directory: FakeSlackDirectory | None = None,
) -> FastAPI:
    return _app(
        control_store,
        pg_dsn,
        sign_in if sign_in is not None else FakeOIDCProvider(identity=VERIFIED_JANE),
        installer=installer,
        directory=directory,
    )


async def _row(control_store: ControlStore, team: str) -> Any:
    return await control_store._connected_pool.fetchrow(
        "SELECT * FROM workspaces WHERE slack_team_id = $1", team
    )


async def _admins(control_store: ControlStore, team: str) -> list[str]:
    records = await control_store._connected_pool.fetch(
        """
        SELECT slack_user_id FROM workspace_admins
        JOIN workspaces ON workspaces.id = workspace_admins.workspace_id
        WHERE workspaces.slack_team_id = $1 ORDER BY slack_user_id
        """,
        team,
    )
    return [record["slack_user_id"] for record in records]


async def _start_install(client: httpx.AsyncClient, installer: FakeInstaller) -> str:
    """Runs the /install leg and returns the state Slack would echo back."""

    response = await client.get("/api/slack/install")
    assert response.status_code == 302
    redirect_uri, state = installer.authorize_calls[-1]
    assert redirect_uri == CALLBACK_URI
    return state


async def _finish(client: httpx.AsyncClient, installer: FakeInstaller) -> httpx.Response:
    state = await _start_install(client, installer)
    return await client.get("/api/slack/install/callback", params={"code": "c", "state": state})


def _sign_in_hop(team: str, next_path: str) -> str:
    query = urlencode({"team": team, "next": next_path})
    return f"{APP_URL}/api/auth/slack/login?{query}"


async def test_install_redirects_to_slack_without_a_session(
    control_store: ControlStore, pg_dsn: str
) -> None:
    installer = FakeInstaller(_installation())
    async with _client(_install_app(control_store, pg_dsn, installer)) as client:
        anonymous = await client.get("/api/slack/install")
        await _sign_in(client)
        signed_in = await client.get("/api/slack/install")

    for response in (anonymous, signed_in):
        assert response.status_code == 302
        assert response.headers["location"].startswith("https://slack.example/oauth")
    states = [state for _, state in installer.authorize_calls]
    assert states == [
        parse_qs(urlsplit(response.headers["location"]).query)["state"][0]
        for response in (anonymous, signed_in)
    ]
    # The state is unguessable and never leaves our session cookie otherwise.
    assert all(len(state) >= 32 for state in states)


async def test_install_is_not_found_when_slack_is_unconfigured(
    control_store: ControlStore, pg_dsn: str
) -> None:
    async with _client(_app(control_store, pg_dsn)) as client:
        install = await client.get("/api/slack/install")
        callback = await client.get("/api/slack/install/callback", params={"code": "c"})

    assert install.status_code == 404
    assert callback.headers["location"] == f"{APP_URL}/?slack_error=unavailable"


async def test_first_install_creates_the_workspace_with_the_installer_as_admin(
    control_store: ControlStore, pg_dsn: str
) -> None:
    installer = FakeInstaller(_installation())
    async with _client(_install_app(control_store, pg_dsn, installer)) as client:
        await _sign_in(client)
        callback = await _finish(client, installer)
        workspaces = await client.get("/api/workspaces")
        me = await client.get("/api/me")
        row = await _row(control_store, "T1")
        admins = await _admins(control_store, "T1")

    assert callback.status_code == 302
    assert callback.headers["location"] == f"{APP_URL}/?installed=1"
    assert installer.exchange_calls == [("c", CALLBACK_URI)]
    assert row is not None
    assert row["name"] == "Acme Slack"
    assert row["status"] == "active"
    assert TokenCipher(CRYPTO_KEY).decrypt(row["bot_token_encrypted"]) == "xoxb-granted"
    assert "xoxb-granted" not in row["bot_token_encrypted"]
    assert admins == ["U1"]
    assert [item["id"] for item in workspaces.json()["workspaces"]] == [str(row["id"])]
    assert me.json()["team"]["workspace_id"] == str(row["id"])
    assert me.json()["team"]["is_admin"] is True


async def test_install_from_another_browser_account_signs_in_as_the_installer(
    control_store: ControlStore, pg_dsn: str
) -> None:
    installer = FakeInstaller(_installation(installer="U7"))
    async with _client(_install_app(control_store, pg_dsn, installer)) as client:
        anonymous = await _finish(client, installer)
        await _sign_in(client)  # U1, while U7 approved the next install in Slack
        installer.installation = _installation(installer="U7", token="again")
        other_account = await _finish(client, installer)
        admins = await _admins(control_store, "T1")

    hop = _sign_in_hop("T1", "/?installed=1")
    assert anonymous.headers["location"] == hop
    assert other_account.headers["location"] == hop
    assert admins == ["U7"]


@pytest.mark.parametrize(("listed", "slack_admin"), [(True, False), (False, True)])
async def test_an_admin_reinstall_refreshes_the_token_and_revives_a_paused_workspace(
    control_store: ControlStore, pg_dsn: str, listed: bool, slack_admin: bool
) -> None:
    installer = FakeInstaller(_installation(name="Renamed", token="fresh"))
    directory = FakeSlackDirectory()
    directory.set("U1", admin=slack_admin)
    app = _install_app(control_store, pg_dsn, installer, directory=directory)
    async with _client(app) as client:
        workspace_id = await _insert_workspace(control_store, status="paused")
        await _add_admin(control_store, workspace_id, "U0")
        if listed:
            await _add_admin(control_store, workspace_id, "U1")
        await _sign_in(client)
        callback = await _finish(client, installer)
        row = await _row(control_store, "T1")
        admins = await _admins(control_store, "T1")

    assert callback.headers["location"] == f"{APP_URL}/?installed=1"
    assert row["id"] == workspace_id
    assert row["name"] == "Renamed"
    assert row["status"] == "active"
    assert TokenCipher(CRYPTO_KEY).decrypt(row["bot_token_encrypted"]) == "xoxb-fresh"
    # A reinstall never changes who the admins are.
    assert admins == (["U0", "U1"] if listed else ["U0"])


async def test_a_slack_admin_who_reinstalls_gets_access_at_once(
    control_store: ControlStore, pg_dsn: str
) -> None:
    installer = FakeInstaller(_installation(installer="U5"))
    directory = FakeSlackDirectory()
    slack = FakeOIDCProvider(identity=slack_account("U5"))
    app = _install_app(control_store, pg_dsn, installer, sign_in=slack, directory=directory)
    async with _client(app) as client:
        workspace_id = await _insert_workspace(control_store)
        await _add_admin(control_store, workspace_id, "U1")
        await _sign_in(client)
        # The saved check says "not an admin"; then a Slack owner promotes U5.
        assert (await client.get("/api/me")).json()["team"]["is_admin"] is False
        directory.set("U5", admin=True)
        callback = await _finish(client, installer)
        # The install's fresh answer from Slack replaces the older "not an
        # admin", so an outage right after it still lets U5 in.
        directory.error = RuntimeError("slack is down")
        me = await client.get("/api/me")

    assert callback.headers["location"] == f"{APP_URL}/?installed=1"
    assert me.json()["team"]["is_admin"] is True


async def test_a_listed_admin_slack_reports_inactive_cannot_reinstall(
    control_store: ControlStore, pg_dsn: str
) -> None:
    installer = FakeInstaller(_installation(token="theirs"))
    directory = FakeSlackDirectory()
    directory.set("U1", active=False)
    app = _install_app(control_store, pg_dsn, installer, directory=directory)
    async with _client(app) as client:
        workspace_id = await _insert_workspace(control_store, status="paused")
        await _add_admin(control_store, workspace_id, "U1")
        callback = await _finish(client, installer)
        row = await _row(control_store, "T1")

    assert callback.headers["location"] == _sign_in_hop("T1", "/?slack_error=not_admin")
    assert row["status"] == "paused"
    assert TokenCipher(CRYPTO_KEY).decrypt(row["bot_token_encrypted"]) == "xoxb-T1"


async def test_a_reinstall_by_someone_who_is_not_an_admin_changes_nothing(
    control_store: ControlStore, pg_dsn: str
) -> None:
    installer = FakeInstaller(_installation(installer="U2", name="Hijacked", token="theirs"))
    slack = FakeOIDCProvider(identity=slack_account("U2"))
    app = _install_app(control_store, pg_dsn, installer, sign_in=slack)
    async with _client(app) as client:
        workspace_id = await _insert_workspace(control_store, status="paused")
        await _add_admin(control_store, workspace_id, "U1")
        await _sign_in(client)
        signed_in = await _finish(client, installer)
        client.cookies.clear()
        anonymous = await _finish(client, installer)
        row = await _row(control_store, "T1")
        admins = await _admins(control_store, "T1")

    assert signed_in.headers["location"] == f"{APP_URL}/?slack_error=not_admin"
    assert anonymous.headers["location"] == _sign_in_hop("T1", "/?slack_error=not_admin")
    assert row["name"] == "Acme"
    # The operator's pause survives, and the stored token did not move.
    assert row["status"] == "paused"
    assert TokenCipher(CRYPTO_KEY).decrypt(row["bot_token_encrypted"]) == "xoxb-T1"
    assert admins == ["U1"]


async def test_a_slack_lookup_failure_asks_a_possible_slack_admin_to_retry(
    control_store: ControlStore, pg_dsn: str
) -> None:
    installer = FakeInstaller(_installation(installer="U9"))
    directory = FakeSlackDirectory()
    directory.error = RuntimeError("slack is down")
    app = _install_app(control_store, pg_dsn, installer, directory=directory)
    async with _client(app) as client:
        first_install = await _finish(client, installer)
        installer.installation = _installation(installer="U1")
        unknown_slack_admin = await _finish(client, installer)
        admins = await _admins(control_store, "T1")

    assert first_install.headers["location"] == _sign_in_hop("T1", "/?installed=1")
    assert admins == ["U9"]
    # Slack didn't say whether U1 is a Slack admin, so "no access" would be a guess.
    assert unknown_slack_admin.headers["location"] == f"{APP_URL}/?slack_error=install_failed"


async def test_an_install_into_a_second_workspace_is_refused_and_changes_nothing(
    control_store: ControlStore, pg_dsn: str
) -> None:
    installer = FakeInstaller(_installation(team="T2", name="Other", installer="U9"))
    directory = FakeSlackDirectory()
    directory.set("U9", admin=True)
    app = _install_app(control_store, pg_dsn, installer, directory=directory)
    async with _client(app) as client:
        workspace_id = await _insert_workspace(control_store)
        await _add_admin(control_store, workspace_id, "U1")
        await _sign_in(client)
        signed_in = await _finish(client, installer)
        client.cookies.clear()
        anonymous = await _finish(client, installer)
        other = await _row(control_store, "T2")
        row = await _row(control_store, "T1")

    assert signed_in.status_code == 302
    assert signed_in.headers["location"] == f"{APP_URL}/?slack_error=other_workspace"
    assert anonymous.headers["location"] == f"{APP_URL}/?slack_error=other_workspace"
    assert other is None
    assert row["status"] == "active"
    assert TokenCipher(CRYPTO_KEY).decrypt(row["bot_token_encrypted"]) == "xoxb-T1"


async def test_concurrent_first_installs_of_two_workspaces_make_one_workspace(
    control_store: ControlStore,
) -> None:
    other = ControlStore(control_store.dsn)
    await other.connect(run_migrations=False, pool_min_size=1, pool_max_size=1)
    try:
        results = await asyncio.gather(
            *(
                store.install_workspace(
                    slack_team_id=team,
                    name=team,
                    bot_token_encrypted=bot_token(team),
                    installer_id="U1",
                    installer_status=None,
                )
                for store, team in ((control_store, "T1"), (other, "T2"))
            )
        )
    finally:
        await other.aclose()

    assert sorted(type(result).__name__ for result in results) == [
        "OtherWorkspaceInstalled",
        "WorkspaceInstalled",
    ]
    assert await control_store._connected_pool.fetchval("SELECT count(*) FROM workspaces") == 1


async def test_callback_rejects_a_state_that_does_not_match(
    control_store: ControlStore, pg_dsn: str
) -> None:
    installer = FakeInstaller(_installation())
    async with _client(_install_app(control_store, pg_dsn, installer)) as client:
        await _start_install(client, installer)

        forged = await client.get(
            "/api/slack/install/callback", params={"code": "c", "state": "not-the-state"}
        )
        stateless = await client.get("/api/slack/install/callback", params={"code": "c"})
        # Non-ASCII is a mismatch, not a crash: comparing it as str would raise.
        non_ascii = await client.get(
            "/api/slack/install/callback", params={"code": "c", "state": "café"}
        )
        row = await _row(control_store, "T1")

    for response in (forged, stateless, non_ascii):
        assert response.status_code == 302
        assert response.headers["location"] == f"{APP_URL}/?slack_error=expired"
    assert installer.exchange_calls == []
    assert row is None


async def test_a_forged_error_callback_does_not_discard_a_pending_install(
    control_store: ControlStore, pg_dsn: str
) -> None:
    installer = FakeInstaller(_installation())
    async with _client(_install_app(control_store, pg_dsn, installer)) as client:
        await _sign_in(client)
        state = await _start_install(client, installer)

        forged = await client.get(
            "/api/slack/install/callback",
            params={"error": "access_denied", "state": "not-the-state"},
        )
        # The real callback still works: the forged one consumed nothing.
        genuine = await client.get(
            "/api/slack/install/callback", params={"code": "c", "state": state}
        )
        row = await _row(control_store, "T1")

    assert forged.headers["location"] == f"{APP_URL}/?slack_error=expired"
    assert genuine.headers["location"] == f"{APP_URL}/?installed=1"
    assert row is not None


async def test_callback_reports_denied_consent_and_failed_exchanges(
    control_store: ControlStore, pg_dsn: str
) -> None:
    installer = FakeInstaller(error=SlackInstallError("nope"))
    async with _client(_install_app(control_store, pg_dsn, installer)) as client:
        state = await _start_install(client, installer)
        denied = await client.get(
            "/api/slack/install/callback",
            params={"error": "access_denied", "state": state},
        )
        state = await _start_install(client, installer)
        no_code = await client.get("/api/slack/install/callback", params={"state": state})
        state = await _start_install(client, installer)
        failed = await client.get(
            "/api/slack/install/callback", params={"code": "c", "state": state}
        )
        row = await _row(control_store, "T1")

    assert denied.headers["location"] == f"{APP_URL}/?slack_error=denied"
    assert no_code.headers["location"] == f"{APP_URL}/?slack_error=exchange_failed"
    assert failed.headers["location"] == f"{APP_URL}/?slack_error=exchange_failed"
    assert row is None


async def test_a_failed_write_reports_install_failed(
    control_store: ControlStore, pg_dsn: str
) -> None:
    class FailingStore(ControlStore):
        async def install_workspace(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("database down")

    failing = FailingStore(control_store.dsn)
    await failing.connect(run_migrations=False, pool_min_size=1, pool_max_size=1)
    installer = FakeInstaller(_installation())
    async with _client(_install_app(failing, pg_dsn, installer)) as client:
        callback = await _finish(client, installer)

    assert callback.headers["location"] == f"{APP_URL}/?slack_error=install_failed"


async def test_concurrent_first_installs_make_one_workspace(control_store: ControlStore) -> None:
    other = ControlStore(control_store.dsn)
    await other.connect(run_migrations=False, pool_min_size=1, pool_max_size=1)
    try:
        results = await asyncio.gather(
            *(
                store.install_workspace(
                    slack_team_id="T1",
                    name="Acme",
                    bot_token_encrypted=bot_token("T1"),
                    installer_id=installer,
                    installer_status=None,
                )
                for store, installer in ((control_store, "U1"), (other, "U2"))
            )
        )
    finally:
        await other.aclose()

    created = [result for result in results if getattr(result, "created", False)]
    assert len(created) == 1
    rows = await control_store._connected_pool.fetchval(
        "SELECT count(*) FROM workspaces WHERE slack_team_id = 'T1'"
    )
    assert rows == 1
    assert len(await _admins(control_store, "T1")) == 1


@pytest.mark.parametrize("slack", [None, {"client_id": "cid", "client_secret": "csecret"}])
def test_the_api_refuses_to_start_without_an_encryption_key(
    pg_dsn: str, slack: dict[str, str] | None
) -> None:
    config = Config.model_validate(
        {
            "database": {"url": pg_dsn},
            "api": {"session_secret": "test-session-secret-0123456789abcdef"},
            "auth": {"slack": slack},
            "workspace": {"slack": {"app_token": "xapp-test"}},
        }
    )
    with pytest.raises(ValueError, match="crypto.token_key"):
        create_app(config)


def test_authorize_url_requests_the_manifest_scopes() -> None:
    installer = SlackOAuthInstaller("client-id", "client-secret", _cipher())
    url = installer.authorize_url(redirect_uri=CALLBACK_URI, state="the-state")

    query = parse_qs(urlsplit(url).query)
    assert urlsplit(url).netloc == "slack.com"
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == [CALLBACK_URI]
    assert query["state"] == ["the-state"]
    assert query["scope"] == [",".join(BOT_SCOPES)]
    assert "client-secret" not in url


async def test_exchange_encrypts_the_token_and_reads_the_team(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    class StubResponse:
        data = {
            "access_token": "xoxb-from-slack",
            "team": {"id": "T-STUB", "name": "Stub Slack"},
            "authed_user": {"id": "U-INSTALLER"},
        }

    class StubClient:
        async def oauth_v2_access(self, **kwargs: Any) -> StubResponse:
            captured.update(kwargs)
            return StubResponse()

    monkeypatch.setattr(slack_oauth, "AsyncWebClient", StubClient)
    cipher = _cipher()
    installer = SlackOAuthInstaller("client-id", "client-secret", cipher)

    installation = await installer.exchange(code="the-code", redirect_uri=CALLBACK_URI)

    assert installation.team_id == "T-STUB"
    assert installation.team_name == "Stub Slack"
    assert installation.installer_user_id == "U-INSTALLER"
    assert cipher.decrypt(installation.bot_token_encrypted) == "xoxb-from-slack"
    assert captured == {
        "client_id": "client-id",
        "client_secret": "client-secret",
        "code": "the-code",
        "redirect_uri": CALLBACK_URI,
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"team": {"id": "T", "name": "N"}, "authed_user": {"id": "U"}},
        {"access_token": "xoxb-x", "authed_user": {"id": "U"}},
        {"access_token": "xoxb-x", "team": {"name": "N"}, "authed_user": {"id": "U"}},
        {"access_token": "xoxb-x", "team": {"id": "T"}, "authed_user": {"id": "U"}},
        {"access_token": "", "team": {"id": "T", "name": "N"}, "authed_user": {"id": "U"}},
        {"access_token": "xoxb-x", "team": {"id": "T", "name": "N"}},
        {"access_token": "xoxb-x", "team": {"id": "T", "name": "N"}, "authed_user": {}},
    ],
)
async def test_exchange_rejects_an_unusable_response(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]
) -> None:
    class StubResponse:
        def __init__(self, data: dict[str, Any]) -> None:
            self.data = data

    class StubClient:
        async def oauth_v2_access(self, **kwargs: Any) -> StubResponse:
            return StubResponse(payload)

    monkeypatch.setattr(slack_oauth, "AsyncWebClient", StubClient)
    installer = SlackOAuthInstaller("client-id", "client-secret", _cipher())

    with pytest.raises(SlackInstallError):
        await installer.exchange(code="c", redirect_uri=CALLBACK_URI)
