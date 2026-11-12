from __future__ import annotations

from fastapi import HTTPException, Request

from hondo.api.sessions import SESSION_COOKIE, hash_token
from hondo.api.store import ControlStore, User
from hondo.mcp.catalog import BuiltinCatalog


def control_store(request: Request) -> ControlStore:
    return request.app.state.store


def builtin_catalog(request: Request) -> BuiltinCatalog:
    return request.app.state.builtin_catalog


def session_hash(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    return hash_token(token) if token is not None else None


async def session_user(request: Request) -> User | None:
    """The signed-in user, or None — for flows that must not answer with a 401."""

    session = session_hash(request)
    if session is None:
        return None
    return await control_store(request).get_session_user(session)


async def current_user(request: Request) -> User:
    user = await session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    return user
