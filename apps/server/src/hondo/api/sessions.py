from __future__ import annotations

import hashlib
import secrets

from starlette.responses import Response

SESSION_COOKIE = "hondo_session"


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    # Only the hash is stored, so a leaked auth_sessions table can't be
    # replayed as cookies.
    return hashlib.sha256(token.encode()).hexdigest()


def set_session_cookie(response: Response, token: str, *, max_age: int, secure: bool) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookie(response: Response, *, secure: bool) -> None:
    response.delete_cookie(
        SESSION_COOKIE,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
