from __future__ import annotations

import json
from collections.abc import AsyncGenerator, Callable, Generator

import httpx
import structlog

from hondo.mcp.models import MCPConnection, MCPError
from hondo.mcp.oauth import MCPOAuth
from hondo.mcp.outcomes import RequestEvent

logger = structlog.get_logger("hondo.mcp")

# Set by the MCP transport and the HTTP client. A key header cannot replace them.
RESERVED_HEADERS = frozenset(
    {
        "accept",
        "accept-encoding",
        "connection",
        "content-encoding",
        "content-length",
        "content-type",
        "host",
        "keep-alive",
        "last-event-id",
        "mcp-protocol-version",
        "mcp-session-id",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)
HEADER_NAME = r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$"


def printable_ascii(value: str) -> bool:
    return all(" " <= char <= "~" for char in value)


class UnsavedKeyAuth(httpx.Auth):
    """Sends key headers that are not saved yet. A refused key fails at once.

    The key is the only thing the admin typed, so a 400 also means a bad key:
    GitHub answers 400 to a value that is not a token.
    """

    def __init__(self, headers: dict[str, str]) -> None:
        self.last_headers: dict[str, str] | None = headers

    def auth_flow(self, request: httpx.Request) -> Generator[httpx.Request, httpx.Response]:
        request.headers.update(self.last_headers or {})
        response = yield request
        if response.status_code in (400, 401, 403):
            raise MCPError("key_refused", 422)


class MCPAuth(httpx.Auth):
    def __init__(
        self,
        oauth: MCPOAuth,
        saved: MCPConnection,
        *,
        on_request: Callable[[RequestEvent], None] | None = None,
    ) -> None:
        self.oauth, self.saved = oauth, saved
        self.last_headers: dict[str, str] | None = None
        self.on_request = on_request

    async def _key_headers(self) -> tuple[MCPConnection, dict[str, str]]:
        saved = await self.oauth.store.reload_credentials(self.saved)
        if saved.credential_version != self.saved.credential_version:
            raise MCPError("connection_access_changed", 409)
        if not saved.credentials or saved.status == "sign_in_required":
            raise MCPError("sign_in_required", 409)
        return saved, json.loads(self.oauth.cipher.decrypt(saved.credentials))["headers"]

    async def _authorize(
        self, rejected_credentials: str | None = None
    ) -> tuple[MCPConnection, dict[str, str]]:
        try:
            if self.saved.auth == "key":
                saved, headers = await self._key_headers()
            else:
                saved, token = await self.oauth.access_token(
                    self.saved, rejected_credentials=rejected_credentials
                )
                headers = {"Authorization": f"Bearer {token}"}
            self.saved = saved
            self.last_headers = headers
            return saved, headers
        except MCPError:
            raise
        except Exception as exc:
            logger.warning("mcp authentication failed", error_type=type(exc).__name__)
            raise MCPError("connection_unavailable") from None

    async def async_auth_flow(
        self, request: httpx.Request
    ) -> AsyncGenerator[httpx.Request, httpx.Response]:
        body = json.loads(request.content) if request.method == "POST" else {}
        is_request = isinstance(body, dict) and "id" in body
        is_call = is_request and body.get("method") == "tools/call"
        saved, headers = await self._authorize()
        for attempt in range(2):
            request.headers.update(headers)
            if is_call and self.on_request is not None:
                self.on_request("dispatch")
            response = yield request
            # A server answers 404 to every request of a session that it ended,
            # so the request did not run. Without a session ID, a 404 shows nothing.
            if is_request and response.status_code == 404 and "mcp-session-id" in request.headers:
                if is_call and self.on_request is not None:
                    self.on_request("refused")
                raise MCPError("session_ended")
            if is_call and response.status_code in (401, 403) and self.on_request is not None:
                self.on_request("refused")
            # Only an OAuth token can change. A refused key fails the same way again.
            if response.status_code == 401 and attempt == 0 and saved.auth != "key":
                saved, headers = await self._authorize(saved.credentials)
                continue
            if response.status_code == 401:
                await self.oauth.store.record_status(saved, "sign_in_required")
            if response.status_code in (401, 403):
                raise MCPError(
                    "sign_in_required" if response.status_code == 401 else "access_denied",
                    409 if response.status_code == 401 else 403,
                )
            return
