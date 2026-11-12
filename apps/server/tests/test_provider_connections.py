from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from dbos import DBOS, SetWorkflowID
from fastapi import FastAPI, Request

from hondo.api.provider_connections import ProviderConnections
from hondo.api.routers.providers import provider_connections
from hondo.api.store import ControlStore
from hondo.crypto import TokenCipher, generate_key
from hondo.models import SessionKey
from hondo.runtime.durable import run_workflow_id, summary_workflow_id
from hondo.store.postgres import PostgresStore
from tests.test_auth import (
    VERIFIED_JANE,
    FakeOIDCProvider,
    _app,
    _client,
    slack_account,
)
from tests.test_workspaces import _insert_workspace, _sign_in, _sign_in_as_admin


class Gateway:
    def __init__(self) -> None:
        self.status = 200
        self.payload: Any = {"balance": "0", "total_used": "10.50"}
        self.error: httpx.HTTPError | None = None
        self.keys: list[str] = []
        self.catalog: Any = {
            "data": [
                {
                    "id": "xai/grok-4.5",
                    "name": "Grok 4.5",
                    "type": "language",
                    "tags": ["tool-use", "vision"],
                },
                {
                    "id": "openai/gpt-4.1",
                    "name": "GPT 4.1",
                    "type": "language",
                    "tags": ["tool-use"],
                },
                {"id": "image/example", "name": "Image", "type": "image", "tags": ["tool-use"]},
                {"id": "text/example", "name": "Text only", "type": "language", "tags": []},
            ]
        }
        self.catalog_calls = 0
        self.requests: list[httpx.Request] = []
        self.openai_catalog: Any = {
            "data": [
                {"id": "gpt-4.1"},
                {"id": "gpt-5.2"},
                {"id": "gpt-6.1-sol"},
                {"id": "o3-mini"},
                {"id": "text-embedding-3-small"},
                {"id": "gpt-image-1"},
                {"id": "gpt-4o-audio-preview"},
                {"id": "gpt-4o-realtime-preview"},
                {"id": "gpt-4o-mini-transcribe"},
                {"id": "gpt-4o-mini-tts"},
                {"id": "gpt-4o-search-preview"},
                {"id": "gpt-5.2-chat-latest"},
                {"id": "o1-mini"},
                {"id": "o1-preview"},
                {"id": "o3-deep-research"},
            ]
        }
        self.anthropic_catalog: Any = {
            "data": [{"id": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6"}],
            "has_more": False,
            "last_id": "claude-sonnet-4-6",
        }
        self.anthropic_next_page: Any = None
        self.catalogs_by_key: dict[str, Any] = {}

    def handle(self, request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        self.requests.append(request)
        if request.url.host in ("api.openai.com", "api.anthropic.com"):
            assert request.url.path == "/v1/models"
            if request.url.host == "api.openai.com":
                assert "x-api-key" not in request.headers
                self.keys.append(request.headers["Authorization"])
                payload = self.openai_catalog
            else:
                assert "Authorization" not in request.headers
                assert request.headers["anthropic-version"] == "2023-06-01"
                self.keys.append(request.headers["x-api-key"])
                payload = (
                    self.anthropic_next_page
                    if "after_id" in request.url.params
                    else self.anthropic_catalog
                )
            if self.error:
                raise self.error
            return httpx.Response(
                self.status, json=self.catalogs_by_key.get(self.keys[-1], payload)
            )
        if request.url.path == "/v1/models":
            assert "Authorization" not in request.headers
            self.catalog_calls += 1
            return httpx.Response(self.status, json=self.catalog)
        assert str(request.url) == "https://ai-gateway.vercel.sh/v1/credits"
        self.keys.append(request.headers["Authorization"])
        if self.error:
            raise self.error
        return httpx.Response(self.status, json=self.payload)


@pytest.fixture
def gateway() -> Gateway:
    return Gateway()


@pytest.fixture
def cipher() -> TokenCipher:
    return TokenCipher(generate_key())


@pytest.fixture
async def provider_app(
    control_store: ControlStore, pg_dsn: str, gateway: Gateway, cipher: TokenCipher
) -> AsyncIterator[FastAPI]:
    app = _app(control_store, pg_dsn, FakeOIDCProvider(identity=VERIFIED_JANE))
    async with httpx.AsyncClient(transport=httpx.MockTransport(gateway.handle)) as client:

        def service(request: Request) -> ProviderConnections:
            result = provider_connections(request)
            result.cipher = cipher
            result.client = client
            return result

        app.dependency_overrides[provider_connections] = service
        yield app


async def _add(
    client: httpx.AsyncClient,
    workspace_id: UUID,
    *,
    provider: str = "vercel",
    name: str = "Production",
    key: str = "vck-secret",
) -> str:
    response = await client.post(
        f"/api/workspaces/{workspace_id}/provider-connections",
        json={"provider": provider, "name": name, "api_key": key},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"id", "name", "provider", "check_status", "checked_at", "updated_at"}
    assert body["name"] == name.strip()
    assert body["provider"] == provider
    assert body["check_status"] == "valid"
    assert key.strip() not in response.text
    return body["id"]


@pytest.mark.parametrize("provider", ["vercel", "openai", "anthropic"])
async def test_add_rename_replace_check_and_remove_connection(
    provider_app: FastAPI,
    control_store: ControlStore,
    cipher: TokenCipher,
    gateway: Gateway,
    provider: str,
) -> None:
    async with _client(provider_app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        catalog = await client.get(f"/api/workspaces/{workspace_id}/providers")
        assert catalog.status_code == 200
        assert catalog.json()["connections"] == []
        assert [(item["id"], item["name"]) for item in catalog.json()["providers"]] == [
            ("vercel", "Vercel AI Gateway"),
            ("openai", "OpenAI"),
            ("anthropic", "Anthropic"),
        ]
        connection_id = await _add(
            client, workspace_id, provider=provider, name="  Production  ", key="  first-secret  "
        )
        path = f"/api/workspaces/{workspace_id}/provider-connections/{connection_id}"
        original = (await control_store.list_provider_connections(workspace_id))[0]
        assert original.id == UUID(connection_id)
        assert cipher.decrypt(original.api_key_encrypted) == "first-secret"
        assert "first-secret" not in repr(original)
        assert original.api_key_encrypted != "first-secret"
        requests = len(gateway.requests)
        renamed = await client.patch(path, json={"name": " Team key "})
        assert renamed.status_code == 200
        assert renamed.json()["id"] == connection_id
        assert renamed.json()["name"] == "Team key"
        saved = (await control_store.list_provider_connections(workspace_id))[0]
        assert saved.api_key_encrypted == original.api_key_encrypted
        assert saved.checked_at == original.checked_at
        assert len(gateway.requests) == requests
        assert (await client.post(path + "/check")).status_code == 204
        replaced = await client.patch(path, json={"name": "Team key", "api_key": "second-secret"})
        assert replaced.status_code == 200
        assert replaced.json()["id"] == connection_id
        saved = (await control_store.list_provider_connections(workspace_id))[0]
        assert cipher.decrypt(saved.api_key_encrypted) == "second-secret"
        listed = await client.get(f"/api/workspaces/{workspace_id}/providers")
        assert len(listed.json()["connections"]) == 1
        assert "secret" not in listed.text
        assert saved.api_key_encrypted not in listed.text
        assert gateway.keys == (
            ["first-secret", "first-secret", "second-secret"]
            if provider == "anthropic"
            else ["Bearer first-secret", "Bearer first-secret", "Bearer second-secret"]
        )
        removed = await client.delete(path)
        assert removed.status_code == 204 and not removed.content
        assert await control_store.list_provider_connections(workspace_id) == []
        assert (await client.delete(path)).status_code == 204
        missing = await client.post(path + "/check")
        assert missing.status_code == 404
        assert missing.json() == {"detail": "connection_not_found"}


@pytest.mark.parametrize("length", [4097, 65_536])
async def test_long_provider_key_survives_storage(
    provider_app: FastAPI,
    control_store: ControlStore,
    cipher: TokenCipher,
    gateway: Gateway,
    length: int,
) -> None:
    key = "A" * (length - 8) + "tail1234"
    async with _client(provider_app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        await _add(client, workspace_id, key=key)
        assert gateway.keys == [f"Bearer {key}"]
        saved = (await control_store.list_provider_connections(workspace_id))[0]
        assert cipher.decrypt(saved.api_key_encrypted) == key
        listed = await client.get(f"/api/workspaces/{workspace_id}/providers")
        assert key not in listed.text
        assert saved.api_key_encrypted not in listed.text


@pytest.mark.parametrize(
    "provider,model,expected_models,reads_images",
    [
        (
            "vercel",
            "xai/grok-4.5",
            [
                {"id": "openai/gpt-4.1", "name": "GPT 4.1", "reads_images": False},
                {"id": "xai/grok-4.5", "name": "Grok 4.5", "reads_images": True},
            ],
            True,
        ),
        (
            "vercel",
            "openai/gpt-4.1",
            [
                {"id": "openai/gpt-4.1", "name": "GPT 4.1", "reads_images": False},
                {"id": "xai/grok-4.5", "name": "Grok 4.5", "reads_images": True},
            ],
            False,
        ),
        (
            "openai",
            "gpt-5.2",
            [
                {"id": "gpt-4.1", "name": "gpt-4.1", "reads_images": True},
                {"id": "gpt-5.2", "name": "gpt-5.2", "reads_images": True},
                {"id": "gpt-6.1-sol", "name": "gpt-6.1-sol", "reads_images": True},
                {"id": "o3-mini", "name": "o3-mini", "reads_images": True},
            ],
            True,
        ),
        (
            "anthropic",
            "claude-sonnet-4-6",
            [
                {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "reads_images": True},
            ],
            True,
        ),
    ],
)
async def test_model_catalog_selection_remove_and_runtime_read(
    provider_app: FastAPI,
    control_store: ControlStore,
    cipher: TokenCipher,
    pg_dsn: str,
    provider: str,
    model: str,
    expected_models: list[dict[str, object]],
    reads_images: bool,
) -> None:
    async with _client(provider_app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        second_workspace = await _insert_workspace(control_store, team="T2", name="Second")
        path = f"/api/workspaces/{workspace_id}/agent/model"
        unconfigured = {
            "source": "unconfigured",
            "ready": False,
            "connection_id": None,
            "provider": None,
            "model_name": None,
        }
        assert (await client.get(path)).json() == unconfigured
        missing = await client.put(path, json={"connection_id": str(uuid4()), "model_name": model})
        assert missing.status_code == 404
        assert missing.json() == {"detail": "connection_not_found"}
        connection_id = await _add(client, workspace_id, provider=provider, key="model-secret")
        connection_path = f"/api/workspaces/{workspace_id}/provider-connections/{connection_id}"
        catalog = await client.get(connection_path + "/models")
        assert catalog.status_code == 200
        assert catalog.json() == {"models": expected_models}
        body = {"connection_id": connection_id, "model_name": model}
        saved = await client.put(path, json={**body, "model_name": f"  {model}  "})
        assert saved.status_code == 204 and not saved.content
        assert (await client.get(path)).json() == {
            "source": "connection",
            "ready": True,
            "provider": provider,
            **body,
        }
        assert await control_store.get_agent_model(second_workspace) is None
        in_use = await client.delete(connection_path)
        assert in_use.status_code == 409
        assert in_use.json() == {"detail": "connection_in_use"}
        selection = await control_store.get_agent_model(workspace_id)
        assert selection is not None and selection.reads_images is reads_images
        runtime = PostgresStore(pg_dsn)
        await runtime.connect(run_migrations=False, pool_min_size=1, pool_max_size=1)
        try:
            assert await runtime.get_agent_model("T1") == selection
            encrypted = await runtime.get_provider_key(workspace_id, selection.connection_id)
            assert encrypted is not None and cipher.decrypt(encrypted) == "model-secret"
            assert await runtime.get_provider_key(uuid4(), selection.connection_id) is None
            assert await runtime.get_provider_key(second_workspace, selection.connection_id) is None
            assert (
                await client.patch(
                    connection_path, json={"name": "Renamed", "api_key": "rotated-key"}
                )
            ).status_code == 200
            assert await runtime.get_agent_model("T1") == selection
            encrypted = await runtime.get_provider_key(workspace_id, selection.connection_id)
            assert encrypted is not None and cipher.decrypt(encrypted) == "rotated-key"
            removed = await client.delete(path)
            assert removed.status_code == 204 and not removed.content
            assert (await client.get(path)).json() == unconfigured
            assert await runtime.get_agent_model("T1") is None
            assert (await client.delete(path)).status_code == 204
            assert (await client.delete(connection_path)).status_code == 204
        finally:
            await runtime.aclose()


async def test_two_connections_to_same_provider_keep_keys_and_models_separate(
    provider_app: FastAPI,
    control_store: ControlStore,
    cipher: TokenCipher,
    gateway: Gateway,
) -> None:
    gateway.catalogs_by_key = {
        "Bearer key-production": {"data": [{"id": "gpt-5.2"}]},
        "Bearer key-development": {"data": [{"id": "gpt-4.1"}]},
    }
    async with _client(provider_app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        first = await _add(
            client, workspace_id, provider="openai", name="Production", key="key-production"
        )
        second = await _add(
            client, workspace_id, provider="openai", name="Development", key="key-development"
        )
        assert first != second
        base = f"/api/workspaces/{workspace_id}/provider-connections"
        assert (await client.get(f"{base}/{first}/models")).json() == {
            "models": [{"id": "gpt-5.2", "name": "gpt-5.2", "reads_images": True}]
        }
        assert (await client.get(f"{base}/{second}/models")).json() == {
            "models": [{"id": "gpt-4.1", "name": "gpt-4.1", "reads_images": True}]
        }
        model_path = f"/api/workspaces/{workspace_id}/agent/model"
        unavailable = await client.put(
            model_path, json={"connection_id": second, "model_name": "gpt-5.2"}
        )
        assert unavailable.status_code == 400
        assert unavailable.json() == {"detail": "model_unavailable"}
        assert (
            await client.put(model_path, json={"connection_id": first, "model_name": "gpt-5.2"})
        ).status_code == 204
        assert (
            await client.put(model_path, json={"connection_id": second, "model_name": "gpt-4.1"})
        ).status_code == 204
        assert (await client.get(model_path)).json() == {
            "source": "connection",
            "ready": True,
            "connection_id": second,
            "provider": "openai",
            "model_name": "gpt-4.1",
        }
        gateway.status = 401
        assert (await client.post(f"{base}/{first}/check")).status_code == 400
        rejected = await control_store.get_provider_connection(workspace_id, UUID(first))
        assert rejected is not None and rejected.check_status == "rejected"
        assert (
            await client.patch(f"{base}/{first}", json={"name": "Old production"})
        ).status_code == 200
        renamed = await control_store.get_provider_connection(workspace_id, UUID(first))
        assert renamed is not None and renamed.check_status == "rejected"
        assert renamed.checked_at == rejected.checked_at
        sibling = await control_store.get_provider_connection(workspace_id, UUID(second))
        assert sibling is not None and sibling.check_status == "valid"
        assert cipher.decrypt(sibling.api_key_encrypted) == "key-development"
        assert (await client.delete(f"{base}/{first}")).status_code == 204
        assert (await client.delete(f"{base}/{second}")).status_code == 409


async def test_connection_names_are_unique_per_workspace_and_provider_cannot_change(
    provider_app: FastAPI,
    control_store: ControlStore,
) -> None:
    async with _client(provider_app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        first = await _add(client, workspace_id, name="Production")
        second = await _add(client, workspace_id, name="Development")
        base = f"/api/workspaces/{workspace_id}/provider-connections"
        duplicate = await client.post(
            base, json={"provider": "openai", "name": " production ", "api_key": "sk-key"}
        )
        assert duplicate.status_code == 409
        assert duplicate.json() == {"detail": "connection_name_taken"}
        duplicate = await client.patch(f"{base}/{second}", json={"name": "PRODUCTION"})
        assert duplicate.status_code == 409
        assert duplicate.json() == {"detail": "connection_name_taken"}
        changed = await client.patch(
            f"{base}/{first}", json={"name": "Production", "provider": "openai"}
        )
        assert changed.status_code == 422
        assert changed.json() == {"detail": "invalid_request"}
        connections = (await client.get(f"/api/workspaces/{workspace_id}/providers")).json()[
            "connections"
        ]
        assert [item["name"] for item in connections] == ["Development", "Production"]
        other_workspace = await _insert_workspace(control_store, team="T2", name="Other")
        other = await control_store.create_provider_connection(
            other_workspace, "vercel", "Production", "encrypted"
        )
        assert other.name == "Production"


@pytest.mark.parametrize("model", ["image/example", "text/example", "missing/model"])
async def test_non_tool_and_unknown_models_are_rejected(
    provider_app: FastAPI,
    control_store: ControlStore,
    model: str,
) -> None:
    async with _client(provider_app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        connection_id = await _add(client, workspace_id)
        result = await client.put(
            f"/api/workspaces/{workspace_id}/agent/model",
            json={"connection_id": connection_id, "model_name": model},
        )
        assert result.status_code == 400
        assert result.json() == {"detail": "model_unavailable"}
        assert await control_store.get_agent_model(workspace_id) is None


async def test_model_catalog_failure_and_rejected_key_preserve_selection(
    provider_app: FastAPI,
    control_store: ControlStore,
    gateway: Gateway,
) -> None:
    async with _client(provider_app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        path = f"/api/workspaces/{workspace_id}/agent/model"
        connection_id = await _add(client, workspace_id)
        connection_path = f"/api/workspaces/{workspace_id}/provider-connections/{connection_id}"
        body = {"connection_id": connection_id, "model_name": "xai/grok-4.5"}
        assert (await client.put(path, json=body)).status_code == 204
        for status, payload in [(503, {"error": "sensitive detail"}), (200, {"data": [{}]})]:
            gateway.status, gateway.catalog = status, payload
            result = await client.get(connection_path + "/models")
            assert result.status_code == 503
            assert result.json() == {"detail": "models_unavailable"}
            result = await client.put(path, json=body)
            assert result.status_code == 503
            assert result.json() == {"detail": "models_unavailable"}
            assert (await client.get(path)).json() == {
                "source": "connection",
                "ready": True,
                "provider": "vercel",
                **body,
            }
        gateway.status = 401
        assert (await client.post(connection_path + "/check")).status_code == 400
        assert (await client.get(path)).json()["ready"] is False
        gateway.status, gateway.catalog = 200, Gateway().catalog
        result = await client.put(path, json=body)
        assert result.status_code == 409
        assert result.json() == {"detail": "connection_required"}


@pytest.mark.parametrize("provider", ["vercel", "openai", "anthropic"])
@pytest.mark.parametrize(
    "status,expected_status,detail",
    [
        (401, 400, "key_rejected"),
        (403, 400, "key_rejected"),
        (429, 503, "check_unavailable"),
        (500, 503, "check_unavailable"),
        (302, 503, "check_unavailable"),
    ],
)
async def test_failed_replacement_preserves_saved_key_and_recheck_records_only_rejection(
    provider_app: FastAPI,
    control_store: ControlStore,
    gateway: Gateway,
    provider: str,
    status: int,
    expected_status: int,
    detail: str,
) -> None:
    async with _client(provider_app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        connection_id = await _add(client, workspace_id, provider=provider)
        path = f"/api/workspaces/{workspace_id}/provider-connections/{connection_id}"
        original = (await control_store.list_provider_connections(workspace_id))[0]
        gateway.status = status
        gateway.payload = {"error": "private-key-must-not-leak"}
        result = await client.patch(
            path, json={"name": "Changed", "api_key": "private-key-must-not-leak"}
        )
        assert result.status_code == expected_status
        assert result.json() == {"detail": detail}
        assert (await control_store.list_provider_connections(workspace_id))[0] == original
        checked = await client.post(path + "/check")
        assert checked.status_code == expected_status
        assert checked.json() == {"detail": detail}
        saved = (await control_store.list_provider_connections(workspace_id))[0]
        if status in (401, 403):
            assert saved.check_status == "rejected"
            catalog = await client.get(path + "/models")
            assert catalog.status_code == 409
            assert catalog.json() == {"detail": "connection_required"}
        else:
            assert saved == original
            catalog = await client.get(path + "/models")
            assert catalog.status_code == 503
            assert catalog.json() == {"detail": "models_unavailable"}
        gateway.status, gateway.payload = 200, Gateway().payload
        assert (await client.post(path + "/check")).status_code == 204
        assert (await control_store.list_provider_connections(workspace_id))[
            0
        ].check_status == "valid"


@pytest.mark.parametrize("provider", ["vercel", "openai", "anthropic"])
@pytest.mark.parametrize("failure", ["payload", "timeout"])
async def test_invalid_responses_do_not_change_saved_key(
    provider_app: FastAPI,
    control_store: ControlStore,
    gateway: Gateway,
    provider: str,
    failure: str,
) -> None:
    async with _client(provider_app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        connection_id = await _add(client, workspace_id, provider=provider)
        path = f"/api/workspaces/{workspace_id}/provider-connections/{connection_id}"
        original = (await control_store.list_provider_connections(workspace_id))[0]
        if failure == "payload":
            gateway.payload = gateway.catalog = gateway.openai_catalog = (
                gateway.anthropic_catalog
            ) = {"data": [{}]}
        else:
            gateway.error = httpx.ReadTimeout("private transport details")
        checked = await client.post(path + "/check")
        assert checked.status_code == 503
        assert checked.json() == {"detail": "check_unavailable"}
        result = await client.post(
            f"/api/workspaces/{workspace_id}/provider-connections",
            json={"provider": provider, "name": "New", "api_key": "private-key"},
        )
        assert result.status_code == 503
        assert result.json() == {"detail": "check_unavailable"}
        if provider != "vercel" or failure == "payload":
            catalog = await client.get(path + "/models")
            assert catalog.status_code == 503
            assert catalog.json() == {"detail": "models_unavailable"}
        assert (await control_store.list_provider_connections(workspace_id)) == [original]


@pytest.mark.parametrize(
    "body",
    [
        {"api_key": ""},
        {"api_key": "   "},
        {"api_key": "vck-secret\nheader"},
        {"api_key": {"secret": "vck-test"}},
        {"api_key": "x" * 65_537},
        {"api_key": "vck-🔑"},
        {"api_key": "vck-\x00secret"},
        {"name": ""},
        {"name": "   "},
        {"name": "x" * 101},
        {"provider": "unsupported"},
    ],
)
async def test_invalid_input_never_echoes_credentials(
    provider_app: FastAPI,
    control_store: ControlStore,
    gateway: Gateway,
    body: Any,
) -> None:
    async with _client(provider_app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        response = await client.post(
            f"/api/workspaces/{workspace_id}/provider-connections",
            json={"provider": "vercel", "name": "Production", "api_key": "vck-test", **body},
        )
        assert response.status_code == 422
        assert response.json() == {"detail": "invalid_request"}
        assert gateway.keys == []


async def test_routes_are_admin_only_and_workspace_isolated(
    provider_app: FastAPI,
    control_store: ControlStore,
    cipher: TokenCipher,
    gateway: Gateway,
) -> None:
    async with _client(provider_app) as client:
        fake_id = uuid4()
        endpoints = [
            ("GET", "/providers"),
            ("POST", "/provider-connections"),
            ("PATCH", f"/provider-connections/{fake_id}"),
            ("GET", f"/provider-connections/{fake_id}/models"),
            ("POST", f"/provider-connections/{fake_id}/check"),
            ("DELETE", f"/provider-connections/{fake_id}"),
            ("GET", "/agent/model"),
            ("PUT", "/agent/model"),
            ("DELETE", "/agent/model"),
        ]
        for method, suffix in endpoints:
            response = await client.request(method, f"/api/workspaces/{uuid4()}{suffix}", json={})
            assert response.status_code == 401
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        other_workspace = await _insert_workspace(control_store, team="T2", name="Other")
        for target in (other_workspace, uuid4()):
            for method, suffix in endpoints:
                response = await client.request(
                    method, f"/api/workspaces/{target}{suffix}", json={}
                )
                assert response.status_code == 404
                assert response.json() == {"detail": "not_found"}
        other = await control_store.create_provider_connection(
            other_workspace, "openai", "Production", cipher.encrypt("sk-other")
        )
        path = f"/api/workspaces/{workspace_id}/provider-connections/{other.id}"
        for method, suffix, body in [
            ("GET", "/models", {}),
            ("POST", "/check", {}),
            ("PATCH", "", {"name": "Stolen", "api_key": "key"}),
        ]:
            response = await client.request(method, path + suffix, json=body)
            assert response.status_code == 404
            assert response.json() == {"detail": "connection_not_found"}
        assert (await client.delete(path)).status_code == 204
        assert await control_store.get_provider_connection(other_workspace, other.id) == other
        model_path = f"/api/workspaces/{workspace_id}/agent/model"
        response = await client.put(
            model_path, json={"connection_id": str(other.id), "model_name": "gpt-4.1"}
        )
        assert response.status_code == 404
        assert response.json() == {"detail": "connection_not_found"}

        provider_app.state.sign_in.identity = slack_account("U2")
        await _sign_in(client)
        for method, suffix in endpoints:
            response = await client.request(
                method, f"/api/workspaces/{workspace_id}{suffix}", json={}
            )
            assert response.status_code == 404
            assert response.json() == {"detail": "not_found"}
        assert gateway.requests == []


async def test_old_check_cannot_change_replacement_or_another_connection(
    provider_app: FastAPI,
    control_store: ControlStore,
    cipher: TokenCipher,
) -> None:
    async with _client(provider_app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        connection_id = await _add(client, workspace_id, key="vck-first")
        path = f"/api/workspaces/{workspace_id}/provider-connections/{connection_id}"
        old = (await control_store.list_provider_connections(workspace_id))[0]
        twin = await control_store.create_provider_connection(
            workspace_id, "vercel", "Twin", old.api_key_encrypted
        )
        assert (
            await client.patch(path, json={"name": "Production", "api_key": "vck-second"})
        ).status_code == 200
        await control_store.record_provider_check(workspace_id, old, "rejected")
        saved = await control_store.get_provider_connection(workspace_id, old.id)
        assert saved is not None and saved.check_status == "valid"
        assert cipher.decrypt(saved.api_key_encrypted) == "vck-second"
        assert await control_store.get_provider_connection(workspace_id, twin.id) == twin
        assert (await client.delete(path)).status_code == 204
        await control_store.record_provider_check(workspace_id, saved, "valid")
        assert await control_store.get_provider_connection(workspace_id, old.id) is None


async def test_anthropic_catalog_reads_all_pages(
    provider_app: FastAPI,
    control_store: ControlStore,
    gateway: Gateway,
) -> None:
    async with _client(provider_app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        connection_id = await _add(client, workspace_id, provider="anthropic")
        path = f"/api/workspaces/{workspace_id}/provider-connections/{connection_id}/models"
        gateway.anthropic_catalog["has_more"] = True
        gateway.anthropic_next_page = {
            "data": [{"id": "claude-haiku-4-5", "display_name": "Claude Haiku 4.5"}],
            "has_more": False,
            "last_id": "claude-haiku-4-5",
        }
        catalog = await client.get(path)
        assert catalog.status_code == 200
        assert catalog.json() == {
            "models": [
                {"id": "claude-haiku-4-5", "name": "Claude Haiku 4.5", "reads_images": True},
                {"id": "claude-sonnet-4-6", "name": "Claude Sonnet 4.6", "reads_images": True},
            ]
        }
        assert gateway.requests[-1].url.params["after_id"] == "claude-sonnet-4-6"
        gateway.anthropic_next_page = gateway.anthropic_catalog
        invalid = await client.get(path)
        assert invalid.status_code == 503
        assert invalid.json() == {"detail": "models_unavailable"}


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("status", [401, 403])
async def test_native_catalog_records_revoked_keys(
    provider_app: FastAPI,
    control_store: ControlStore,
    gateway: Gateway,
    provider: str,
    status: int,
) -> None:
    async with _client(provider_app) as client:
        _, workspace_id = await _sign_in_as_admin(client, control_store)
        connection_id = await _add(client, workspace_id, provider=provider)
        original = (await control_store.list_provider_connections(workspace_id))[0]
        gateway.status = status
        result = await client.get(
            f"/api/workspaces/{workspace_id}/provider-connections/{connection_id}/models"
        )
        assert result.status_code == 409
        assert result.json() == {"detail": "connection_required"}
        saved = (await control_store.list_provider_connections(workspace_id))[0]
        assert saved.check_status == "rejected"
        assert saved.api_key_encrypted == original.api_key_encrypted


@pytest.mark.parametrize("workflow_id", [run_workflow_id, summary_workflow_id])
async def test_reset_cannot_disconnect_a_key_used_by_an_active_workflow(
    provider_app: FastAPI,
    control_store: ControlStore,
    pg_dsn: str,
    cipher: TokenCipher,
    workflow_id: Any,
) -> None:
    entered, finish = asyncio.Event(), asyncio.Event()
    DBOS(
        config={
            "name": "disconnect-test",
            "system_database_url": pg_dsn,
            "run_admin_server": False,
            "dbos_system_schema": "hondo_dbos",
            "log_level": "WARNING",
        }
    )

    @DBOS.workflow(name="test.hold_provider_connection")
    async def hold_connection() -> None:
        entered.set()
        await finish.wait()

    try:
        DBOS.launch()
        async with _client(provider_app) as client:
            _, workspace_id = await _sign_in_as_admin(client, control_store)
            connection_id = await _add(client, workspace_id, key="vck-active-run")
            provider_path = f"/api/workspaces/{workspace_id}/provider-connections/{connection_id}"
            model_path = f"/api/workspaces/{workspace_id}/agent/model"
            assert (
                await client.put(
                    model_path, json={"connection_id": connection_id, "model_name": "xai/grok-4.5"}
                )
            ).status_code == 204
            key = SessionKey(team="T1", channel="C01", thread_root="thread")
            with SetWorkflowID(workflow_id(key, str(uuid4()))):
                handle = await DBOS.start_workflow_async(hold_connection)
            await asyncio.wait_for(entered.wait(), timeout=10)
            assert (await client.delete(model_path)).status_code == 204
            blocked = await client.delete(provider_path)
            assert blocked.status_code == 409
            assert blocked.json() == {"detail": "connection_in_use"}
            connection = (await control_store.list_provider_connections(workspace_id))[0]
            assert cipher.decrypt(connection.api_key_encrypted) == "vck-active-run"

            other_workspace = await _insert_workspace(control_store, team="T2", name="Other")
            other = await control_store.create_provider_connection(
                other_workspace, "vercel", "Other", cipher.encrypt("other")
            )
            assert await control_store.delete_provider_connection(other_workspace, other.id)

            finish.set()
            await asyncio.wait_for(handle.get_result(), timeout=10)
            assert (await client.delete(provider_path)).status_code == 204
            assert await control_store.list_provider_connections(workspace_id) == []
    finally:
        finish.set()
        DBOS.destroy(destroy_registry=True)
