from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import httpx
import structlog
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, ClientRequest

from hondo.mcp.auth import MCPAuth, UnsavedKeyAuth
from hondo.mcp.models import MCPError, RemoteTool
from hondo.mcp.network import MAX_RESPONSE_BYTES, public_client

logger = structlog.get_logger("hondo.mcp")
_SESSION_TIMEOUT = 45


def check_depth(value: Any, depth: int = 0) -> None:
    if depth > 64:
        raise MCPError("invalid_tools", 400)
    if isinstance(value, dict):
        for child in value.values():
            check_depth(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            check_depth(child, depth + 1)


def check_references(schema: Any) -> None:
    if not isinstance(schema, dict):
        return
    for keyword in ("$ref", "$dynamicRef", "$recursiveRef"):
        reference = schema.get(keyword)
        if isinstance(reference, str) and not reference.startswith("#"):
            raise MCPError("invalid_tools", 400)
    for keyword, child in schema.items():
        if keyword in (
            "properties",
            "patternProperties",
            "$defs",
            "definitions",
            "dependentSchemas",
            "dependencies",
        ) and isinstance(child, dict):
            for item in child.values():
                check_references(item)
        elif keyword in ("allOf", "anyOf", "oneOf", "prefixItems", "items") and isinstance(
            child, list
        ):
            for item in child:
                check_references(item)
        elif keyword in (
            "items",
            "additionalItems",
            "additionalProperties",
            "contains",
            "propertyNames",
            "unevaluatedItems",
            "unevaluatedProperties",
            "not",
            "if",
            "then",
            "else",
            "contentSchema",
        ):
            check_references(child)


def check_schema(definition: dict[str, Any]) -> None:
    check_depth(definition)
    for keyword in ("inputSchema", "outputSchema"):
        check_references(definition.get(keyword))


class MCPRemote:
    async def call_tool(
        self, session: ClientSession, name: str, *, arguments: dict[str, Any]
    ) -> CallToolResult:
        # SDK schema validation can fetch URLs and block the event loop on remote rules.
        return await session.send_request(
            ClientRequest(
                CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments))
            ),
            CallToolResult,
        )

    @asynccontextmanager
    async def session(
        self,
        url: str,
        auth: MCPAuth | UnsavedKeyAuth,
        *,
        timeout: float | None = _SESSION_TIMEOUT,
    ) -> AsyncIterator[ClientSession]:
        async with public_client() as client:
            client.auth = auth
            # The session's read timeout and the caller's deadline bound reads
            # between SSE events.
            client.timeout = httpx.Timeout(20, connect=10, read=None)
            get_session_id: Callable[[], str | None] | None = None
            protocol_version: str | None = None
            try:
                async with (
                    asyncio.timeout(timeout),
                    streamable_http_client(url, http_client=client, terminate_on_close=False) as (
                        read,
                        write,
                        get_session_id,
                    ),
                ):
                    async with ClientSession(
                        read, write, read_timeout_seconds=timedelta(seconds=30)
                    ) as session:
                        initialized = await session.initialize()
                        protocol_version = str(initialized.protocolVersion)
                        yield session
            finally:
                session_id = get_session_id() if get_session_id else None
                if session_id and auth.last_headers:
                    headers = {
                        **auth.last_headers,
                        "Mcp-Session-Id": session_id,
                        "Accept": "application/json, text/event-stream",
                    }
                    if protocol_version:
                        headers["MCP-Protocol-Version"] = protocol_version
                    # Cleanup cannot refresh credentials or replace a completed result.
                    try:
                        async with asyncio.timeout(2):
                            response = await client.delete(
                                url, headers=headers, auth=httpx.Auth(), timeout=2
                            )
                            # 404: the server has already ended the session.
                            if response.status_code not in (200, 204, 404, 405):
                                logger.warning(
                                    "mcp session cleanup failed", status_code=response.status_code
                                )
                    except Exception as exc:
                        logger.warning("mcp session cleanup failed", error_type=type(exc).__name__)

    async def tools(self, session: ClientSession) -> list[RemoteTool]:
        tools: list[RemoteTool] = []
        names: set[str] = set()
        cursors: set[str] = set()
        cursor = None
        size = 0
        for _ in range(10):
            page = await session.list_tools(cursor=cursor)
            for tool in page.tools:
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", tool.name) or tool.name in names:
                    raise MCPError("invalid_tools", 400)
                definition = tool.model_dump(mode="json", exclude_none=True)
                size += len(json.dumps(definition).encode())
                check_schema(definition)
                if (
                    len(tool.description or "") > 10000
                    or len(tools) >= 200
                    or size > MAX_RESPONSE_BYTES
                ):
                    raise MCPError("invalid_tools", 400)
                names.add(tool.name)
                tools.append(RemoteTool.from_definition(definition))
            if not page.nextCursor:
                return tools
            if page.nextCursor in cursors:
                break
            cursor = page.nextCursor
            cursors.add(cursor)
        raise MCPError("invalid_tools", 400)
