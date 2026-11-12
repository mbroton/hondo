from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from starlette.requests import Request
from starlette.responses import Response

from hondo.config import AuthConfig

logger = structlog.get_logger("hondo.api.oidc")

_DISCOVERY_URL = "https://slack.com/.well-known/openid-configuration"
_SCOPES = "openid email profile"


@dataclass(frozen=True, slots=True)
class OIDCIdentity:
    email: str | None
    email_verified: bool
    name: str | None
    picture: str | None
    team_id: str | None
    team_name: str | None
    slack_user_id: str | None


class OIDCProvider(Protocol):
    """The test seam: routes talk to this, tests swap in a fake."""

    async def authorize_redirect(
        self, request: Request, redirect_uri: str, *, team: str | None = None
    ) -> Response: ...

    async def fetch_identity(self, request: Request) -> OIDCIdentity: ...


def _text(claims: Any, name: str) -> str | None:
    value = claims.get(name)
    return value if isinstance(value, str) and value else None


class AuthlibOIDCProvider:
    def __init__(self, client: Any) -> None:
        # Any because authlib ships untyped; StarletteOAuth2App is the real type.
        self._client = client

    async def authorize_redirect(
        self, request: Request, redirect_uri: str, *, team: str | None = None
    ) -> Response:
        extra = {"team": team} if team else {}
        return await self._client.authorize_redirect(request, redirect_uri, **extra)

    async def fetch_identity(self, request: Request) -> OIDCIdentity:
        token = await self._client.authorize_access_token(request)
        # With the openid scope authlib validates the id_token and exposes its
        # claims as "userinfo"; hit the userinfo endpoint only when absent.
        claims = token.get("userinfo")
        from_userinfo = not claims
        if from_userinfo:
            claims = await self._client.userinfo(token=token)
        team_name = _text(claims, "https://slack.com/team_name")
        if team_name is None and not from_userinfo:
            # Slack's id_token carries the team ID but not its name.
            team_name = await self._team_name(token)
        return OIDCIdentity(
            email=claims.get("email"),
            # Strict is-true check: a missing or stringly claim counts as unverified.
            email_verified=claims.get("email_verified") is True,
            name=claims.get("name"),
            picture=claims.get("picture"),
            team_id=_text(claims, "https://slack.com/team_id"),
            team_name=team_name,
            slack_user_id=_text(claims, "https://slack.com/user_id"),
        )

    async def _team_name(self, token: Any) -> str | None:
        try:
            info = await self._client.userinfo(token=token)
        except Exception:
            # The name is only shown on screen; sign-in must not fail for it.
            logger.warning("slack userinfo failed", exc_info=True)
            return None
        return _text(info, "https://slack.com/team_name")


def build_sign_in(auth: AuthConfig) -> OIDCProvider | None:
    """Sign in with Slack for the configured Slack app, or None when it isn't configured."""

    if auth.slack is None:
        return None

    from authlib.integrations.starlette_client import OAuth

    client = OAuth().register(  # pyright: ignore[reportUnknownMemberType]
        name="slack",
        client_id=auth.slack.client_id,
        client_secret=auth.slack.client_secret.get_secret_value(),
        server_metadata_url=_DISCOVERY_URL,
        client_kwargs={"scope": _SCOPES},
    )
    return AuthlibOIDCProvider(client)
