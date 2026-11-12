from __future__ import annotations

import asyncio
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import Any
from uuid import UUID

import httpx
import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults

from hondo.api.store import ControlStore
from hondo.context import reset_scope, set_scope
from hondo.mcp.http import ArgumentError, build_request, check_template, response_result
from hondo.mcp.models import HTTPParam, HTTPRequest
from hondo.mcp.network import allow_private_networks
from hondo.mcp.runtime import tool_name
from hondo.mcp.service import MCPService
from hondo.runtime.model_selection import RunModel
from tests.test_auth import _client
from tests.test_mcp import (
    OAuthServer,
    fixture,
    mcp_store,
    private_networks,
    remove_admin,
    runtime,
    scope,
    setup_connection,
)
from tests.test_workspaces import _sign_in_as_admin

__all__ = ["fixture", "mcp_store", "private_networks"]

SECRET = "Bearer very-secret-key"
TEMPLATE = HTTPRequest(
    method="GET",
    url="https://api.example.com/v1/items/{id}/notes?q={q}",
    body=None,
    params=(HTTPParam("id", "Item id"), HTTPParam("q", "Search text")),
)


@pytest.mark.parametrize(
    "value,path",
    [
        ("a/b", "/v1/items/a%2Fb/notes"),
        ("@evil.example", "/v1/items/%40evil.example/notes"),
        ("?admin=1", "/v1/items/%3Fadmin%3D1/notes"),
        ("#top", "/v1/items/%23top/notes"),
        ("%2F", "/v1/items/%252F/notes"),
        ("../../admin", "/v1/items/..%2F..%2Fadmin/notes"),
        ("evil.example:443/x", "/v1/items/evil.example%3A443%2Fx/notes"),
        ("https://evil.example/", "/v1/items/https%3A%2F%2Fevil.example%2F/notes"),
    ],
)
def test_a_path_value_cannot_change_the_host_or_the_path(value: str, path: str) -> None:
    url, body = build_request(TEMPLATE, {"id": value, "q": "x"})
    sent = httpx.URL(url)
    assert (sent.scheme, sent.host, sent.port) == ("https", "api.example.com", None)
    assert sent.raw_path == f"{path}?q=x".encode()
    assert body is None


@pytest.mark.parametrize(
    "value,query",
    [
        ("a&admin=true", "q=a%26admin%3Dtrue"),
        ("x#frag", "q=x%23frag"),
        ("@evil.example/", "q=%40evil.example%2F"),
        ("..", "q=.."),
    ],
)
def test_a_query_value_stays_one_query_value(value: str, query: str) -> None:
    url, _ = build_request(TEMPLATE, {"id": "7", "q": value})
    sent = httpx.URL(url)
    assert sent.host == "api.example.com"
    assert sent.raw_path == f"/v1/items/7/notes?{query}".encode()


@pytest.mark.parametrize("value", ["..", ".", ""])
def test_a_path_value_cannot_be_a_dot_segment(value: str) -> None:
    with pytest.raises(ArgumentError):
        build_request(TEMPLATE, {"id": value, "q": "x"})


@pytest.mark.parametrize(
    "arguments",
    [{"id": "1"}, {"id": "1", "q": "x", "host": "evil.example"}, {"id": ["1"], "q": "x"}],
)
def test_arguments_must_match_the_parameters(arguments: dict[str, Any]) -> None:
    with pytest.raises(ArgumentError):
        build_request(TEMPLATE, arguments)


def test_a_body_value_stays_inside_its_json_string() -> None:
    request = HTTPRequest(
        method="POST",
        url="https://api.example.com/search",
        body='{"query": "{q}", "limit": 10}',
        params=(HTTPParam("q", "Search text"),),
    )
    value = 'x", "admin": true, "y": "\\'
    _, body = build_request(request, {"q": value})
    assert body is not None
    assert json.loads(body) == {"query": value, "limit": 10}


@asynccontextmanager
async def api_server(
    body: bytes = b'{"items": [1, 2]}', content_type: bytes = b"application/json"
) -> AsyncIterator[tuple[str, list[dict[str, Any]]]]:
    seen: list[dict[str, Any]] = []

    async def answer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        lines = head.split("\r\n")
        method, target, _ = lines[0].split(" ")
        headers = dict(line.split(": ", 1) for line in lines[1:] if line)
        headers = {key.lower(): value for key, value in headers.items()}
        content = await reader.readexactly(int(headers.get("content-length", "0")))
        seen.append({"method": method, "target": target, "headers": headers, "body": content})
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: "
            + content_type
            + b"\r\nContent-Length: "
            + str(len(body)).encode()
            + b"\r\nConnection: close\r\n\r\n"
            + body
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(answer, "127.0.0.1", 0)
    async with server:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}", seen


def search_tool(base: str, **change: Any) -> dict[str, Any]:
    return {
        "name": "search_items",
        "description": "Search items by text.",
        "method": "POST",
        "url": f"{base}/v1/items/{{id}}/search?q={{q}}",
        "body": '{"text": "{text}"}',
        "params": [
            {"name": "id", "description": "Item id"},
            {"name": "q", "description": "Search text"},
            {"name": "text", "description": "Note text"},
        ],
        **change,
    }


async def http_connector(
    client: httpx.AsyncClient, control: ControlStore, base: str
) -> tuple[UUID, UUID, str]:
    user, workspace = await _sign_in_as_admin(client, control)
    response = await client.post(
        f"/api/workspaces/{workspace}/mcp-connections/http",
        json={
            "name": "Items API",
            "url": base,
            "headers": [{"name": "Authorization", "value": SECRET}],
        },
    )
    assert response.status_code == 201, response.text
    return user, workspace, response.json()["id"]


async def turn_on(
    client: httpx.AsyncClient, workspace: UUID, connection: str, **change: Any
) -> dict[str, Any]:
    path = f"/api/workspaces/{workspace}/mcp-connections/{connection}"
    current = next(
        item
        for item in (await client.get(f"/api/workspaces/{workspace}/mcp-connections")).json()[
            "connections"
        ]
        if item["id"] == connection
    )
    response = await client.put(
        path,
        json={
            "revision": current["revision"],
            "name": current["name"],
            "enabled": True,
            "all_channels": False,
            "channels": ["C01"],
            "allowed_tools": ["search_items"],
            "ask_tools": [],
            "review_tools": True,
            **change,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_admin_creates_an_http_connector_and_its_secret_never_comes_back(
    fixture: tuple[Any, MCPService, OAuthServer], control_store: ControlStore
) -> None:
    app, service, _ = fixture
    async with _client(app) as client:
        _, workspace, connection = await http_connector(
            client, control_store, "https://api.example.com"
        )
        added = await client.post(
            f"/api/workspaces/{workspace}/mcp-connections/{connection}/http-tools",
            json=search_tool("https://api.example.com"),
        )
        listed = await client.get(f"/api/workspaces/{workspace}/mcp-connections")

    assert added.status_code == 201
    saved = added.json()
    assert saved["kind"] == "http"
    assert saved["connected"] is True
    assert saved["header_names"] == ["Authorization"]
    assert saved["tools"] == [
        {
            "name": "search_items",
            "description": "Search items by text.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "id": {"type": "string", "description": "Item id"},
                    "q": {"type": "string", "description": "Search text"},
                    "text": {"type": "string", "description": "Note text"},
                },
                "required": ["id", "q", "text"],
                "additionalProperties": False,
            },
            "enabled": False,
            "read_only": False,
            "ask": False,
            "request": {
                "method": "POST",
                "url": "https://api.example.com/v1/items/{id}/search?q={q}",
                "body": '{"text": "{text}"}',
                "params": [
                    {"name": "id", "description": "Item id"},
                    {"name": "q", "description": "Search text"},
                    {"name": "text", "description": "Note text"},
                ],
            },
        }
    ]
    assert "very-secret-key" not in added.text
    assert "very-secret-key" not in listed.text
    stored = await service.store._connected_pool.fetchval(
        "SELECT credentials FROM mcp_connections WHERE id=$1", UUID(connection)
    )
    assert "very-secret-key" not in stored
    assert json.loads(service.oauth.cipher.decrypt(stored)) == {
        "headers": {"Authorization": SECRET}
    }


@pytest.mark.parametrize(
    "change",
    [
        {"url": "https://{host}/v1/items"},
        {"url": "https://api.example.com{port}/v1/items"},
        {"url": "https://api.example.com/v1/items/{id}#{q}"},
        {"url": "https://api.example.com/v1/{id}/{q}/{other}"},
        {"url": "https://api.example.com/v1/{id}/{q}/{"},
        {"params": [{"name": "id", "description": "Item id"}]},
        {"body": '{"text": {text}'},
        {"body": "text={text}"},
        {"method": "GET POST"},
    ],
)
async def test_a_definition_that_lets_values_change_the_request_is_refused(
    fixture: tuple[Any, MCPService, OAuthServer],
    control_store: ControlStore,
    change: dict[str, Any],
) -> None:
    app, _, _ = fixture
    async with _client(app) as client:
        _, workspace, connection = await http_connector(
            client, control_store, "https://api.example.com"
        )
        response = await client.post(
            f"/api/workspaces/{workspace}/mcp-connections/{connection}/http-tools",
            json=search_tool("https://api.example.com", **change),
        )
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_request"}


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example/v1/items/{id}/search?q={q}",
        "http://api.example.com/v1/items/{id}/search?q={q}",
        "https://api.example.com:8443/v1/items/{id}/search?q={q}",
        "https://api.example.com.evil.example/v1/items/{id}/search?q={q}",
    ],
)
async def test_a_tool_url_must_keep_the_connector_host(
    fixture: tuple[Any, MCPService, OAuthServer], control_store: ControlStore, url: str
) -> None:
    app, _, _ = fixture
    async with _client(app) as client:
        _, workspace, connection = await http_connector(
            client, control_store, "https://api.example.com"
        )
        response = await client.post(
            f"/api/workspaces/{workspace}/mcp-connections/{connection}/http-tools",
            json=search_tool("https://api.example.com", url=url),
        )
    assert response.status_code == 422
    assert response.json() == {"detail": "tool_url_outside_connector"}


async def test_http_routes_are_for_admins_only(
    fixture: tuple[Any, MCPService, OAuthServer], control_store: ControlStore
) -> None:
    app, _, _ = fixture
    async with _client(app) as client:
        user, workspace, connection = await http_connector(
            client, control_store, "https://api.example.com"
        )
        tools = f"/api/workspaces/{workspace}/mcp-connections/{connection}/http-tools"
        assert (
            await client.post(tools, json=search_tool("https://api.example.com"))
        ).status_code == 201
        await remove_admin(control_store, user)
        responses = [
            await client.post(
                f"/api/workspaces/{workspace}/mcp-connections/http",
                json={"name": "Other", "url": "https://api.example.com", "headers": []},
            ),
            await client.post(tools, json=search_tool("https://api.example.com", name="other")),
            await client.put(tools + "/search_items", json=search_tool("https://api.example.com")),
            await client.post(
                tools + "/search_items/test", json={"arguments": {"id": "1", "q": "a", "text": "b"}}
            ),
            await client.delete(tools + "/search_items"),
        ]
    for response in responses:
        assert response.status_code == 404 and response.json() == {"detail": "not_found"}


async def test_mcp_and_http_connectors_refuse_each_others_routes(
    fixture: tuple[Any, MCPService, OAuthServer], control_store: ControlStore
) -> None:
    app, _, _ = fixture
    async with _client(app) as client:
        _, workspace, mcp = await setup_connection(client, control_store)
        http = (
            await client.post(
                f"/api/workspaces/{workspace}/mcp-connections/http",
                json={"name": "Items API", "url": "https://api.example.com", "headers": []},
            )
        ).json()["id"]
        on_mcp = await client.post(
            f"/api/workspaces/{workspace}/mcp-connections/{mcp}/http-tools",
            json=search_tool("https://mcp.example"),
        )
        on_http = await client.post(
            f"/api/workspaces/{workspace}/mcp-connections/{http}/authorize", json={}
        )
    assert on_mcp.status_code == 409 and on_mcp.json() == {"detail": "not_http"}
    assert on_http.status_code == 409 and on_http.json() == {"detail": "not_mcp"}


async def test_a_changed_definition_needs_approval_again(
    fixture: tuple[Any, MCPService, OAuthServer], control_store: ControlStore
) -> None:
    app, service, _ = fixture
    base = "https://api.example.com"
    async with _client(app) as client:
        _, workspace, connection = await http_connector(client, control_store, base)
        tools = f"/api/workspaces/{workspace}/mcp-connections/{connection}/http-tools"
        await client.post(tools, json=search_tool(base))
        approved = await turn_on(client, workspace, connection, ask_tools=["search_items"])
        assert [(t["enabled"], t["ask"]) for t in approved["tools"]] == [(True, True)]
        _, ctx = await runtime(service)

        same = await client.put(tools + "/search_items", json=search_tool(base))
        assert same.status_code == 200
        assert [(t["enabled"], t["ask"]) for t in same.json()["tools"]] == [(True, True)]
        assert same.json()["review_required"] is False

        changed = await client.put(
            tools + "/search_items",
            json=search_tool(base, url=f"{base}/v2/items/{{id}}/search?q={{q}}"),
        )
    assert changed.status_code == 200
    assert [(t["enabled"], t["ask"]) for t in changed.json()["tools"]] == [(False, False)]
    assert changed.json()["review_required"] is True
    assert await service.store.callable_tools("T1", "C01", list(ctx.deps.mcp_connections)) == set()


async def test_admin_test_call_sends_the_saved_request_with_its_headers(
    fixture: tuple[Any, MCPService, OAuthServer],
    control_store: ControlStore,
    private_networks: None,
) -> None:
    app, _, _ = fixture
    async with api_server() as (base, seen), _client(app) as client:
        _, workspace, connection = await http_connector(client, control_store, base)
        tools = f"/api/workspaces/{workspace}/mcp-connections/{connection}/http-tools"
        await client.post(tools, json=search_tool(base))
        response = await client.post(
            tools + "/search_items/test",
            json={"arguments": {"id": "a/b", "q": "x y", "text": 'say "hi"'}},
        )
        missing = await client.post(tools + "/search_items/test", json={"arguments": {"id": "1"}})
    assert response.status_code == 200
    assert response.json() == {"status": 200, "body": '{\n  "items": [\n    1,\n    2\n  ]\n}'}
    assert missing.status_code == 422 and missing.json() == {"detail": "invalid_arguments"}
    assert len(seen) == 1
    assert seen[0]["method"] == "POST"
    assert seen[0]["target"] == "/v1/items/a%2Fb/search?q=x%20y"
    assert seen[0]["headers"]["authorization"] == SECRET
    assert seen[0]["headers"]["content-type"] == "application/json"
    assert json.loads(seen[0]["body"]) == {"text": 'say "hi"'}


async def test_a_private_address_is_refused_by_default(
    fixture: tuple[Any, MCPService, OAuthServer],
    control_store: ControlStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _, _ = fixture
    async with api_server() as (local, seen), _client(app) as client:
        _, workspace = await _sign_in_as_admin(client, control_store)
        direct = await client.post(
            f"/api/workspaces/{workspace}/mcp-connections/http",
            json={"name": "Local", "url": local, "headers": []},
        )
        connection = (
            await client.post(
                f"/api/workspaces/{workspace}/mcp-connections/http",
                json={"name": "Items API", "url": "https://api.example.com", "headers": []},
            )
        ).json()["id"]
        tools = f"/api/workspaces/{workspace}/mcp-connections/{connection}/http-tools"
        await client.post(tools, json=search_tool("https://api.example.com"))
        port = int(local.rsplit(":", 1)[1])

        async def resolve(*args: Any, **kwargs: Any) -> list[Any]:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", resolve)
        response = await client.post(
            tools + "/search_items/test", json={"arguments": {"id": "1", "q": "a", "text": "b"}}
        )
    assert direct.status_code == 422 and direct.json() == {"detail": "invalid_server_url"}
    assert response.status_code == 422 and response.json() == {"detail": "invalid_server_url"}
    assert seen == []


async def ready_http(
    fixture: tuple[Any, MCPService, OAuthServer],
    control: ControlStore,
    base: str,
    **policy: Any,
) -> None:
    app = fixture[0]
    async with _client(app) as client:
        _, workspace, connection = await http_connector(client, control, base)
        await client.post(
            f"/api/workspaces/{workspace}/mcp-connections/{connection}/http-tools",
            json=search_tool(base),
        )
        await turn_on(client, workspace, connection, **policy)


ARGUMENTS = {"id": "a/b", "q": "x", "text": "hello"}


async def test_the_model_calls_an_http_tool_like_a_connector_tool(
    fixture: tuple[Any, MCPService, OAuthServer],
    control_store: ControlStore,
    private_networks: None,
) -> None:
    service = fixture[1]
    async with api_server() as (base, seen):
        await ready_http(fixture, control_store, base)
        tools, ctx = await runtime(service)
        selection = ctx.deps.mcp_connections[0]
        name = tool_name(selection, selection.tools[0])
        token = set_scope(scope())
        try:
            catalog = await tools.get_tools(ctx)
            result = await tools.call_tool(name, {"arguments": ARGUMENTS}, ctx, catalog[name])
            wrong = await tools.call_tool(
                name, {"arguments": {"id": "1"}}, replace(ctx, tool_call_id="two"), catalog[name]
            )
        finally:
            reset_scope(token)
    assert list(catalog) == [name]
    assert catalog[name].tool_def.kind == "function"
    assert catalog[name].tool_def.metadata == {"code_mode": True}
    assert result == {"status": 200, "body": {"items": [1, 2]}}
    assert "No request was sent" in wrong["error"]
    assert [item["target"] for item in seen] == ["/v1/items/a%2Fb/search?q=x"]


async def test_an_http_tool_call_refuses_a_private_address_by_default(
    fixture: tuple[Any, MCPService, OAuthServer], control_store: ControlStore
) -> None:
    service = fixture[1]
    async with api_server() as (base, seen):
        allow_private_networks(True)
        try:
            await ready_http(fixture, control_store, base)
        finally:
            allow_private_networks(False)
        tools, ctx = await runtime(service)
        selection = ctx.deps.mcp_connections[0]
        name = tool_name(selection, selection.tools[0])
        token = set_scope(scope())
        try:
            catalog = await tools.get_tools(ctx)
            result = await tools.call_tool(name, {"arguments": ARGUMENTS}, ctx, catalog[name])
        finally:
            reset_scope(token)
    assert "No action was sent" in result["error"]
    assert seen == []


async def test_http_tools_follow_the_connector_channels(
    fixture: tuple[Any, MCPService, OAuthServer],
    control_store: ControlStore,
    private_networks: None,
) -> None:
    service = fixture[1]
    async with api_server() as (base, seen):
        await ready_http(fixture, control_store, base, all_channels=True, channels=[])
        tools, ctx = await runtime(service)
        selection = ctx.deps.mcp_connections[0]
        name = tool_name(selection, selection.tools[0])
        shared = replace(ctx, deps=replace(ctx.deps, shared_channel=True))
        token = set_scope(scope("C02"))
        try:
            catalog = await tools.get_tools(shared)
            result = await tools.call_tool(name, {"arguments": ARGUMENTS}, shared, catalog[name])
        finally:
            reset_scope(token)
        await service.store._connected_pool.execute(
            "UPDATE mcp_connections SET all_channels=false, channels=ARRAY['C01']"
        )
        tools, ctx = await runtime(service)
        token = set_scope(scope("C02"))
        try:
            elsewhere = await tools.get_tools(ctx)
        finally:
            reset_scope(token)
    assert "not available in this channel" in result["error"]
    assert elsewhere == {}
    assert seen == []


async def test_ask_before_running_stops_an_http_call_for_approval(
    fixture: tuple[Any, MCPService, OAuthServer],
    control_store: ControlStore,
    private_networks: None,
) -> None:
    service = fixture[1]
    async with api_server() as (base, seen):
        await ready_http(fixture, control_store, base, ask_tools=["search_items"])
        tools, ctx = await runtime(service)
        selection = ctx.deps.mcp_connections[0]
        name = tool_name(selection, selection.tools[0])

        async def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            if len(messages) > 1:
                return ModelResponse(parts=[TextPart("done")])
            return ModelResponse(parts=[ToolCallPart(name, {"arguments": ARGUMENTS}, "call-1")])

        agent: Agent[RunModel, str | DeferredToolRequests] = Agent(
            FunctionModel(model),
            deps_type=RunModel,
            toolsets=[tools],
            output_type=[str, DeferredToolRequests],
        )
        token = set_scope(scope())
        try:
            asked = await agent.run("Search", deps=ctx.deps)
            assert isinstance(asked.output, DeferredToolRequests)
            assert [call.tool_name for call in asked.output.approvals] == [name]
            assert seen == []
            approved = await agent.run(
                message_history=asked.all_messages(),
                deps=ctx.deps,
                deferred_tool_results=DeferredToolResults(approvals={"call-1": True}),
            )
        finally:
            reset_scope(token)
    assert approved.output == "done"
    assert [item["target"] for item in seen] == ["/v1/items/a%2Fb/search?q=x"]


@pytest.mark.parametrize(
    "body",
    ['{"ids": [{ids}]}', '{"limit": {ids}}', "{ids}"],
)
def test_a_parameter_outside_a_json_string_is_refused(body: str) -> None:
    with pytest.raises(ValueError):
        check_template("https://api.example.com/search", body, {"ids"})


def test_a_parameter_inside_a_json_string_keeps_the_body_shape() -> None:
    check_template("https://api.example.com/search", '{"ids": "{ids}"}', {"ids"})
    request = HTTPRequest(
        method="POST",
        url="https://api.example.com/search",
        body='{"ids": "{ids}"}',
        params=(HTTPParam(name="ids", description="IDs"),),
    )
    _, body = build_request(request, {"ids": '1", "admin": true, "x": "'})
    assert body is not None
    assert json.loads(body) == {"ids": '1", "admin": true, "x": "'}


def test_a_response_that_echoes_a_header_value_is_redacted() -> None:
    response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=b'{"seen": "Bearer sk-live-123456", "other": "ok"}',
    )
    result = response_result(response, ["Bearer sk-live-123456"])
    assert result == {"status": 200, "body": {"seen": "[redacted]", "other": "ok"}}


def test_a_json_escaped_echo_of_a_header_value_is_redacted() -> None:
    response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=b'{"seen": "Bearer sk\\u002dlive\\u002d123456", "Bearer sk-live-123456": 1}',
    )
    result = response_result(response, ["Bearer sk-live-123456"])
    assert result == {"status": 200, "body": {"seen": "[redacted]", "[redacted]": 1}}


async def test_an_http_connector_can_drop_all_its_headers(
    fixture: tuple[Any, MCPService, OAuthServer], control_store: ControlStore
) -> None:
    app, _, _ = fixture
    async with _client(app) as client:
        _, workspace, connection = await http_connector(
            client, control_store, "https://api.example.com"
        )
        response = await client.put(
            f"/api/workspaces/{workspace}/mcp-connections/{connection}/key",
            json={"headers": []},
        )
    assert response.status_code == 200, response.text
    assert response.json()["header_names"] == []
