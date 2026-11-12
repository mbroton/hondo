from __future__ import annotations

import hmac
import secrets
from typing import Annotated
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from starlette.responses import RedirectResponse

from hondo.api.access import AdminAccess, SlackDirectoryError, admin_access
from hondo.api.deps import session_user
from hondo.api.slack_oauth import SlackInstallation, SlackInstaller, SlackInstallError
from hondo.api.store import (
    ControlStore,
    InstallRefused,
    OtherWorkspaceInstalled,
    SlackStatus,
    User,
)
from hondo.config import Config

logger = structlog.get_logger("hondo.api.slack_install")

router = APIRouter()

# The pending install lives in the Starlette session cookie, which is signed and
# expires with the OAuth round trip. It carries only the CSRF state: who may
# install is decided from the Slack account that approved the install.
_PENDING_KEY = "slack_install"
_INSTALLED = "/?installed=1"
_REFUSED = "/?slack_error=not_admin"


def _installer(request: Request) -> SlackInstaller | None:
    installer: SlackInstaller | None = request.app.state.slack_installer
    return installer


def _redirect_uri(config: Config) -> str:
    return f"{config.api.public_base_url}/api/slack/install/callback"


def _back_to_app(config: Config, code: str) -> RedirectResponse:
    # 302 and back to the SPA: the browser arrives here on a GET mid-flow and
    # must never land on a bare JSON body.
    return RedirectResponse(f"{config.api.app_url}/?slack_error={code}", status_code=302)


def _land(
    config: Config, user: User | None, installation: SlackInstallation, path: str
) -> RedirectResponse:
    """Open the dashboard as the Slack account that approved the install.

    Install needs no Hondo account first, so a browser without that
    account's session signs in on the way.
    """

    if (
        user is not None
        and user.slack_team_id == installation.team_id
        and user.slack_user_id == installation.installer_user_id
    ):
        return RedirectResponse(f"{config.api.app_url}{path}", status_code=302)
    query = urlencode({"team": installation.team_id, "next": path})
    return RedirectResponse(
        f"{config.api.public_base_url}/api/auth/slack/login?{query}", status_code=302
    )


@router.get(
    "/install",
    responses={
        302: {"description": "Redirect to Slack consent"},
        404: {"description": "Slack install is not configured"},
    },
)
async def install(request: Request) -> Response:
    config: Config = request.app.state.config
    impl = _installer(request)
    if impl is None:
        raise HTTPException(status_code=404, detail="not found")
    state = secrets.token_urlsafe(32)
    request.session[_PENDING_KEY] = {"state": state}
    # No team hint: an install started from Slack may be for another
    # workspace than the browser's Hondo session, and Slack lets people pick.
    return RedirectResponse(
        impl.authorize_url(redirect_uri=_redirect_uri(config), state=state), status_code=302
    )


@router.get(
    "/install/callback",
    responses={302: {"description": "Redirect back to the dashboard"}},
)
async def install_callback(
    request: Request,
    user: Annotated[User | None, Depends(session_user)],
    access: Annotated[AdminAccess, Depends(admin_access)],
) -> Response:
    config: Config = request.app.state.config
    impl = _installer(request)
    if impl is None:
        return _back_to_app(config, "unavailable")

    # State first, and only then consume it: Slack echoes the state on denial
    # too, so a forged error callback can't discard a genuine pending install.
    if not _state_matches(request.session.get(_PENDING_KEY), request.query_params.get("state")):
        logger.warning("slack install state did not match")
        return _back_to_app(config, "expired")
    request.session.pop(_PENDING_KEY, None)

    if request.query_params.get("error") is not None:
        return _back_to_app(config, "denied")
    code = request.query_params.get("code")
    if not code:
        return _back_to_app(config, "exchange_failed")

    try:
        installation = await impl.exchange(code=code, redirect_uri=_redirect_uri(config))
    except SlackInstallError:
        logger.warning("slack install exchange failed", exc_info=True)
        return _back_to_app(config, "exchange_failed")

    lookup_failed = False
    try:
        # Asked fresh with the new token; the answer also serves the
        # installer's next access decision.
        installer = await access.lookup(
            installation.bot_token_encrypted,
            installation.team_id,
            installation.installer_user_id,
        )
    except SlackDirectoryError as exc:
        # Only Slack admins of an already installed workspace need this answer;
        # listed admins and first installs do not depend on it.
        logger.warning("slack installer lookup failed", error=str(exc))
        access.cache.forget(installation.team_id, installation.installer_user_id)
        installer = None
        lookup_failed = True

    store: ControlStore = request.app.state.store
    try:
        result = await store.install_workspace(
            slack_team_id=installation.team_id,
            name=installation.team_name,
            bot_token_encrypted=installation.bot_token_encrypted,
            installer_id=installation.installer_user_id,
            installer_status=None
            if lookup_failed
            else SlackStatus(
                admin=installer is not None and installer.admin,
                active=installer is not None and installer.active,
            ),
        )
    except Exception:
        # The token is already granted at this point, so a failed write must not
        # strand the user on an error page — they can retry the install.
        logger.error("slack install persistence failed", exc_info=True)
        return _back_to_app(config, "install_failed")

    if isinstance(result, OtherWorkspaceInstalled):
        logger.warning("slack install refused: another workspace", team_id=installation.team_id)
        return _back_to_app(config, "other_workspace")
    if isinstance(result, InstallRefused):
        if lookup_failed:
            # The installer may be a Slack admin; Slack just didn't say. A retry
            # can succeed, so don't tell them they have no access.
            return _back_to_app(config, "install_failed")
        logger.warning("slack install refused: installer is not an admin")
        return _land(config, user, installation, _REFUSED)
    logger.info(
        "workspace_installed",
        team_id=installation.team_id,
        workspace_id=str(result.workspace_id),
        created=result.created,
    )
    return _land(config, user, installation, _INSTALLED)


def _state_matches(pending: object, state: str | None) -> bool:
    if not isinstance(pending, dict) or state is None:
        return False
    expected = pending.get("state")
    if not isinstance(expected, str):
        return False
    # Compared as bytes: compare_digest refuses str arguments that aren't ASCII,
    # and the state here is whatever arrived in the query string.
    return hmac.compare_digest(expected.encode(), state.encode())
