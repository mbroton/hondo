from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from hondo.config import AuthConfig
from hondo.crypto import TokenCipher

_AUTHORIZE_URL = "https://slack.com/oauth/v2/authorize"

# The scopes the runtime needs, in step with slack-manifest.json. Changing this
# list means every already-installed workspace is on the old grant until it
# reinstalls, so add scopes here and to the manifest together.
BOT_SCOPES = (
    "app_mentions:read",
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "im:history",
    "chat:write",
    "files:read",
    "reactions:write",
    "users:read",
)


class SlackInstallError(Exception):
    """Slack refused the code exchange or answered with something unusable."""


@dataclass(frozen=True, slots=True)
class SlackInstallation:
    team_id: str
    team_name: str
    bot_token_encrypted: str
    installer_user_id: str


class SlackInstaller(Protocol):
    """The test seam: routes talk to this, tests swap in a fake."""

    def authorize_url(self, *, redirect_uri: str, state: str) -> str: ...

    async def exchange(self, *, code: str, redirect_uri: str) -> SlackInstallation: ...


class SlackOAuthInstaller:
    """Turns an OAuth code into an encrypted bot token.

    Encryption happens here so the plaintext token never leaves this object —
    routes handle ciphertext only.
    """

    def __init__(self, client_id: str, client_secret: str, cipher: TokenCipher) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._cipher = cipher

    def authorize_url(self, *, redirect_uri: str, state: str) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "scope": ",".join(BOT_SCOPES),
                "redirect_uri": redirect_uri,
                "state": state,
            }
        )
        return f"{_AUTHORIZE_URL}?{query}"

    async def exchange(self, *, code: str, redirect_uri: str) -> SlackInstallation:
        try:
            # oauth.v2.access authenticates with the client credentials, so it
            # is the one Web API call made without a token.
            response = await AsyncWebClient().oauth_v2_access(
                client_id=self._client_id,
                client_secret=self._client_secret,
                code=code,
                redirect_uri=redirect_uri,
            )
        except SlackApiError as exc:
            raise SlackInstallError("Slack rejected the code exchange") from exc
        # Timeouts, DNS and TLS failures come out of the transport, not as
        # SlackApiError. The caller is mid-redirect in a browser, so every way
        # this can fail has to arrive as one exception it can report on.
        except Exception as exc:
            raise SlackInstallError("the Slack token exchange failed") from exc
        return self._installation(response.data)

    def _installation(self, payload: Any) -> SlackInstallation:
        if not isinstance(payload, Mapping):
            raise SlackInstallError("oauth.v2.access returned a non-JSON body")
        token = payload.get("access_token")
        team = payload.get("team")
        authed_user = payload.get("authed_user")
        if not isinstance(token, str) or not token:
            raise SlackInstallError("oauth.v2.access returned no bot token")
        if not isinstance(team, Mapping):
            raise SlackInstallError("oauth.v2.access returned no team")
        team_id = team.get("id")
        team_name = team.get("name")
        if not isinstance(team_id, str) or not team_id:
            raise SlackInstallError("oauth.v2.access returned no team ID")
        if not isinstance(team_name, str) or not team_name:
            raise SlackInstallError("oauth.v2.access returned no team name")
        installer_id = authed_user.get("id") if isinstance(authed_user, Mapping) else None
        if not isinstance(installer_id, str) or not installer_id:
            raise SlackInstallError("oauth.v2.access returned no installing user")
        return SlackInstallation(
            team_id=team_id,
            team_name=team_name,
            bot_token_encrypted=self._cipher.encrypt(token),
            installer_user_id=installer_id,
        )


def build_installer(auth: AuthConfig, cipher: TokenCipher) -> SlackInstaller | None:
    """The installer for the configured Slack app, or None when it isn't configured."""

    if auth.slack is None:
        return None
    return SlackOAuthInstaller(
        auth.slack.client_id, auth.slack.client_secret.get_secret_value(), cipher
    )
