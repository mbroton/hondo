from __future__ import annotations

import re
from datetime import timedelta
from typing import Annotated

import structlog
from authlib.integrations.base_client import OAuthError
from fastapi import APIRouter, HTTPException, Query, Request, Response
from starlette.responses import RedirectResponse

from hondo.api.oidc import OIDCProvider
from hondo.api.sessions import (
    SESSION_COOKIE,
    clear_session_cookie,
    hash_token,
    new_token,
    set_session_cookie,
)
from hondo.api.store import ControlStore
from hondo.config import Config

logger = structlog.get_logger("hondo.api.auth")

router = APIRouter()

# Kept in the signed Starlette session cookie between /login and /callback.
_NEXT_KEY = "login_next"
_TEAM_PATTERN = re.compile(r"[A-Z0-9]{1,32}")


def _sign_in(request: Request) -> OIDCProvider | None:
    provider: OIDCProvider | None = request.app.state.sign_in
    return provider


def safe_next(value: str | None) -> str | None:
    """A same-site path to land on after sign-in, or None for anything else."""

    if value is None or not value.startswith("/") or value.startswith(("//", "/\\")):
        return None
    if any(ord(char) < 0x20 or char == "\\" for char in value):
        return None
    return value


def _login_error_redirect(config: Config, code: str) -> RedirectResponse:
    # 302, not 307: the browser arrives on a GET and must land on the SPA's
    # login page, never a bare error body mid-flow.
    return RedirectResponse(f"{config.api.app_url}/login?error={code}", status_code=302)


@router.get("/slack/login", responses={302: {"description": "Redirect to Slack sign-in"}})
async def login(
    request: Request,
    team: Annotated[str | None, Query(max_length=64, description="Slack team to suggest.")] = None,
    next_path: Annotated[
        str | None,
        Query(alias="next", max_length=2048, description="Dashboard path to open after sign-in."),
    ] = None,
) -> Response:
    config: Config = request.app.state.config
    provider = _sign_in(request)
    if provider is None:
        raise HTTPException(status_code=404, detail="not found")
    target = safe_next(next_path)
    if target is None:
        request.session.pop(_NEXT_KEY, None)
    else:
        request.session[_NEXT_KEY] = target
    redirect_uri = f"{config.api.public_base_url}/api/auth/slack/callback"
    hint = team if team is not None and _TEAM_PATTERN.fullmatch(team) else None
    return await provider.authorize_redirect(request, redirect_uri, team=hint)


@router.get("/slack/callback", responses={302: {"description": "Redirect to the dashboard"}})
async def callback(request: Request) -> Response:
    config: Config = request.app.state.config
    provider = _sign_in(request)
    if provider is None:
        # The user lands here from an external redirect — send them back to
        # the SPA instead of a bare 404.
        return _login_error_redirect(config, "exchange_failed")

    try:
        identity = await provider.fetch_identity(request)
    except OAuthError as exc:
        code = "denied" if exc.error == "access_denied" else "exchange_failed"
        logger.warning("oidc exchange failed", error=exc.error)
        return _login_error_redirect(config, code)
    except Exception:
        logger.warning("oidc exchange failed", exc_info=True)
        return _login_error_redirect(config, "exchange_failed")

    if not identity.email or not identity.email_verified:
        logger.warning("oidc identity rejected: unverified email")
        return _login_error_redirect(config, "unverified_email")
    if identity.team_id is None or identity.slack_user_id is None:
        logger.warning("oidc identity rejected: no Slack account")
        return _login_error_redirect(config, "exchange_failed")

    # Only now: a forged or stale callback fails the exchange above, and must
    # not discard the destination of the sign-in that is still pending.
    target = safe_next(request.session.pop(_NEXT_KEY, None)) or "/"
    store: ControlStore = request.app.state.store
    ttl = timedelta(days=config.auth.session_ttl_days)
    token = new_token()
    try:
        installed_team = await store.installed_team_id()
        if installed_team is not None and installed_team != identity.team_id:
            logger.warning("oidc identity rejected: another workspace", team_id=identity.team_id)
            return _login_error_redirect(config, "other_workspace")
        user = await store.upsert_user(
            slack_team_id=identity.team_id,
            slack_user_id=identity.slack_user_id,
            email=identity.email,
            display_name=identity.name or "",
            avatar_url=identity.picture,
            team_name=identity.team_name or "",
        )
        await store.create_auth_session(hash_token(token), user.id, ttl)
        # A sign-in asks Slack again instead of reusing an answer from before.
        request.app.state.slack_members.forget(user.slack_team_id, user.slack_user_id)
        # Only after the new session is saved: a failed sign-in must not also
        # sign the browser out of the account it already had.
        previous = request.cookies.get(SESSION_COOKIE)
        if previous is not None:
            await store.delete_auth_session(hash_token(previous))
    except Exception:
        # A raw 500 here would strand the user on a bare error page mid-flow.
        logger.error("sign-in persistence failed", exc_info=True)
        return _login_error_redirect(config, "sign_in_failed")

    response = RedirectResponse(f"{config.api.app_url}{target}", status_code=302)
    set_session_cookie(
        response,
        token,
        max_age=int(ttl.total_seconds()),
        secure=config.api.cookie_secure,
    )
    return response


@router.post("/logout", status_code=204)
async def logout(request: Request) -> Response:
    config: Config = request.app.state.config
    token = request.cookies.get(SESSION_COOKIE)
    if token is not None:
        store: ControlStore = request.app.state.store
        await store.delete_auth_session(hash_token(token))
    response = Response(status_code=204)
    clear_session_cookie(response, secure=config.api.cookie_secure)
    return response
