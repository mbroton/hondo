from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import structlog
from fastapi import APIRouter, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

from hondo.api.access import (
    SlackDirectory,
    SlackDirectoryError,
    SlackMemberCache,
    SlackRateLimited,
    SlackWebDirectory,
)
from hondo.api.oidc import OIDCProvider, build_sign_in
from hondo.api.provider_connections import ProviderConnectionError
from hondo.api.routers.admins import router as admins_router
from hondo.api.routers.agents import router as agents_router
from hondo.api.routers.auth import router as auth_router
from hondo.api.routers.health import router as health_router
from hondo.api.routers.mcp import router as mcp_router
from hondo.api.routers.me import router as me_router
from hondo.api.routers.providers import router as providers_router
from hondo.api.routers.skills import router as skills_router
from hondo.api.routers.slack_install import router as slack_install_router
from hondo.api.routers.summaries import router as summaries_router
from hondo.api.routers.workspaces import router as workspaces_router
from hondo.api.schemas import ApiErrorResponse
from hondo.api.slack_oauth import SlackInstaller, build_installer
from hondo.api.store import ControlStore
from hondo.config import Config
from hondo.crypto import TokenCipher
from hondo.mcp.catalog import load_catalog
from hondo.mcp.models import MCPError
from hondo.mcp.network import public_client
from hondo.mcp.oauth import MCPOAuth
from hondo.mcp.remote import MCPRemote
from hondo.mcp.service import MCPService
from hondo.mcp.store import MCPStore
from hondo.skills import SkillError, SkillErrorCode

# The Starlette session cookie carries only the OAuth state/nonce, so it lives
# just long enough to complete a sign-in round trip.
_OAUTH_STATE_MAX_AGE_SECONDS = 600


logger = structlog.get_logger("hondo.api.app")

_SKILL_ERROR_STATUS: dict[SkillErrorCode, int] = {
    "skill_not_found": 404,
    "skill_changed": 409,
    "skill_name_taken": 409,
    "skill_not_ready": 409,
    "skill_limit": 409,
    "invalid_skill_frontmatter": 422,
    "invalid_skill_name": 422,
    "invalid_skill_description": 422,
}


class DashboardFiles(StaticFiles):
    """The built dashboard. Client routes get index.html; unknown /api paths stay 404."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            response = await super().get_response(path, scope)
        except HTTPException as error:
            if error.status_code != 404 or path == "api" or path.startswith("api/"):
                raise
            response = await super().get_response("index.html", scope)
        if response.media_type == "text/html":
            response.headers["Cache-Control"] = "no-cache"
        return response


def create_app(
    config: Config,
    *,
    store: ControlStore | None = None,
    sign_in: OIDCProvider | None = None,
    installer: SlackInstaller | None = None,
    directory: SlackDirectory | None = None,
) -> FastAPI:
    if config.api.session_secret is None:
        raise ValueError(
            "api.session_secret must be set to run the API (HONDO_API__SESSION_SECRET)"
        )
    if config.crypto is None:
        raise ValueError("crypto.token_key must be set to run the API (HONDO_CRYPTO__TOKEN_KEY)")
    cipher = TokenCipher(config.crypto.token_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        control_store = (
            store if store is not None else ControlStore(config.database.url.get_secret_value())
        )
        await control_store.connect()
        app.state.store = control_store
        mcp_store = MCPStore(config.database.url.get_secret_value())
        app.state.mcp_store = mcp_store
        try:
            await mcp_store.connect(run_migrations=False, pool_min_size=1, pool_max_size=5)
            async with httpx.AsyncClient() as client, public_client() as mcp_client:
                app.state.provider_client = client
                app.state.mcp_service = MCPService(
                    mcp_store,
                    MCPOAuth(mcp_store, cipher, mcp_client, config.api.public_base_url),
                    MCPRemote(),
                )
                yield
        finally:
            await mcp_store.aclose()
            await control_store.aclose()

    app = FastAPI(
        lifespan=lifespan,
        responses={422: {"model": ApiErrorResponse, "description": "Invalid request."}},
    )
    app.state.provider_cipher = cipher
    app.state.builtin_catalog = load_catalog()

    @app.exception_handler(MCPError)
    async def mcp_error(request: Request, error: MCPError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status,
            content={"detail": error.code},
            headers={"Retry-After": str(error.retry_after)}
            if error.retry_after is not None
            else None,
        )

    @app.exception_handler(SkillError)
    async def skill_error(request: Request, error: SkillError) -> JSONResponse:
        return JSONResponse(
            status_code=_SKILL_ERROR_STATUS[error.code], content={"detail": error.code}
        )

    @app.exception_handler(SlackDirectoryError)
    async def slack_unavailable(request: Request, error: SlackDirectoryError) -> JSONResponse:
        logger.warning("slack request failed", path=request.url.path, error=str(error))
        if isinstance(error, SlackRateLimited):
            return JSONResponse(
                status_code=429,
                content={"detail": "rate_limited"},
                headers={"Retry-After": str(error.retry_after)},
            )
        return JSONResponse(status_code=503, content={"detail": "unavailable"})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, error: RequestValidationError) -> JSONResponse:
        # FastAPI's default validation response includes the rejected input,
        # which can contain a provider credential.
        return JSONResponse(status_code=422, content={"detail": "invalid_request"})

    @app.exception_handler(ProviderConnectionError)
    async def connection_error(request: Request, error: ProviderConnectionError) -> JSONResponse:
        status = {
            "key_rejected": 400,
            "model_unavailable": 400,
            "connection_not_found": 404,
            "connection_in_use": 409,
            "connection_required": 409,
            "connection_name_taken": 409,
        }.get(error.code, 503)
        return JSONResponse(status_code=status, content={"detail": error.code})

    app.state.config = config
    app.state.sign_in = sign_in if sign_in is not None else build_sign_in(config.auth)
    app.state.slack_directory = directory if directory is not None else SlackWebDirectory()
    app.state.slack_members = SlackMemberCache()
    app.state.slack_installer = (
        installer if installer is not None else build_installer(config.auth, cipher)
    )
    # The URL space is owned here: routers declare paths relative to the
    # prefix they're mounted at, and the /api segment is spelled once.
    # Everything lives under it except the bare /healthz infra probe.
    app.include_router(health_router)
    api = APIRouter(prefix="/api")
    api.include_router(auth_router, prefix="/auth")
    api.include_router(me_router)
    api.include_router(providers_router, tags=["providers"])
    api.include_router(agents_router, tags=["agents"])
    api.include_router(slack_install_router, prefix="/slack", tags=["slack"])
    api.include_router(workspaces_router, tags=["workspaces"])
    api.include_router(admins_router, tags=["admins"])
    api.include_router(mcp_router, tags=["mcp"])
    api.include_router(skills_router, tags=["skills"])
    api.include_router(summaries_router, tags=["summaries"])
    app.include_router(api)
    if config.api.dashboard_dir is not None:
        app.mount("/", DashboardFiles(directory=config.api.dashboard_dir), name="dashboard")
    app.add_middleware(
        SessionMiddleware,
        secret_key=config.api.session_secret.get_secret_value(),
        max_age=_OAUTH_STATE_MAX_AGE_SECONDS,
        same_site="lax",
        https_only=config.api.cookie_secure,
    )
    if config.api.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(config.api.cors_origins),
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["Retry-After"],
        )
    return app
