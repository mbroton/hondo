from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

import httpx
import structlog
from pydantic import BaseModel, Field, SecretStr

from hondo.agent_settings import AgentModel
from hondo.api.store import ControlStore, ProviderConnection, ProviderConnectionNameTaken
from hondo.crypto import TokenCipher, TokenCipherError

logger = structlog.get_logger("hondo.api.provider_connections")

type ProviderId = Literal["vercel", "openai", "anthropic"]
type ConnectionErrorCode = Literal[
    "key_rejected",
    "check_unavailable",
    "connection_not_found",
    "connections_unavailable",
    "connection_in_use",
    "connection_required",
    "models_unavailable",
    "model_unavailable",
    "disconnect_unavailable",
    "connection_name_taken",
]


@dataclass(frozen=True, slots=True)
class ProviderDefinition:
    id: ProviderId
    name: str
    description: str
    api_key_url: str


@dataclass(frozen=True, slots=True)
class ProviderModel:
    id: str
    name: str
    # OpenAI and Anthropic do not say; a refused image becomes a text note.
    reads_images: bool = True


class _GatewayModel(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    name: str = Field(min_length=1, max_length=200)
    type: str
    tags: list[str] = Field(default_factory=list)


class _GatewayCatalog(BaseModel):
    data: list[_GatewayModel]


class _OpenAIModel(BaseModel):
    id: str = Field(min_length=1, max_length=200)


class _OpenAICatalog(BaseModel):
    data: list[_OpenAIModel]


class _AnthropicModel(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)


class _AnthropicCatalog(BaseModel):
    data: list[_AnthropicModel]
    has_more: bool
    last_id: str | None


def _openai_tool_model(model_id: str) -> bool:
    # OpenAI's catalog has no capability fields. Limit it to Responses model
    # families with function calling, excluding models for specialized endpoints.
    return bool(
        re.fullmatch(r"(?:gpt-(?:4o|4\.1|4\.5|[5-9](?:\.\d+)?)|o[134])(?:-[\w.]+)*", model_id)
        and not any(
            part in model_id
            for part in (
                "audio",
                "realtime",
                "transcribe",
                "tts",
                "search",
                "chat",
                "image",
                "o1-mini",
                "o1-preview",
            )
        )
    )


# These IDs match Pydantic AI's native providers. Add entries only when the
# connection form and credential check are supported here.
PROVIDERS = (
    ProviderDefinition(
        id="vercel",
        name="Vercel AI Gateway",
        description="Access models from multiple providers with one API key.",
        api_key_url="https://vercel.com/docs/ai-gateway/authentication-and-byok/api-keys",
    ),
    ProviderDefinition(
        id="openai",
        name="OpenAI",
        description="Access OpenAI models with your API key.",
        api_key_url="https://platform.openai.com/api-keys",
    ),
    ProviderDefinition(
        id="anthropic",
        name="Anthropic",
        description="Access Claude models with your Anthropic API key.",
        api_key_url="https://platform.claude.com/settings/keys",
    ),
)


class ProviderConnectionError(Exception):
    def __init__(self, code: ConnectionErrorCode) -> None:
        self.code = code
        super().__init__(code)


class ProviderConnections:
    def __init__(
        self,
        store: ControlStore,
        cipher: TokenCipher,
        client: httpx.AsyncClient,
        *,
        runtime_schema: str = "hondo_dbos",
    ) -> None:
        self.store = store
        self.cipher = cipher
        self.client = client
        self.runtime_schema = runtime_schema

    async def models(self, workspace_id: UUID, connection_id: UUID) -> list[ProviderModel]:
        connection = await self._connection(workspace_id, connection_id)
        if connection.check_status != "valid":
            raise ProviderConnectionError("connection_required")
        provider = connection.provider
        if provider != "vercel":
            key = self._key(workspace_id, connection)
            try:
                models = await self._native_models(provider, key)
            except (httpx.HTTPError, ValueError) as error:
                if isinstance(error, httpx.HTTPStatusError) and error.response.status_code in (
                    401,
                    403,
                ):
                    logger.info("provider key rejected", status=error.response.status_code)
                    await self.store.record_provider_check(workspace_id, connection, "rejected")
                    raise ProviderConnectionError("connection_required") from None
                logger.warning("model catalog request failed", error_type=type(error).__name__)
                raise ProviderConnectionError("models_unavailable") from None
            return sorted(models, key=lambda item: (item.name.casefold(), item.id))
        try:
            response = await self.client.get(
                "https://ai-gateway.vercel.sh/v1/models", timeout=10, follow_redirects=False
            )
            response.raise_for_status()
            catalog = _GatewayCatalog.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as error:
            logger.warning("model catalog request failed", error_type=type(error).__name__)
            raise ProviderConnectionError("models_unavailable") from None
        return sorted(
            (
                ProviderModel(item.id, item.name, reads_images="vision" in item.tags)
                for item in catalog.data
                if item.type == "language" and "tool-use" in item.tags
            ),
            key=lambda item: (item.name.casefold(), item.id),
        )

    async def select_model(self, workspace_id: UUID, connection_id: UUID, model_name: str) -> None:
        model = next(
            (
                model
                for model in await self.models(workspace_id, connection_id)
                if model.id == model_name
            ),
            None,
        )
        if model is None:
            raise ProviderConnectionError("model_unavailable")
        if not await self.store.set_agent_model(
            workspace_id, connection_id, model_name, reads_images=model.reads_images
        ):
            raise ProviderConnectionError("connection_required")

    async def model_ready(self, selection: AgentModel | None) -> bool:
        if selection is None:
            return False
        connection = await self.store.get_provider_connection(
            selection.workspace_id, selection.connection_id
        )
        if connection is None or connection.check_status != "valid":
            return False
        try:
            self._key(selection.workspace_id, connection)
        except ProviderConnectionError:
            return False
        return True

    async def clear_model(self, workspace_id: UUID) -> None:
        await self.store.clear_agent_model(workspace_id)

    async def disconnect(self, workspace_id: UUID, connection_id: UUID) -> None:
        try:
            deleted = await self.store.delete_provider_connection(
                workspace_id, connection_id, runtime_schema=self.runtime_schema
            )
        except Exception as error:
            logger.error("provider disconnect failed", error_type=type(error).__name__)
            raise ProviderConnectionError("disconnect_unavailable") from None
        if not deleted:
            raise ProviderConnectionError("connection_in_use")

    async def _native_catalog(
        self, provider: str, key: SecretStr, *, after_id: str | None = None
    ) -> httpx.Response:
        if provider == "openai":
            url = "https://api.openai.com/v1/models"
            headers = {"Authorization": f"Bearer {key.get_secret_value()}"}
            params = {}
        elif provider == "anthropic":
            url = "https://api.anthropic.com/v1/models"
            headers = {
                "x-api-key": key.get_secret_value(),
                "anthropic-version": "2023-06-01",
            }
            params = {"limit": "1000"}
            if after_id is not None:
                params["after_id"] = after_id
        else:
            raise ProviderConnectionError("models_unavailable")
        return await self.client.get(
            url, headers=headers, params=params, timeout=10, follow_redirects=False
        )

    async def _native_models(self, provider: str, key: SecretStr) -> list[ProviderModel]:
        models: list[ProviderModel] = []
        cursors: set[str] = set()
        after_id = None
        while True:
            response = await self._native_catalog(provider, key, after_id=after_id)
            response.raise_for_status()
            if provider == "openai":
                catalog = _OpenAICatalog.model_validate(response.json())
                return [
                    ProviderModel(item.id, item.id)
                    for item in catalog.data
                    if _openai_tool_model(item.id)
                ]
            page = _AnthropicCatalog.model_validate(response.json())
            models.extend(ProviderModel(item.id, item.display_name) for item in page.data)
            if not page.has_more:
                return models
            if not page.data or not page.last_id or page.last_id in cursors:
                raise ValueError("invalid model catalog cursor")
            after_id = page.last_id
            cursors.add(after_id)

    async def _check(self, provider: str, key: SecretStr) -> None:
        # Vercel's model catalog is public, so its key check uses credits.
        # The native catalogs require authentication. None generates a response.
        try:
            if provider == "vercel":
                response = await self.client.get(
                    "https://ai-gateway.vercel.sh/v1/credits",
                    headers={"Authorization": f"Bearer {key.get_secret_value()}"},
                    timeout=10,
                    follow_redirects=False,
                )
            else:
                response = await self._native_catalog(provider, key)
        except httpx.HTTPError as error:
            logger.warning("provider check failed", error_type=type(error).__name__)
            raise ProviderConnectionError("check_unavailable") from None
        if response.status_code in (401, 403):
            logger.info("provider key rejected", status=response.status_code)
            raise ProviderConnectionError("key_rejected")
        if response.status_code != 200:
            logger.warning("provider check failed", status=response.status_code)
            raise ProviderConnectionError("check_unavailable")
        try:
            payload = response.json()
            if provider == "openai":
                _OpenAICatalog.model_validate(payload)
                return
            if provider == "anthropic":
                _AnthropicCatalog.model_validate(payload)
                return
            if not isinstance(payload, dict) or not all(
                isinstance(payload.get(field), str) for field in ("balance", "total_used")
            ):
                raise ValueError
        except ValueError:
            logger.warning("provider check returned an invalid response")
            raise ProviderConnectionError("check_unavailable") from None

    async def create(
        self, workspace_id: UUID, provider: ProviderId, name: str, key: SecretStr
    ) -> ProviderConnection:
        await self._check(provider, key)
        try:
            return await self.store.create_provider_connection(
                workspace_id, provider, name, self.cipher.encrypt(key.get_secret_value())
            )
        except ProviderConnectionNameTaken:
            raise ProviderConnectionError("connection_name_taken") from None

    async def update(
        self, workspace_id: UUID, connection_id: UUID, name: str, key: SecretStr | None
    ) -> ProviderConnection:
        connection = await self._connection(workspace_id, connection_id)
        encrypted = None
        if key is not None:
            await self._check(connection.provider, key)
            encrypted = self.cipher.encrypt(key.get_secret_value())
        try:
            updated = await self.store.update_provider_connection(
                workspace_id, connection_id, name, encrypted
            )
        except ProviderConnectionNameTaken:
            raise ProviderConnectionError("connection_name_taken") from None
        if updated is None:
            raise ProviderConnectionError("connection_not_found")
        return updated

    async def _connection(self, workspace_id: UUID, connection_id: UUID) -> ProviderConnection:
        connection = await self.store.get_provider_connection(workspace_id, connection_id)
        if connection is None:
            raise ProviderConnectionError("connection_not_found")
        return connection

    def _key(self, workspace_id: UUID, connection: ProviderConnection) -> SecretStr:
        try:
            return SecretStr(self.cipher.decrypt(connection.api_key_encrypted))
        except TokenCipherError:
            logger.error("provider key could not be decrypted", workspace_id=str(workspace_id))
            raise ProviderConnectionError("connections_unavailable") from None

    async def check(self, workspace_id: UUID, connection_id: UUID) -> None:
        connection = await self._connection(workspace_id, connection_id)
        key = self._key(workspace_id, connection)
        try:
            await self._check(connection.provider, key)
        except ProviderConnectionError as error:
            if error.code == "key_rejected":
                await self.store.record_provider_check(workspace_id, connection, "rejected")
            raise
        await self.store.record_provider_check(workspace_id, connection, "valid")
