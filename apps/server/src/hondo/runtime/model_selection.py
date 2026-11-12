from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import UUID

from pydantic_ai.models import Model, ModelResolutionContext, infer_model
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.vercel import VercelProvider

from hondo.chat import ChannelInfo
from hondo.config import Config
from hondo.crypto import TokenCipher
from hondo.mcp.catalog import BuiltinCatalog
from hondo.mcp.models import MCPSelection
from hondo.mcp.store import MCPStore
from hondo.store.base import SkillStore, SkillSummary, Store


@dataclass(frozen=True, slots=True)
class RunModel:
    """Durable model and tool choices. Credentials never enter workflow state."""

    model_id: str
    connection_id: UUID | None = None
    workspace_id: UUID | None = None
    mcp_connections: tuple[MCPSelection, ...] = ()
    skills: tuple[SkillSummary, ...] = ()
    # Shared with another organization: "All Slack channels" does not reach it.
    shared_channel: bool = False
    reads_images: bool = True


# Anthropic models need cache breakpoints, and pydantic-ai adds none on the
# gateway's OpenAI-compatible API. The gateway adds them itself.
VERCEL_SETTINGS = OpenAIChatModelSettings(
    extra_body={"providerOptions": {"gateway": {"caching": "auto"}}}
)


type ModelSelector = Callable[[str, str], Awaitable[RunModel | None]]


class RuntimeModels:
    def __init__(
        self,
        store: Store,
        config: Config,
        mcp_store: MCPStore | None = None,
        *,
        skill_store: SkillStore | None = None,
        channel_info: Callable[[str], Awaitable[ChannelInfo | None]] | None = None,
        builtins: BuiltinCatalog = BuiltinCatalog(),
    ) -> None:
        self.store = store
        self.mcp_store = mcp_store
        self.skill_store = skill_store
        self.channel_info = channel_info
        self.builtins = builtins
        self.cipher = TokenCipher(config.crypto.token_key) if config.crypto else None

    async def select(self, team: str, channel: str | None = None) -> RunModel | None:
        selected = await self.store.get_agent_model(team)
        if selected is None:
            return None
        connections = await self.mcp_store.snapshot(team) if self.mcp_store else ()
        connections = tuple(
            connection
            for connection in connections
            if channel is not None and (connection.all_channels or channel in connection.channels)
        )
        skills = (
            tuple(await self.skill_store.channel_skills(team, channel))
            if self.skill_store is not None and channel is not None
            else ()
        )
        shared = False
        if connections or skills:
            info = (
                await self.channel_info(channel)
                if channel is not None and self.channel_info is not None
                else None
            )
            if channel is None or info is None or not info.is_channel:
                connections, skills = (), ()
            elif info.is_ext_shared:
                # "All Slack channels" leaves out channels shared with another
                # organization: an admin must select such a channel by name.
                shared = True
                connections = tuple(item for item in connections if channel in item.channels)
                skills = (
                    tuple(await self.skill_store.channel_skills(team, channel, listed_only=True))
                    if skills and self.skill_store is not None
                    else ()
                )
        builtin_skills = [
            SkillSummary(skill.name, skill.description)
            for connection in connections
            if connection.tools and (builtin := self.builtins.find(connection.builtin)) is not None
            for skill in builtin.skills
        ]
        if builtin_skills:
            # The team's own skill wins over a built-in skill with the same name.
            by_name = {skill.name: skill for skill in (*builtin_skills, *skills)}
            skills = tuple(by_name[name] for name in sorted(by_name))
        return RunModel(
            selected.model_id,
            selected.connection_id,
            selected.workspace_id,
            connections,
            skills,
            shared_channel=shared,
            reads_images=selected.reads_images,
        )

    async def resolve(self, ctx: ModelResolutionContext[RunModel], model_id: str) -> Model:
        selected = ctx.deps
        if model_id != selected.model_id:
            raise ValueError("model does not match the run's selection")
        provider, _, _ = model_id.partition(":")
        if (
            provider not in ("vercel", "openai", "anthropic")
            or selected.connection_id is None
            or selected.workspace_id is None
            or self.cipher is None
        ):
            raise ValueError("provider connection is unavailable")
        encrypted = await self.store.get_provider_key(selected.workspace_id, selected.connection_id)
        if encrypted is None:
            raise ValueError("provider connection is unavailable")
        # Resolve inside each model request so a rotated key takes effect without
        # recording either plaintext or ciphertext in DBOS step arguments/results.
        key = self.cipher.decrypt(encrypted)
        if provider == "openai":
            return infer_model(
                model_id,
                provider_factory=lambda _: OpenAIProvider(
                    api_key=key, base_url="https://api.openai.com/v1"
                ),
            )
        if provider == "anthropic":
            return infer_model(
                model_id,
                provider_factory=lambda _: AnthropicProvider(
                    api_key=key, base_url="https://api.anthropic.com"
                ),
            )
        return OpenAIChatModel(
            model_id.removeprefix("vercel:"),
            provider=VercelProvider(api_key=key),
            settings=VERCEL_SETTINGS,
        )
