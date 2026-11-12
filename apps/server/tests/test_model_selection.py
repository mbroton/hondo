from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import uuid4

import httpx2 as httpx
import pytest
from dbos import DBOS, SetWorkflowID
from pydantic_ai import Agent
from pydantic_ai.capabilities.resolve_model_id import ResolveModelId
from pydantic_ai.durable_exec.dbos import DBOSDurability
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models import Model, ModelResolutionContext
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.providers.vercel import VercelProvider

import hondo.runtime.model_selection as model_selection
from hondo.agent_settings import AgentModel
from hondo.config import Config, RuntimeConfig
from hondo.crypto import TokenCipher, TokenCipherError, generate_key
from hondo.models import Session, SessionKey, SteerPriority
from hondo.runtime.durable import step
from hondo.runtime.model_selection import RunModel, RuntimeModels
from hondo.runtime.runner import SessionRunner
from hondo.slack import SlackStyle
from hondo.users import UserDirectory
from tests.fakes import FakeSlack, FakeStore, make_event

TEAM = "T01"
FIRST_MODEL_NAME = "xai/grok-4.5"
SECOND_MODEL_NAME = "openai/gpt-4.1"
FIRST_MODEL_ID = f"vercel:{FIRST_MODEL_NAME}"
SECOND_MODEL_ID = f"vercel:{SECOND_MODEL_NAME}"


def make_config(pg_dsn: str, token_key: str) -> Config:
    return Config.model_validate(
        {
            "database": {"url": pg_dsn},
            "crypto": {"token_key": token_key},
            "workspace": {"slack": {"app_token": "xapp-test"}},
        }
    )


async def test_native_vercel_keeps_run_model_but_uses_rotated_key(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_key = generate_key()
    first_key = "vck-first-plaintext-test-key"
    second_key = "vck-second-plaintext-test-key"
    cipher = TokenCipher(token_key)
    first_ciphertext = cipher.encrypt(first_key)
    second_ciphertext = cipher.encrypt(second_key)
    workspace_id = uuid4()
    connection_id = uuid4()
    store = FakeStore()
    store.agent_models[TEAM] = AgentModel(
        connection_id=connection_id,
        workspace_id=workspace_id,
        provider="vercel",
        model_name=FIRST_MODEL_NAME,
    )
    store.provider_keys[(workspace_id, connection_id)] = first_ciphertext

    requests: list[tuple[str, str, str]] = []

    async def gateway(request: httpx.Request) -> httpx.Response:
        payload: dict[str, Any] = json.loads(request.content)
        requests.append((request.url.path, payload["model"], request.headers["Authorization"]))
        if len(requests) == 1:
            message: dict[str, Any] = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-rotate",
                        "type": "function",
                        "function": {
                            "name": "rotate_saved_model",
                            "arguments": "{}",
                        },
                    }
                ],
            }
            finish_reason = "tool_calls"
        else:
            message = {
                "role": "assistant",
                "content": (
                    "First workflow complete." if len(requests) == 2 else "Fresh workflow complete."
                ),
            }
            finish_reason = "stop"
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{len(requests)}",
                "object": "chat.completion",
                "created": 1_700_000_000,
                "model": payload["model"],
                "choices": [
                    {
                        "index": 0,
                        "message": message,
                        "finish_reason": finish_reason,
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 2,
                    "total_tokens": 12,
                },
            },
        )

    config = make_config(pg_dsn, token_key)
    models = RuntimeModels(store, config)
    suffix = uuid4().hex[:8]
    schema = f"hondo_model_selection_{suffix}"
    first_workflow_id = f"model-selection-{suffix}-first"
    second_workflow_id = f"model-selection-{suffix}-second"

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as http_client:
        native_provider = VercelProvider

        def mocked_vercel_provider(*, api_key: str) -> VercelProvider:
            return native_provider(api_key=api_key, http_client=http_client)

        monkeypatch.setattr(model_selection, "VercelProvider", mocked_vercel_provider)
        agent: Agent[RunModel, str] = Agent(
            model=FIRST_MODEL_ID,
            deps_type=RunModel,
            name=f"model_selection_{suffix}",
            capabilities=[
                ResolveModelId(models.resolve),
                DBOSDurability(
                    name=f"model_selection_{suffix}",
                    parallel_execution_mode="sequential",
                ),
            ],
        )

        @agent.tool_plain
        async def rotate_saved_model() -> str:
            store.agent_models[TEAM] = AgentModel(
                connection_id=connection_id,
                workspace_id=workspace_id,
                provider="vercel",
                model_name=SECOND_MODEL_NAME,
            )
            store.provider_keys[(workspace_id, connection_id)] = second_ciphertext
            return "saved model and key rotated"

        DBOS(
            config={
                "name": "model-selection-test",
                "system_database_url": pg_dsn,
                "run_admin_server": False,
                "dbos_system_schema": schema,
                "log_level": "WARNING",
            }
        )  # type: ignore[arg-type]

        @DBOS.workflow(name=f"test.model_selection.{suffix}")
        async def run_selected_model(prompt: str) -> str:
            selected = await step("load_model", models.select, TEAM)
            assert selected is not None
            result = await agent.run(prompt, model=selected.model_id, deps=selected)
            return result.output

        try:
            DBOS.launch()
            with SetWorkflowID(first_workflow_id):
                first_output = await asyncio.wait_for(
                    run_selected_model("Rotate the credentials, then answer."), timeout=15
                )
            with SetWorkflowID(second_workflow_id):
                second_output = await asyncio.wait_for(
                    run_selected_model("Answer without a tool."), timeout=15
                )

            assert first_output == "First workflow complete."
            assert second_output == "Fresh workflow complete."
            assert requests == [
                ("/v1/chat/completions", FIRST_MODEL_NAME, f"Bearer {first_key}"),
                ("/v1/chat/completions", FIRST_MODEL_NAME, f"Bearer {second_key}"),
                ("/v1/chat/completions", SECOND_MODEL_NAME, f"Bearer {second_key}"),
            ]

            steps = await asyncio.wait_for(
                DBOS.list_workflow_steps_async(first_workflow_id), timeout=10
            )
            selection_outputs = [
                item["output"] for item in steps if item["function_name"] == "load_model"
            ]
            assert selection_outputs == [
                RunModel(FIRST_MODEL_ID, connection_id=connection_id, workspace_id=workspace_id)
            ]

            status = await asyncio.wait_for(
                DBOS.get_workflow_status_async(first_workflow_id), timeout=10
            )
            assert status is not None
            durable_state = json.dumps(
                {"input": status.input, "output": status.output, "steps": steps},
                default=str,
                sort_keys=True,
            )
            assert FIRST_MODEL_ID in durable_state
            assert str(connection_id) in durable_state
            assert str(workspace_id) in durable_state
            for secret in (first_key, second_key, first_ciphertext, second_ciphertext):
                assert secret not in durable_state
        finally:
            DBOS.destroy(destroy_registry=True)


async def test_vercel_requests_ask_the_gateway_to_cache_prompts(
    pg_dsn: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_key = generate_key()
    store = FakeStore()
    workspace_id, connection_id = uuid4(), uuid4()
    store.agent_models[TEAM] = AgentModel(
        connection_id, workspace_id, "vercel", "anthropic/claude-sonnet-5"
    )
    store.provider_keys[(workspace_id, connection_id)] = TokenCipher(token_key).encrypt("vck-key")
    models = RuntimeModels(store, make_config(pg_dsn, token_key))
    selected = await models.select(TEAM)
    assert selected is not None
    bodies: list[dict[str, Any]] = []

    def gateway(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "created": 1_700_000_000,
                "model": "anthropic/claude-sonnet-5",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "Hi."},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway)) as http_client:
        native_provider = VercelProvider

        def mocked_vercel_provider(*, api_key: str) -> VercelProvider:
            return native_provider(api_key=api_key, http_client=http_client)

        monkeypatch.setattr(model_selection, "VercelProvider", mocked_vercel_provider)
        agent: Agent[RunModel, str] = Agent(
            model=selected.model_id,
            deps_type=RunModel,
            capabilities=[ResolveModelId(models.resolve)],
        )
        result = await agent.run("Hello.", deps=selected)
    assert result.output == "Hi."
    assert [body["model"] for body in bodies] == ["anthropic/claude-sonnet-5"]
    assert bodies[0]["providerOptions"] == {"gateway": {"caching": "auto"}}


async def test_session_runner_selects_once_per_drive_and_holds_it_for_followups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = FakeStore()
    slack = FakeSlack()
    style = SlackStyle()
    directory = UserDirectory(slack)
    root = "1700000000.000100"
    event = make_event(ts=root)
    key = SessionKey.from_event(event)
    selected = RunModel("vercel:model-a")
    selection_calls: list[tuple[str, str]] = []
    resolved_model_ids: list[str] = []
    model_calls = 0

    async def select(team: str, channel: str) -> RunModel:
        selection_calls.append((team, channel))
        return selected

    original_get_session = store.get_session

    async def change_model_during_preparation(key: SessionKey) -> Session | None:
        nonlocal selected
        selected = RunModel("vercel:model-b")
        return await original_get_session(key)

    monkeypatch.setattr(store, "get_session", change_model_during_preparation)

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls == 1:
            steer = make_event(
                event_id="Ev-steer",
                ts="1700000000.000200",
                thread_ts=root,
                text="Use the updated detail.",
                mentions_bot=False,
            )
            await store.insert_mail(
                key,
                steer,
                SteerPriority.WHEN_IDLE,
                target_workflow_id=None,
            )
        return ModelResponse(parts=[TextPart(f"answer-{model_calls}")])

    function_model = FunctionModel(model_fn)

    async def resolve(ctx: ModelResolutionContext[RunModel], model_id: str) -> Model:
        assert ctx.deps.model_id == model_id
        resolved_model_ids.append(model_id)
        return function_model

    agent: Agent[RunModel, str] = Agent(
        deps_type=RunModel,
        capabilities=[ResolveModelId(resolve)],
    )
    runner = SessionRunner(
        agent,
        store=store,
        client=slack,
        style=style,
        directory=directory,
        runtime=RuntimeConfig(
            max_followup_runs=1,
            run_timeout=10,
            stall_timeout=10,
        ),
        model_selector=select,
    )

    first_output = await asyncio.wait_for(
        runner.drive(key.model_dump(mode="json"), event.model_dump(mode="json")), timeout=15
    )
    selected = RunModel("vercel:model-b")
    next_event = make_event(
        event_id="Ev-next",
        ts="1700000000.000300",
        thread_ts=root,
        text="Start a new drive.",
    )
    second_output = await asyncio.wait_for(
        runner.drive(key.model_dump(mode="json"), next_event.model_dump(mode="json")), timeout=15
    )

    assert first_output == "answer-2"
    assert second_output == "answer-3"
    assert selection_calls == [(TEAM, event.channel), (TEAM, event.channel)]
    assert resolved_model_ids == ["vercel:model-a", "vercel:model-a", "vercel:model-b"]


@pytest.mark.parametrize(
    ("is_dm", "mentions_bot", "notice_count"),
    [(False, True, 1), (True, False, 1), (False, False, 0)],
)
@pytest.mark.parametrize("has_mail", [False, True])
async def test_unconfigured_agent_explains_setup_and_resumes_after_model_selection(
    is_dm: bool, mentions_bot: bool, notice_count: int, has_mail: bool
) -> None:
    store = FakeStore()
    slack = FakeSlack()
    selected: RunModel | None = None
    model_calls = 0

    async def select(team: str, channel: str) -> RunModel | None:
        return selected

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal model_calls
        model_calls += 1
        return ModelResponse(parts=[TextPart("Ready to help.")])

    async def resolve(ctx: ModelResolutionContext[RunModel], model_id: str) -> Model:
        return FunctionModel(model_fn)

    agent: Agent[RunModel, str] = Agent(
        deps_type=RunModel,
        capabilities=[ResolveModelId(resolve)],
    )
    runner = SessionRunner(
        agent,
        store=store,
        client=slack,
        style=SlackStyle(),
        directory=UserDirectory(slack),
        model_selector=select,
    )
    event = make_event(
        ts="1700000000.000100",
        channel="D01" if is_dm else "C01",
        is_dm=is_dm,
        mentions_bot=mentions_bot,
    )
    key = SessionKey.from_event(event)
    if has_mail:
        await store.insert_mail(key, event, SteerPriority.ASAP, target_workflow_id=None)

    assert await runner.drive(key.model_dump(mode="json"), event.model_dump(mode="json")) == ""
    assert model_calls == 0
    assert len(slack.posted) == notice_count
    if notice_count:
        assert slack.posted[0]["channel"] == key.channel
        assert slack.posted[0]["thread_ts"] == (None if is_dm else key.thread_root)
        assert slack.posted[0]["text"] == (
            "Hondo needs a model before it can respond. "
            "Ask a Hondo admin to select one in Settings. "
            "Then send your request again."
        )
    assert not await store.unconsumed_mail(key)
    if has_mail:
        await runner.drive(key.model_dump(mode="json"), event.model_dump(mode="json"))
        assert len(slack.posted) == notice_count

    selected = RunModel(FIRST_MODEL_ID, uuid4(), uuid4())
    next_event = make_event(
        event_id="Ev-after-setup",
        ts="1700000000.000200",
        channel=key.channel,
        thread_ts=None if is_dm else key.thread_root,
        is_dm=is_dm,
    )

    assert (
        await runner.drive(key.model_dump(mode="json"), next_event.model_dump(mode="json"))
        == "Ready to help."
    )
    assert model_calls == 1
    assert slack.posted[-1]["text"] == "Ready to help."


@pytest.mark.parametrize("failure", ["missing", "other_org", "wrong_crypto"])
async def test_saved_model_credentials_fail_closed_without_default_fallback(
    pg_dsn: str, failure: str
) -> None:
    token_key = generate_key()
    config = make_config(pg_dsn, token_key)
    store = FakeStore()
    workspace_id = uuid4()
    connection_id = uuid4()
    store.agent_models[TEAM] = AgentModel(
        connection_id=connection_id,
        workspace_id=workspace_id,
        provider="vercel",
        model_name=FIRST_MODEL_NAME,
    )
    if failure == "other_org":
        store.provider_keys[(uuid4(), connection_id)] = TokenCipher(token_key).encrypt("vck-key")
    elif failure == "wrong_crypto":
        store.provider_keys[(workspace_id, connection_id)] = TokenCipher(generate_key()).encrypt(
            "vck-key"
        )

    models = RuntimeModels(store, config)
    selected = await models.select(TEAM)
    assert selected is not None
    agent: Agent[RunModel, str] = Agent(deps_type=RunModel)
    context = ModelResolutionContext(agent=agent, deps=selected)
    expected_error: type[Exception] = TokenCipherError if failure == "wrong_crypto" else ValueError
    expected_message = "token decryption failed" if failure == "wrong_crypto" else "unavailable"

    assert selected.model_id == FIRST_MODEL_ID
    with pytest.raises(expected_error, match=expected_message):
        await models.resolve(context, selected.model_id)


async def test_unconfigured_agent_ignores_environment_provider_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HONDO_PROVIDERS__PROVIDER", "vercel")
    monkeypatch.setenv("HONDO_PROVIDERS__VERCEL__API_KEY", "unused-environment-key")
    monkeypatch.setenv("HONDO_WORKSPACE__MODEL", FIRST_MODEL_NAME)
    monkeypatch.setenv("VERCEL_AI_GATEWAY_API_KEY", "unused-sdk-environment-key")
    monkeypatch.setenv("HONDO_DATABASE__URL", "postgresql://test:test@localhost/test")
    monkeypatch.setenv("HONDO_CRYPTO__TOKEN_KEY", generate_key())
    monkeypatch.setenv("HONDO_WORKSPACE__SLACK__APP_TOKEN", "xapp-test")
    config = Config(_env_file=None)  # pyright: ignore[reportCallIssue]
    models = RuntimeModels(FakeStore(), config)

    assert await models.select(TEAM) is None


@pytest.mark.parametrize(
    ("provider", "model_name", "path", "header", "first_credential", "second_credential"),
    [
        (
            "openai",
            "gpt-5.2",
            "/v1/responses",
            "authorization",
            "Bearer sk-first",
            "Bearer sk-second",
        ),
        (
            "anthropic",
            "claude-sonnet-4-6",
            "/v1/messages",
            "authorization",
            "Bearer sk-first",
            "Bearer sk-second",
        ),
    ],
)
async def test_native_provider_calls_tools_with_rotated_organization_key(
    pg_dsn: str,
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
    model_name: str,
    path: str,
    header: str,
    first_credential: str,
    second_credential: str,
) -> None:
    from pydantic_ai.providers.anthropic import AnthropicProvider
    from pydantic_ai.providers.openai import OpenAIProvider

    token_key = generate_key()
    cipher = TokenCipher(token_key)
    store = FakeStore()
    workspace_id, connection_id = uuid4(), uuid4()
    store.agent_models[TEAM] = AgentModel(connection_id, workspace_id, provider, model_name)
    store.provider_keys[(workspace_id, connection_id)] = cipher.encrypt("sk-first")
    models = RuntimeModels(store, make_config(pg_dsn, token_key))
    selected = await models.select(TEAM)
    assert selected is not None
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["model"] == model_name
        if provider == "openai":
            assert str(request.url) == "https://api.openai.com/v1/responses"
            assert any(tool["name"] == "rotate_key" for tool in payload["tools"])
            output = (
                [
                    {
                        "type": "function_call",
                        "id": "fc_rotate",
                        "call_id": "call_rotate",
                        "name": "rotate_key",
                        "arguments": "{}",
                        "status": "completed",
                    }
                ]
                if len(requests) == 1
                else [
                    {
                        "type": "message",
                        "id": "msg_done",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "Done.", "annotations": []}],
                    }
                ]
            )
            if len(requests) == 2:
                assert any(item.get("type") == "function_call_output" for item in payload["input"])
            return httpx.Response(
                200,
                json={
                    "id": f"resp_{len(requests)}",
                    "object": "response",
                    "created_at": 0,
                    "model": model_name,
                    "status": "completed",
                    "output": output,
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            )
        assert request.url.host == "api.anthropic.com"
        assert request.url.path == "/v1/messages"
        assert any(tool["name"] == "rotate_key" for tool in payload["tools"])
        assert payload["stream"] is True
        content_block = (
            {"type": "tool_use", "id": "tool_rotate", "name": "rotate_key", "input": {}}
            if len(requests) == 1
            else {"type": "text", "text": ""}
        )
        delta = (
            {"type": "input_json_delta", "partial_json": "{}"}
            if len(requests) == 1
            else {"type": "text_delta", "text": "Done."}
        )
        if len(requests) == 2:
            assert payload["messages"][-1]["content"][0]["type"] == "tool_result"
        events = [
            {
                "type": "message_start",
                "message": {
                    "id": f"msg_{len(requests)}",
                    "type": "message",
                    "role": "assistant",
                    "model": model_name,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
            {"type": "content_block_start", "index": 0, "content_block": content_block},
            {"type": "content_block_delta", "index": 0, "delta": delta},
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": "tool_use" if len(requests) == 1 else "end_turn",
                    "stop_sequence": None,
                },
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
            ),
        )

    monkeypatch.setenv("OPENAI_API_KEY", "wrong-environment-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "wrong-environment-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://wrong-endpoint.invalid")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://wrong-endpoint.invalid")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http_client:

        def openai_provider(*, api_key: str, base_url: str) -> OpenAIProvider:
            return OpenAIProvider(api_key=api_key, base_url=base_url, http_client=http_client)

        def anthropic_provider(*, api_key: str, base_url: str) -> AnthropicProvider:
            return AnthropicProvider(api_key=api_key, base_url=base_url, http_client=http_client)

        monkeypatch.setattr(model_selection, "OpenAIProvider", openai_provider)
        monkeypatch.setattr(model_selection, "AnthropicProvider", anthropic_provider)
        suffix = uuid4().hex
        agent: Agent[RunModel, str] = Agent(
            model=selected.model_id,
            deps_type=RunModel,
            name="native_provider_test",
            capabilities=[
                ResolveModelId(models.resolve),
                DBOSDurability(
                    name=f"native_provider_{suffix}", parallel_execution_mode="sequential"
                ),
            ],
        )

        @agent.tool_plain
        def rotate_key() -> str:
            store.provider_keys[(workspace_id, connection_id)] = cipher.encrypt("sk-second")
            store.agent_models[TEAM] = AgentModel(
                connection_id, workspace_id, provider, "new-model"
            )
            return "Key rotated."

        DBOS(
            config={
                "name": "native-provider-test",
                "system_database_url": pg_dsn,
                "run_admin_server": False,
                "dbos_system_schema": f"hondo_native_{suffix}",
                "log_level": "WARNING",
            }
        )  # type: ignore[arg-type]

        @DBOS.workflow(name=f"test.native_provider.{suffix}")
        async def run_model() -> str:
            result = await agent.run(
                "Rotate the key, then answer.", model=selected.model_id, deps=selected
            )
            return result.output

        try:
            DBOS.launch()
            with SetWorkflowID(f"native-provider-{suffix}"):
                output = await asyncio.wait_for(run_model(), timeout=15)
            assert output == "Done."
        finally:
            DBOS.destroy()
        assert [(request.url.path, request.headers[header]) for request in requests] == [
            (path, first_credential),
            (path, second_credential),
        ]
        assert selected.model_id == f"{provider}:{model_name}"
        next_selection = await models.select(TEAM)
        assert next_selection is not None
        assert next_selection.model_id == f"{provider}:new-model"
