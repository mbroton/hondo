from __future__ import annotations

import base64
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import timedelta
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from authlib.integrations.base_client import OAuthError
from authlib.integrations.starlette_client import OAuth
from fastapi import FastAPI
from httpx import ASGITransport
from joserfc import jwt as jose_jwt
from joserfc.jwk import OctKey
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response

from hondo.api.access import SlackDirectoryError, SlackMember, SlackMemberPage
from hondo.api.app import create_app
from hondo.api.oidc import AuthlibOIDCProvider, OIDCIdentity, OIDCProvider
from hondo.api.sessions import SESSION_COOKIE, hash_token, new_token
from hondo.api.slack_oauth import SlackInstaller
from hondo.api.store import ControlStore
from hondo.chat import ChannelInfo
from hondo.config import Config
from hondo.crypto import generate_key

APP_URL = "http://localhost:5173"
SESSION_SECRET = "test-session-secret-0123456789abcdef"
# The app decrypts bot tokens to ask Slack about a person, so tests run with a key.
CRYPTO_KEY = generate_key()

VERIFIED_JANE = OIDCIdentity(
    email="Jane@Example.com",
    email_verified=True,
    name="Jane Doe",
    picture="https://avatars.example/jane.png",
    team_id="T1",
    team_name="Acme",
    slack_user_id="U1",
)


def slack_account(
    slack_user_id: str, *, team: str = "T1", email: str | None = None, name: str = "Bob"
) -> OIDCIdentity:
    return replace(
        VERIFIED_JANE,
        email=email or f"{slack_user_id.lower()}@example.com",
        name=name,
        picture=None,
        team_id=team,
        slack_user_id=slack_user_id,
    )


class FakeOIDCProvider:
    def __init__(
        self,
        identity: OIDCIdentity | None = None,
        error: Exception | None = None,
    ) -> None:
        self.identity = identity
        self.error = error
        self.redirect_uris: list[str] = []
        self.teams: list[str | None] = []

    async def authorize_redirect(
        self, request: Request, redirect_uri: str, *, team: str | None = None
    ) -> Response:
        self.redirect_uris.append(redirect_uri)
        self.teams.append(team)
        return RedirectResponse(
            f"https://idp.example/authorize?redirect_uri={redirect_uri}", status_code=302
        )

    async def fetch_identity(self, request: Request) -> OIDCIdentity:
        if self.error is not None:
            raise self.error
        assert self.identity is not None
        return self.identity


class FakeSlackDirectory:
    """Slack's user directory. Unknown IDs are active members who are not Slack admins."""

    def __init__(self) -> None:
        self.people: dict[str, SlackMember | None] = {}
        # The member list that users.list pages through, page_size at a time.
        self.listing: list[SlackMember] = []
        self.page_size = 2
        self.error: Exception | None = None
        self.calls: list[tuple[str, str]] = []
        self.list_calls: list[str | None] = []
        self.channels: dict[str, ChannelInfo] = {}
        self.channel_calls: list[tuple[str, str]] = []

    def set(
        self,
        slack_user_id: str,
        *,
        name: str = "",
        admin: bool = False,
        active: bool = True,
        avatar_url: str | None = None,
    ) -> None:
        self.people[slack_user_id] = SlackMember(
            id=slack_user_id,
            name=name or slack_user_id,
            admin=admin,
            active=active,
            avatar_url=avatar_url,
        )

    def remove(self, slack_user_id: str) -> None:
        self.people[slack_user_id] = None

    def _raise_error(self) -> None:
        if isinstance(self.error, SlackDirectoryError):
            raise self.error
        if self.error is not None:
            raise SlackDirectoryError(str(self.error)) from self.error

    async def member(self, token: str, team_id: str, slack_user_id: str) -> SlackMember | None:
        self.calls.append((token, slack_user_id))
        self._raise_error()
        if slack_user_id in self.people:
            return self.people[slack_user_id]
        return SlackMember(id=slack_user_id, name=slack_user_id, admin=False, active=True)

    async def members(self, token: str, team_id: str, cursor: str | None) -> SlackMemberPage:
        self.list_calls.append(cursor)
        self._raise_error()
        start = int(cursor) if cursor is not None else 0
        end = start + self.page_size
        return SlackMemberPage(
            members=self.listing[start:end],
            next_cursor=str(end) if end < len(self.listing) else None,
        )

    async def channel(self, token: str, channel: str) -> ChannelInfo | None:
        self.channel_calls.append((token, channel))
        return self.channels.get(channel)


def _config(pg_dsn: str, **api_overrides: Any) -> Config:
    return Config.model_validate(
        {
            "database": {"url": pg_dsn},
            # Every field the assertions depend on is explicit: ambient
            # HONDO_* env or a developer's .env must not steer these tests.
            "api": {
                "session_secret": SESSION_SECRET,
                "public_base_url": APP_URL,
                "app_url": APP_URL,
                **api_overrides,
            },
            "auth": {"session_ttl_days": 30},
            "crypto": {"token_key": CRYPTO_KEY},
            "workspace": {"slack": {"app_token": "xapp-test"}},
        }
    )


def _app(
    control_store: ControlStore,
    pg_dsn: str,
    sign_in: OIDCProvider | None = None,
    *,
    installer: SlackInstaller | None = None,
    directory: FakeSlackDirectory | None = None,
    **api_overrides: Any,
) -> FastAPI:
    # The injected store is already connected, so lifespan startup skips
    # reconnecting (and re-migrating) it.
    return create_app(
        _config(pg_dsn, **api_overrides),
        store=control_store,
        sign_in=sign_in,
        installer=installer,
        directory=directory if directory is not None else FakeSlackDirectory(),
    )


@asynccontextmanager
async def _client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url=APP_URL) as client:
            yield client


async def test_login_redirects_to_slack(control_store: ControlStore, pg_dsn: str) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        plain = await client.get("/api/auth/slack/login")
        hinted = await client.get("/api/auth/slack/login", params={"team": "T1"})
        bad_hint = await client.get("/api/auth/slack/login", params={"team": "t1&x=y"})

    for response in (plain, hinted, bad_hint):
        assert response.status_code == 302
        assert response.headers["location"].startswith("https://idp.example/authorize")
    assert slack.redirect_uris == [f"{APP_URL}/api/auth/slack/callback"] * 3
    assert slack.teams == [None, "T1", None]


async def test_login_without_slack_sign_in_and_other_providers_are_404(
    control_store: ControlStore, pg_dsn: str
) -> None:
    async with _client(_app(control_store, pg_dsn)) as client:
        unconfigured = await client.get("/api/auth/slack/login")
        callback = await client.get("/api/auth/slack/callback?code=x&state=y")
    async with _client(_app(control_store, pg_dsn, FakeOIDCProvider())) as client:
        google = await client.get("/api/auth/google/login")
        google_callback = await client.get("/api/auth/google/callback")

    assert unconfigured.status_code == 404
    assert callback.status_code == 302
    assert callback.headers["location"] == f"{APP_URL}/login?error=exchange_failed"
    assert google.status_code == 404
    assert google_callback.status_code == 404


async def test_callback_signs_in_and_me_returns_user_and_team(
    control_store: ControlStore, pg_dsn: str
) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        callback = await client.get("/api/auth/slack/callback?code=x&state=y")
        assert callback.status_code == 302
        assert callback.headers["location"] == f"{APP_URL}/"
        cookie = callback.cookies[SESSION_COOKIE]
        assert cookie
        set_cookie = callback.headers["set-cookie"]
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
        # http public_base_url: the cookie must work without TLS in dev.
        assert "Secure" not in set_cookie

        me = await client.get("/api/me")

    assert me.status_code == 200
    body = me.json()
    assert set(body) == {"user", "team"}
    assert set(body["user"]) == {"id", "email", "display_name", "avatar_url"}
    assert body["user"]["email"] == "jane@example.com"  # lowercased
    assert body["user"]["display_name"] == "Jane Doe"
    assert body["user"]["avatar_url"] == "https://avatars.example/jane.png"
    assert body["team"] == {
        "id": "T1",
        "name": "Acme",
        "workspace_id": None,
        "is_admin": False,
        "admins": [],
        "slack": "ok",
    }


async def test_callback_lands_on_a_safe_next_path_only(
    control_store: ControlStore, pg_dsn: str
) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        landings = []
        for target in (
            "/settings?installed=1",
            "https://evil.example/",
            "//evil.example/",
            "/\\evil.example/",
            "settings",
            None,
        ):
            params = {"next": target} if target is not None else {}
            assert (await client.get("/api/auth/slack/login", params=params)).status_code == 302
            callback = await client.get("/api/auth/slack/callback?code=x&state=y")
            landings.append(callback.headers["location"])

    assert landings == [f"{APP_URL}/settings?installed=1"] + [f"{APP_URL}/"] * 5


async def test_a_forged_callback_does_not_discard_the_pending_destination(
    control_store: ControlStore, pg_dsn: str
) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        await client.get("/api/auth/slack/login", params={"next": "/settings?installed=1"})
        slack.error = OAuthError(error="mismatching_state")
        forged = await client.get("/api/auth/slack/callback?code=x&state=forged")
        slack.error = None
        genuine = await client.get("/api/auth/slack/callback?code=x&state=y")

    assert forged.headers["location"] == f"{APP_URL}/login?error=exchange_failed"
    assert genuine.headers["location"] == f"{APP_URL}/settings?installed=1"


async def test_sign_in_refreshes_the_profile_and_replaces_the_old_session(
    control_store: ControlStore, pg_dsn: str
) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        first = await client.get("/api/auth/slack/callback?code=x&state=y")
        old_token = first.cookies[SESSION_COOKIE]
        slack.identity = replace(
            VERIFIED_JANE, email="jane@new.example", name="Jane Q.", team_name="Acme Inc"
        )
        second = await client.get("/api/auth/slack/callback?code=x&state=y")
        me = await client.get("/api/me")
        client.cookies.clear()
        replay = await client.get("/api/me", headers={"cookie": f"{SESSION_COOKIE}={old_token}"})
        users = await control_store._connected_pool.fetchval("SELECT count(*) FROM users")
        sessions = await control_store._connected_pool.fetchval(
            "SELECT count(*) FROM auth_sessions"
        )

    assert second.status_code == 302
    assert me.json()["user"]["email"] == "jane@new.example"
    assert me.json()["user"]["display_name"] == "Jane Q."
    assert me.json()["team"]["name"] == "Acme Inc"
    assert replay.status_code == 401
    assert (users, sessions) == (1, 1)


async def test_same_email_in_two_slack_teams_is_two_users(
    control_store: ControlStore, pg_dsn: str
) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        await client.get("/api/auth/slack/callback?code=x&state=y")
        first = (await client.get("/api/me")).json()
        slack.identity = replace(VERIFIED_JANE, team_id="T2", team_name="Other", slack_user_id="U9")
        await client.get("/api/auth/slack/callback?code=x&state=y")
        second = (await client.get("/api/me")).json()

    assert first["user"]["email"] == second["user"]["email"] == "jane@example.com"
    assert first["user"]["id"] != second["user"]["id"]
    assert (first["team"]["id"], second["team"]["id"]) == ("T1", "T2")


async def test_sign_in_from_another_workspace_is_refused(
    control_store: ControlStore, pg_dsn: str
) -> None:
    await control_store._connected_pool.execute(
        """
        INSERT INTO workspaces (slack_team_id, name, status, bot_token_encrypted)
        VALUES ('T1', 'Acme', 'active', 'opaque')
        """
    )
    slack = FakeOIDCProvider(identity=slack_account("U9", team="T2"))
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        refused = await client.get("/api/auth/slack/callback?code=x&state=y")
        me = await client.get("/api/me")
        slack.identity = VERIFIED_JANE
        accepted = await client.get("/api/auth/slack/callback?code=x&state=y")
        users = await control_store._connected_pool.fetch("SELECT slack_team_id FROM users")

    assert refused.status_code == 302
    assert refused.headers["location"] == f"{APP_URL}/login?error=other_workspace"
    assert SESSION_COOKIE not in refused.cookies
    assert me.status_code == 401
    assert accepted.headers["location"] == f"{APP_URL}/"
    assert [user["slack_team_id"] for user in users] == ["T1"]


async def test_callback_cookie_is_secure_when_public_url_is_https(
    control_store: ControlStore, pg_dsn: str
) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    app = _app(control_store, pg_dsn, slack, public_base_url="https://hondo.example")
    async with _client(app) as client:
        callback = await client.get("/api/auth/slack/callback?code=x&state=y")

    assert callback.status_code == 302
    assert "Secure" in callback.headers["set-cookie"]


@pytest.mark.parametrize(
    ("identity", "error"),
    [
        (replace(VERIFIED_JANE, email_verified=False), "unverified_email"),
        (replace(VERIFIED_JANE, email=None), "unverified_email"),
        (replace(VERIFIED_JANE, team_id=None), "exchange_failed"),
        (replace(VERIFIED_JANE, slack_user_id=None), "exchange_failed"),
    ],
)
async def test_unusable_identity_redirects_to_login_error(
    control_store: ControlStore, pg_dsn: str, identity: OIDCIdentity, error: str
) -> None:
    async with _client(_app(control_store, pg_dsn, FakeOIDCProvider(identity=identity))) as client:
        response = await client.get("/api/auth/slack/callback?code=x&state=y")

    assert response.status_code == 302
    assert response.headers["location"] == f"{APP_URL}/login?error={error}"
    assert SESSION_COOKIE not in response.cookies


async def test_denied_consent_redirects_to_login_error(
    control_store: ControlStore, pg_dsn: str
) -> None:
    slack = FakeOIDCProvider(error=OAuthError(error="access_denied"))
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        response = await client.get("/api/auth/slack/callback?error=access_denied")

    assert response.status_code == 302
    assert response.headers["location"] == f"{APP_URL}/login?error=denied"


async def test_exchange_failure_redirects_to_login_error(
    control_store: ControlStore, pg_dsn: str
) -> None:
    slack = FakeOIDCProvider(error=RuntimeError("token endpoint exploded"))
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        response = await client.get("/api/auth/slack/callback?code=x&state=y")

    assert response.status_code == 302
    assert response.headers["location"] == f"{APP_URL}/login?error=exchange_failed"


async def test_logout_deletes_session_and_clears_cookie(
    control_store: ControlStore, pg_dsn: str
) -> None:
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(control_store, pg_dsn, slack)) as client:
        callback = await client.get("/api/auth/slack/callback?code=x&state=y")
        token = callback.cookies[SESSION_COOKIE]
        assert (await client.get("/api/me")).status_code == 200

        logout = await client.post("/api/auth/logout")
        assert logout.status_code == 204
        assert f"{SESSION_COOKIE}=" in logout.headers["set-cookie"]

        # The cookie jar is cleared...
        assert (await client.get("/api/me")).status_code == 401
        # ...and replaying the old token fails too: the session row is gone.
        replay = await client.get("/api/me", cookies={SESSION_COOKIE: token})
        assert replay.status_code == 401


async def test_expired_session_is_401(control_store: ControlStore, pg_dsn: str) -> None:
    user = await control_store.upsert_user(
        slack_team_id="T1",
        slack_user_id="U1",
        email="jane@example.com",
        display_name="Jane",
        avatar_url=None,
        team_name="Acme",
    )
    token = new_token()
    await control_store.create_auth_session(hash_token(token), user.id, timedelta(seconds=-1))

    async with _client(_app(control_store, pg_dsn)) as client:
        response = await client.get("/api/me", cookies={SESSION_COOKIE: token})

    assert response.status_code == 401


async def test_me_without_cookie_is_401(control_store: ControlStore, pg_dsn: str) -> None:
    async with _client(_app(control_store, pg_dsn)) as client:
        response = await client.get("/api/me")

    assert response.status_code == 401


async def test_create_app_requires_session_secret(pg_dsn: str) -> None:
    config = _config(pg_dsn)
    config.api.session_secret = None
    with pytest.raises(ValueError, match="session_secret"):
        create_app(config)


async def test_persistence_failure_redirects_to_login_error(
    control_store: ControlStore, pg_dsn: str
) -> None:
    class FailingStore(ControlStore):
        async def upsert_user(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("database down")

    failing = FailingStore(pg_dsn)
    await failing.connect(run_migrations=False, pool_min_size=1, pool_max_size=1)
    app = _app(failing, pg_dsn, FakeOIDCProvider(identity=VERIFIED_JANE))
    async with _client(app) as client:
        response = await client.get("/api/auth/slack/callback?code=x&state=y")

    assert response.status_code == 302
    assert response.headers["location"] == f"{APP_URL}/login?error=sign_in_failed"
    assert SESSION_COOKIE not in response.cookies


async def test_a_failed_sign_in_keeps_the_session_the_browser_already_has(
    control_store: ControlStore, pg_dsn: str
) -> None:
    class FlakyStore(ControlStore):
        fail = False

        async def create_auth_session(self, *args: Any, **kwargs: Any) -> Any:
            if self.fail:
                raise ConnectionError("database down")
            return await super().create_auth_session(*args, **kwargs)

    flaky = FlakyStore(pg_dsn)
    await flaky.connect(run_migrations=False, pool_min_size=1, pool_max_size=1)
    slack = FakeOIDCProvider(identity=VERIFIED_JANE)
    async with _client(_app(flaky, pg_dsn, slack)) as client:
        await client.get("/api/auth/slack/callback?code=x&state=y")
        flaky.fail = True
        slack.identity = slack_account("U2")
        failed = await client.get("/api/auth/slack/callback?code=x&state=y")
        me = await client.get("/api/me")

    assert failed.headers["location"] == f"{APP_URL}/login?error=sign_in_failed"
    assert me.status_code == 200
    assert me.json()["user"]["email"] == "jane@example.com"


# --- Authlib-backed flow: real state/nonce verification against a stub issuer ---

ISSUER = "https://idp.test"
OIDC_CLIENT_ID = "hondo-test-client"
_ID_TOKEN_KEY = OctKey.import_key(
    "authlib-test-id-token-signing-key-0123456789",
    parameters={"kid": "test-sig", "alg": "HS256"},
)


def _authlib_provider(nonce_holder: dict[str, str]) -> AuthlibOIDCProvider:
    """A real AuthlibOIDCProvider wired to an in-process stub issuer.

    Endpoints and signing keys are passed as static server metadata (no
    discovery fetch), and the token endpoint is served by an httpx
    MockTransport, so Authlib's own state and nonce verification runs with no
    network. The id_token echoes nonce_holder["nonce"], which the test sets to
    the nonce Authlib stashed in the OAuth state cookie.
    """

    def idp(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/userinfo":
            return httpx.Response(
                200,
                json={
                    "sub": "U1",
                    "https://slack.com/team_id": "T1",
                    "https://slack.com/team_name": "Acme",
                },
            )
        if request.url.path != "/token":
            return httpx.Response(404)
        now = int(time.time())
        claims = {
            "iss": ISSUER,
            "sub": "user-1",
            "aud": OIDC_CLIENT_ID,
            "iat": now,
            "exp": now + 300,
            "nonce": nonce_holder.get("nonce", "nonce-never-provided"),
            "email": "Jane@Example.com",
            "email_verified": True,
            "name": "Jane Doe",
            "picture": "https://avatars.example/jane.png",
            "https://slack.com/team_id": "T1",
            "https://slack.com/user_id": "U1",
        }
        id_token = jose_jwt.encode({"alg": "HS256", "kid": "test-sig"}, claims, _ID_TOKEN_KEY)
        return httpx.Response(
            200,
            json={
                "access_token": "at-1",
                "token_type": "Bearer",
                "expires_in": 3600,
                "id_token": id_token,
            },
        )

    oauth = OAuth()
    client = oauth.register(
        name="slack",
        client_id=OIDC_CLIENT_ID,
        client_secret="test-oidc-client-secret",
        authorize_url=f"{ISSUER}/authorize",
        access_token_url=f"{ISSUER}/token",
        userinfo_endpoint=f"{ISSUER}/userinfo",
        issuer=ISSUER,
        jwks={"keys": [_ID_TOKEN_KEY.as_dict()]},
        id_token_signing_alg_values_supported=["HS256"],
        client_kwargs={
            "scope": "openid email profile",
            "transport": httpx.MockTransport(idp),
        },
    )
    return AuthlibOIDCProvider(client)


def _oauth_state_from(login_response: httpx.Response) -> str:
    return parse_qs(urlsplit(login_response.headers["location"]).query)["state"][0]


def _nonce_from_state_cookie(client: httpx.AsyncClient) -> str:
    # The Starlette session cookie is signed base64 JSON; the signature and
    # timestamp trail the payload after "." separators.
    payload = client.cookies["session"].split(".")[0]
    data = json.loads(base64.b64decode(payload + "=" * (-len(payload) % 4)))
    (state_entry,) = (value for key, value in data.items() if key.startswith("_state_slack_"))
    return state_entry["data"]["nonce"]


async def test_authlib_login_sets_short_lived_state_cookie(
    control_store: ControlStore, pg_dsn: str
) -> None:
    provider = _authlib_provider({})
    async with _client(_app(control_store, pg_dsn, provider)) as client:
        response = await client.get("/api/auth/slack/login")

    assert response.status_code == 302
    location = urlsplit(response.headers["location"])
    assert response.headers["location"].startswith(f"{ISSUER}/authorize?")
    query = parse_qs(location.query)
    assert query["state"][0]
    assert query["nonce"][0]
    assert query["redirect_uri"] == [f"{APP_URL}/api/auth/slack/callback"]
    assert query["scope"] == ["openid email profile"]

    set_cookie = response.headers["set-cookie"].lower()
    assert set_cookie.startswith("session=")
    assert "max-age=600" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/" in set_cookie


async def test_authlib_callback_without_state_cookie_is_rejected(
    control_store: ControlStore, pg_dsn: str
) -> None:
    provider = _authlib_provider({})
    async with _client(_app(control_store, pg_dsn, provider)) as client:
        login = await client.get("/api/auth/slack/login")
        state = _oauth_state_from(login)
        # A fresh browser: the state parameter is valid but the state cookie
        # never arrives, so Authlib must reject the exchange.
        client.cookies.clear()
        response = await client.get(f"/api/auth/slack/callback?code=c-1&state={state}")

    assert response.status_code == 302
    assert response.headers["location"] == f"{APP_URL}/login?error=exchange_failed"
    assert SESSION_COOKIE not in response.cookies


async def test_authlib_callback_with_mismatched_state_is_rejected(
    control_store: ControlStore, pg_dsn: str
) -> None:
    provider = _authlib_provider({})
    async with _client(_app(control_store, pg_dsn, provider)) as client:
        await client.get("/api/auth/slack/login")
        response = await client.get("/api/auth/slack/callback?code=c-1&state=forged-state")

    assert response.status_code == 302
    assert response.headers["location"] == f"{APP_URL}/login?error=exchange_failed"
    assert SESSION_COOKIE not in response.cookies


async def test_authlib_callback_with_wrong_nonce_is_rejected(
    control_store: ControlStore, pg_dsn: str
) -> None:
    nonce_holder = {"nonce": "not-the-nonce-authlib-generated"}
    provider = _authlib_provider(nonce_holder)
    async with _client(_app(control_store, pg_dsn, provider)) as client:
        login = await client.get("/api/auth/slack/login")
        state = _oauth_state_from(login)
        response = await client.get(f"/api/auth/slack/callback?code=c-1&state={state}")

    assert response.status_code == 302
    assert response.headers["location"] == f"{APP_URL}/login?error=exchange_failed"
    assert SESSION_COOKIE not in response.cookies


async def test_authlib_full_flow_verifies_state_and_nonce(
    control_store: ControlStore, pg_dsn: str
) -> None:
    nonce_holder: dict[str, str] = {}
    provider = _authlib_provider(nonce_holder)
    async with _client(_app(control_store, pg_dsn, provider)) as client:
        login = await client.get("/api/auth/slack/login")
        state = _oauth_state_from(login)
        nonce_holder["nonce"] = _nonce_from_state_cookie(client)
        # The nonce sent to the issuer is the one stored in the state cookie.
        assert parse_qs(urlsplit(login.headers["location"]).query)["nonce"] == [
            nonce_holder["nonce"]
        ]

        callback = await client.get(f"/api/auth/slack/callback?code=c-1&state={state}")
        assert callback.status_code == 302
        assert callback.headers["location"] == f"{APP_URL}/"
        assert callback.cookies[SESSION_COOKIE]

        me = await client.get("/api/me")

    assert me.status_code == 200
    assert me.json()["user"]["email"] == "jane@example.com"
    # Slack's id_token has no team name, so sign-in asks userInfo for it.
    assert me.json()["team"]["name"] == "Acme"
