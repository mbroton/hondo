from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from uuid import UUID

import structlog
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from hondo.mcp.auth import MCPAuth, UnsavedKeyAuth
from hondo.mcp.catalog import BuiltinCatalog
from hondo.mcp.execution import CALL_TIMEOUT
from hondo.mcp.http import ArgumentError, build_request, same_origin, send, shown_body
from hondo.mcp.models import (
    HTTPTestResult,
    MCPChannelPage,
    MCPConnection,
    MCPError,
    MCPOAuthResult,
    RemoteTool,
)
from hondo.mcp.oauth import MCPOAuth, digest
from hondo.mcp.outcomes import call_failure, error_details
from hondo.mcp.remote import MCPRemote
from hondo.mcp.store import MCPStore

logger = structlog.get_logger("hondo.mcp")

MAX_HTTP_TOOLS = 50


@asynccontextmanager
async def remote_errors() -> AsyncIterator[None]:
    try:
        yield
    except MCPError:
        raise
    except Exception as exc:
        logger.warning("mcp request failed", errors=error_details(exc))
        if isinstance(exc, SlackApiError) and exc.response.status_code == 429:
            headers = {key.lower(): value for key, value in exc.response.headers.items()}
            try:
                retry_after = max(1, int(headers.get("retry-after", "60")))
            except (TypeError, ValueError):
                retry_after = 60
            raise MCPError("rate_limited", 429, retry_after=retry_after) from None
        if isinstance(exc, SlackApiError) and exc.response.get("error") in (
            "missing_scope",
            "invalid_auth",
            "token_revoked",
            "account_inactive",
        ):
            raise MCPError("workspace_reconnect_required", 409) from None
        raise call_failure(exc).error from None


class MCPService:
    def __init__(self, store: MCPStore, oauth: MCPOAuth, remote: MCPRemote) -> None:
        self.store, self.oauth, self.remote = store, oauth, remote

    async def refresh_catalog(self, workspace_id: UUID, connection_id: UUID) -> MCPConnection:
        saved = await self.store.get_connection(workspace_id, connection_id)
        if saved.kind == "http":
            return saved
        auth = MCPAuth(self.oauth, saved)
        try:
            async with remote_errors():
                async with self.remote.session(saved.url, auth) as session:
                    tools = await self.remote.tools(session)
                await self.store.save_catalog(saved, tools)
        except MCPError as exc:
            if exc.code not in (
                "connection_changed",
                "connection_access_changed",
                "sign_in_required",
            ):
                await self.store.record_status(auth.saved, "unavailable")
            raise
        return await self.store.get_connection(workspace_id, connection_id)

    async def save_key(
        self, workspace_id: UUID, connection_id: UUID, user_id: UUID, headers: dict[str, str]
    ) -> MCPConnection:
        if not headers:
            try:
                await self._http_connection(workspace_id, connection_id)
            except MCPError as exc:
                if exc.code != "not_http":
                    raise
                raise MCPError("invalid_request", 422) from exc
        saved = await self.store.save_key(
            workspace_id,
            connection_id,
            user_id,
            self.oauth.cipher.encrypt(json.dumps({"headers": headers})),
            list(headers),
        )
        try:
            return await self.refresh_catalog(workspace_id, saved.id)
        except MCPError:
            return await self.store.get_connection(workspace_id, saved.id)

    async def connect_builtin(
        self,
        workspace_id: UUID,
        user_id: UUID,
        catalog: BuiltinCatalog,
        builtin_id: str,
        name: str,
        inputs: dict[str, str],
    ) -> MCPConnection:
        builtin = catalog.find(builtin_id)
        if builtin is None:
            raise MCPError("builtin_not_found", 404)
        headers = builtin.fill(inputs)
        async with remote_errors():
            async with self.remote.session(builtin.url, UnsavedKeyAuth(headers)) as session:
                tools = await self.remote.tools(session)
        return await self.store.create_builtin_connection(
            workspace_id,
            user_id,
            name=name,
            url=builtin.url,
            builtin=builtin.id,
            credentials=self.oauth.cipher.encrypt(json.dumps({"headers": headers})),
            header_names=list(headers),
            tools=tools,
        )

    async def save_builtin_key(
        self,
        workspace_id: UUID,
        connection_id: UUID,
        user_id: UUID,
        catalog: BuiltinCatalog,
        inputs: dict[str, str],
    ) -> MCPConnection:
        saved = await self.store.get_connection(workspace_id, connection_id)
        builtin = catalog.find(saved.builtin)
        if builtin is None:
            raise MCPError("not_builtin", 409)
        return await self.save_key(workspace_id, connection_id, user_id, builtin.fill(inputs))

    async def create_http(
        self, workspace_id: UUID, user_id: UUID, name: str, url: str, headers: dict[str, str]
    ) -> MCPConnection:
        return await self.store.create_http_connection(
            workspace_id,
            user_id,
            name=name,
            url=url,
            credentials=self.oauth.cipher.encrypt(json.dumps({"headers": headers})),
            header_names=list(headers),
        )

    async def _http_connection(self, workspace_id: UUID, connection_id: UUID) -> MCPConnection:
        saved = await self.store.get_connection(workspace_id, connection_id)
        if saved.kind != "http":
            raise MCPError("not_http", 409)
        return saved

    async def save_http_tool(
        self, workspace_id: UUID, connection_id: UUID, replaces: str | None, tool: RemoteTool
    ) -> MCPConnection:
        """Add a tool, or replace the tool named `replaces`. A changed tool loses its approval."""
        saved = await self._http_connection(workspace_id, connection_id)
        assert tool.request is not None
        if not same_origin(tool.request.url, saved.url):
            raise MCPError("tool_url_outside_connector", 422)
        names = [item.name for item in saved.tools]
        if replaces is not None and replaces not in names:
            raise MCPError("tool_not_found", 404)
        if tool.name != replaces and tool.name in names:
            raise MCPError("tool_name_taken", 409)
        if replaces is None and len(names) >= MAX_HTTP_TOOLS:
            raise MCPError("too_many_tools", 409)
        tools = [tool if item.name == replaces else item for item in saved.tools]
        await self.store.save_catalog(saved, tools if replaces is not None else [*tools, tool])
        return await self.store.get_connection(workspace_id, connection_id)

    async def delete_http_tool(
        self, workspace_id: UUID, connection_id: UUID, name: str
    ) -> MCPConnection:
        saved = await self._http_connection(workspace_id, connection_id)
        if name not in {item.name for item in saved.tools}:
            raise MCPError("tool_not_found", 404)
        await self.store.save_catalog(saved, [item for item in saved.tools if item.name != name])
        return await self.store.get_connection(workspace_id, connection_id)

    async def test_http_tool(
        self, workspace_id: UUID, connection_id: UUID, name: str, arguments: dict[str, str]
    ) -> HTTPTestResult:
        """Send one request with sample values, for an admin. It needs no approval."""
        saved = await self._http_connection(workspace_id, connection_id)
        tool = next((item for item in saved.tools if item.name == name), None)
        if tool is None or tool.request is None:
            raise MCPError("tool_not_found", 404)
        if not saved.credentials:
            raise MCPError("sign_in_required", 409)
        try:
            build_request(tool.request, arguments)
        except ArgumentError as exc:
            raise MCPError("invalid_arguments", 422) from exc
        headers = json.loads(self.oauth.cipher.decrypt(saved.credentials))["headers"]
        async with remote_errors(), asyncio.timeout(CALL_TIMEOUT):
            response = await send(tool.request, arguments, headers)
        return HTTPTestResult(
            status=response.status_code, body=shown_body(response, headers.values())
        )

    async def callback(
        self,
        is_admin: Callable[[UUID], Awaitable[bool]],
        user_id: UUID,
        session_hash: str,
        state: str,
        code: str | None,
        issuer: str | None,
        error: str | None,
    ) -> MCPOAuthResult:
        attempt = await self.store.consume_oauth(digest(state), user_id, session_hash)
        payload = json.loads(self.oauth.cipher.decrypt(attempt["payload"]))
        workspace_id = UUID(payload["workspace_id"])
        if not await is_admin(workspace_id):
            raise MCPError("not_found", 404)
        try:
            async with remote_errors(), asyncio.timeout(40):
                saved = await self.oauth.complete(attempt, user_id, code, issuer, error)
        except MCPError as exc:
            return MCPOAuthResult(
                workspace_id,
                attempt["connection_id"],
                "cancelled" if exc.code == "authorization_cancelled" else "authorization_failed",
            )
        try:
            await self.refresh_catalog(workspace_id, saved.id)
        except MCPError:
            pass
        return MCPOAuthResult(workspace_id, saved.id, "connected")

    async def channels(self, workspace_id: UUID, cursor: str | None = None) -> MCPChannelPage:
        encrypted = await self.store.workspace_token(workspace_id)
        if encrypted is None:
            raise MCPError("connection_unavailable")
        client = AsyncWebClient(token=self.oauth.cipher.decrypt(encrypted), timeout=10)
        async with remote_errors(), asyncio.timeout(30):
            page = await client.users_conversations(
                types="public_channel,private_channel",
                exclude_archived=True,
                limit=200,
                cursor=cursor,
            )
        return MCPChannelPage(
            channels=sorted(
                [
                    {"id": channel["id"], "name": channel["name"]}
                    for channel in page.get("channels", [])
                ],
                key=lambda item: item["name"],
            ),
            next_cursor=(page.get("response_metadata") or {}).get("next_cursor") or None,
        )
