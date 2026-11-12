"""`hondo serve` boot: resolving the workspace row and decrypting its bot token."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx2 as httpx
import pytest
from dbos import DBOS
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.toolsets import FunctionToolset

import hondo.deployment as deployment
from hondo.config import Config
from hondo.crypto import TokenCipher, generate_key
from hondo.deployment import decrypt_bot_token, resolve_workspace, serve_teammate
from hondo.models import WorkspaceRecord
from hondo.runtime.model_selection import RunModel
from hondo.store.postgres import PostgresStore
from tests.fakes import FakeStore


async def test_build_runs_with_native_tools() -> None:
    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        assert {tool.name for tool in info.function_tools} == {
            "add_reaction",
            "post_in_thread",
            "read_thread",
            "read_channel",
            "search_channel",
            "lookup_user",
            "write_todos",
            "summary_search",
            "summary_read",
            "skill_read",
            "skill_draft",
            "set_mention_only",
            "run_code",
        }
        return ModelResponse(parts=[TextPart("Ready.")])

    config = make_config(token_key=generate_key())
    store = PostgresStore(config.database.url.get_secret_value())
    try:
        app = deployment.build(config, bot_token="xoxb-test", store=store)
        assert app.models.store is store
        with app.agent.override(model=FunctionModel(model)):
            result = await app.agent.run("Hello.")
        assert result.output == "Ready."
    finally:
        DBOS.destroy(destroy_registry=True)


async def test_build_routes_supplied_tools_through_code_mode() -> None:
    tools: FunctionToolset[RunModel] = FunctionToolset(id="external-test")
    calls: list[str] = []

    @tools.tool_plain
    def project_status() -> str:
        """Get the project's current status."""
        calls.append("project_status")
        return "Ready for release."

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        names = {tool.name for tool in info.function_tools}
        assert "project_status" not in names
        assert {"run_code", "read_thread", "summary_search", "write_todos"} <= names
        instructions = next(m.instructions for m in messages if isinstance(m, ModelRequest))
        assert instructions is not None and "project_status" in instructions
        for part in messages[-1].parts:
            if isinstance(part, ToolReturnPart) and part.tool_name == "run_code":
                assert part.content == "Ready for release."
                return ModelResponse(parts=[TextPart("The project is ready for release.")])
        return ModelResponse(parts=[ToolCallPart("run_code", {"code": "project_status()"})])

    config = make_config(token_key=generate_key())
    store = PostgresStore(config.database.url.get_secret_value())
    try:
        app = deployment.build(config, bot_token="xoxb-test", store=store, toolsets=[tools])
        with app.agent.override(model=FunctionModel(model)):
            result = await app.agent.run("Check the project status.")
        assert result.output == "The project is ready for release."
        assert calls == ["project_status"]
    finally:
        DBOS.destroy(destroy_registry=True)


async def test_build_asks_anthropic_to_cache_the_prompt() -> None:
    bodies: list[dict[str, Any]] = []

    def anthropic(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        events: list[dict[str, Any]] = [
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Ready."},
            },
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
        if not bodies[-1].get("stream"):
            return httpx.Response(
                200,
                json={
                    **events[0]["message"],
                    "content": [{"type": "text", "text": "Ready."}],
                    "stop_reason": "end_turn",
                },
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
            ),
        )

    config = make_config(token_key=generate_key())
    store = PostgresStore(config.database.url.get_secret_value())
    try:
        app = deployment.build(config, bot_token="xoxb-test", store=store)
        async with httpx.AsyncClient(transport=httpx.MockTransport(anthropic)) as http_client:
            model = AnthropicModel(
                "claude-sonnet-4-6",
                provider=AnthropicProvider(api_key="sk-test", http_client=http_client),
            )
            with app.agent.override(model=model):
                result = await app.agent.run("Hello.")
        assert result.output == "Ready."
    finally:
        DBOS.destroy(destroy_registry=True)
    assert len(bodies) == 1
    ephemeral = {"type": "ephemeral", "ttl": "5m"}
    body = bodies[0]
    assert body["cache_control"] == ephemeral
    assert body["tools"][-1]["cache_control"] == ephemeral
    assert ephemeral in [block.get("cache_control") for block in body["system"]]


def make_config(*, team_id: str | None = None, token_key: str | None = None) -> Config:
    slack: dict[str, Any] = {"app_token": "xapp-test"}
    if team_id is not None:
        slack["team_id"] = team_id
    data: dict[str, Any] = {
        "database": {"url": "postgresql://test:test@localhost/test"},
        "workspace": {"slack": slack},
    }
    if token_key is not None:
        data["crypto"] = {"token_key": token_key}
    return Config.model_validate(data)


def make_workspace(
    team: str, *, encrypted: str = "opaque", status: str = "active"
) -> WorkspaceRecord:
    return WorkspaceRecord(
        slack_team_id=team,
        name=f"{team} workspace",
        status=status,
        bot_token_encrypted=encrypted,
    )


async def make_store(*workspaces: WorkspaceRecord) -> FakeStore:
    store = FakeStore()
    for workspace in workspaces:
        store.seed_workspace(workspace)
    return store


async def test_resolve_returns_the_installed_workspace() -> None:
    store = await make_store(make_workspace("T1"))

    resolved = await resolve_workspace(store)

    assert resolved.slack_team_id == "T1"


async def test_resolve_without_a_workspace_exits_with_install_instruction() -> None:
    with pytest.raises(SystemExit, match="install Hondo from the dashboard"):
        await resolve_workspace(await make_store())


async def test_resolve_with_an_inactive_workspace_exits_naming_the_status() -> None:
    store = await make_store(make_workspace("T1", status="disabled"))

    with pytest.raises(SystemExit, match=r"T1 is disabled, not active; reinstall Hondo"):
        await resolve_workspace(store)


def test_decrypt_without_crypto_key_exits_with_generate_key_command() -> None:
    with pytest.raises(SystemExit, match=r"from hondo\.crypto import generate_key"):
        decrypt_bot_token(make_config(), make_workspace("T1"))


def test_decrypt_with_the_wrong_key_exits_with_reinstall_instruction() -> None:
    sealed_with_other_key = TokenCipher(generate_key()).encrypt("xoxb-live")
    config = make_config(token_key=generate_key())

    with pytest.raises(SystemExit, match="reinstall the workspace from the dashboard") as excinfo:
        decrypt_bot_token(config, make_workspace("T1", encrypted=sealed_with_other_key))

    assert "xoxb-live" not in str(excinfo.value)


def test_decrypt_returns_the_stored_plaintext_token() -> None:
    key = generate_key()
    workspace = make_workspace("T1", encrypted=TokenCipher(key).encrypt("xoxb-live"))

    assert decrypt_bot_token(make_config(token_key=key), workspace) == "xoxb-live"


class ClosableFakeStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        self.closed = False

    async def connect(self, **kwargs: Any) -> None:
        await super().connect()

    async def aclose(self) -> None:
        self.closed = True


async def test_serve_wires_one_store_and_the_decrypted_token_then_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = generate_key()
    store = ClosableFakeStore()
    mcp_store = ClosableFakeStore()
    store.seed_workspace(make_workspace("T1", encrypted=TokenCipher(key).encrypt("xoxb-live")))
    build_calls: dict[str, Any] = {}
    aserve_calls: dict[str, Any] = {}
    summarizer = object()
    screen = SimpleNamespace(decide=object())
    approvals = SimpleNamespace(decide=object())

    def fake_build(
        config: Config, *, bot_token: str, store: Any, mcp_store: Any
    ) -> SimpleNamespace:
        build_calls.update(bot_token=bot_token, store=store, mcp_store=mcp_store)
        return SimpleNamespace(
            agent=object(),
            client=object(),
            style=object(),
            directory=object(),
            models=SimpleNamespace(select=object()),
            summarizer=summarizer,
            screen=screen,
            approvals=approvals,
        )

    async def fake_aserve(agent: Any, *, bot_token: str, store: Any, **kwargs: Any) -> None:
        aserve_calls.update(bot_token=bot_token, store=store, **kwargs)

    monkeypatch.setattr(deployment, "PostgresStore", lambda dsn: store)
    monkeypatch.setattr(deployment, "MCPStore", lambda dsn: mcp_store)
    monkeypatch.setattr(deployment, "build", fake_build)
    monkeypatch.setattr(deployment, "aserve", fake_aserve)

    await serve_teammate(make_config(token_key=key))

    # One store per process: the instance serve_teammate connected reaches both.
    assert build_calls["store"] is store
    assert build_calls["mcp_store"] is mcp_store
    assert aserve_calls["store"] is store
    assert aserve_calls["mcp_store"] is mcp_store
    assert aserve_calls["summarizer"] is summarizer
    assert aserve_calls["screen"] is screen.decide
    assert aserve_calls["decide_approval"] is approvals.decide
    assert build_calls["bot_token"] == "xoxb-live"
    assert aserve_calls["bot_token"] == "xoxb-live"
    assert store.closed
    assert mcp_store.closed


async def test_serve_closes_the_store_on_a_failed_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ClosableFakeStore()  # no workspace row installed
    monkeypatch.setattr(deployment, "PostgresStore", lambda dsn: store)

    with pytest.raises(SystemExit, match="install Hondo from the dashboard"):
        await serve_teammate(make_config(token_key=generate_key()))

    assert store.closed
