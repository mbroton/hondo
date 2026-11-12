from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self, cast
from uuid import UUID

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from hondo.agent_settings import AgentModel
from hondo.api.access import SlackMember, SlackMemberPage, TeamAccess
from hondo.api.admins import Admin
from hondo.api.provider_connections import ProviderDefinition, ProviderId, ProviderModel
from hondo.api.store import (
    ProviderConnection,
    SetupFacts,
    Skill,
    StoredSummary,
    User,
    Workspace,
    WorkspaceActivity,
)
from hondo.chat import ChannelInfo, is_public
from hondo.mcp.auth import HEADER_NAME, RESERVED_HEADERS, printable_ascii
from hondo.mcp.catalog import BUILTIN_ID, INPUT_NAME, BuiltinCatalog, BuiltinConnector
from hondo.mcp.http import PLACEHOLDER_NAME, check_template
from hondo.mcp.models import (
    ConnectorKind,
    HTTPParam,
    HTTPRequest,
    HTTPTestResult,
    MCPAuthMethod,
    MCPChannelPage,
    MCPConnection,
    MCPStatus,
    RemoteTool,
)
from hondo.mcp.network import public_url
from hondo.skills import SKILL_CONTENT_MAX_CHARS, storable
from hondo.slack import SlackStyle
from hondo.usage import RatedLine, RatedUsage

_MONTH_PATTERN = r"^\d{4}-(?:0[1-9]|1[0-2])$"
_CHANNEL = Annotated[str, Field(pattern=r"^[CG][A-Z0-9]{2,30}$")]


class MCPConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2048)

    @field_validator("name", "url", mode="before")
    @classmethod
    def strip_values(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return public_url(value)


class MCPPolicyUpdate(BaseModel):
    revision: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=100)
    enabled: bool
    all_channels: bool
    channels: list[_CHANNEL] = Field(max_length=500)
    allowed_tools: list[
        Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")]
    ] = Field(max_length=200)
    ask_tools: list[str] = Field(
        max_length=200,
        description="Allowed tools that need a person's approval in Slack before each call.",
    )
    review_tools: bool = Field(
        default=False, description="Confirm review of the current tool selection."
    )

    @model_validator(mode="after")
    def ask_only_allowed(self) -> Self:
        if not set(self.ask_tools) <= set(self.allowed_tools):
            raise ValueError("ask_tools must be allowed tools")
        return self

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("channels")
    @classmethod
    def normalize_channels(cls, value: list[str]) -> list[str]:
        return sorted(set(value))


class MCPOAuthStart(BaseModel):
    client_id: str | None = Field(default=None, min_length=1, max_length=2048)
    client_secret: SecretStr | None = Field(default=None, min_length=1, max_length=8192)
    scope: str | None = Field(default=None, max_length=2048)


class MCPOAuthRedirect(BaseModel):
    url: str


class MCPKeyHeader(BaseModel):
    name: str = Field(min_length=1, max_length=100, pattern=HEADER_NAME)
    value: SecretStr = Field(min_length=1, max_length=8192)

    @field_validator("name", "value", mode="before")
    @classmethod
    def strip_values(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("name")
    @classmethod
    def not_reserved(cls, value: str) -> str:
        if value.lower() in RESERVED_HEADERS:
            raise ValueError("reserved header")
        return value

    @field_validator("value")
    @classmethod
    def printable(cls, value: SecretStr) -> SecretStr:
        if not printable_ascii(value.get_secret_value()):
            raise ValueError("header values are printable ASCII")
        return value


class MCPKeySave(BaseModel):
    headers: list[MCPKeyHeader] = Field(
        max_length=10,
        description=(
            "All headers of the key. They replace the saved headers. "
            "Only an HTTP API connector can have none."
        ),
    )

    @field_validator("headers")
    @classmethod
    def unique_names(cls, value: list[MCPKeyHeader]) -> list[MCPKeyHeader]:
        return _unique_headers(value)


def _unique_headers(value: list[MCPKeyHeader]) -> list[MCPKeyHeader]:
    if len({header.name.lower() for header in value}) != len(value):
        raise ValueError("duplicate header")
    return value


class HTTPConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    url: str = Field(
        min_length=1,
        max_length=2048,
        description="The API address. Each tool URL must use its scheme, host and port.",
    )
    headers: list[MCPKeyHeader] = Field(
        max_length=10,
        description="Sent with each request of each tool. The API never returns the values.",
    )

    @field_validator("name", "url", mode="before")
    @classmethod
    def strip_values(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return public_url(value)

    @field_validator("headers")
    @classmethod
    def unique_names(cls, value: list[MCPKeyHeader]) -> list[MCPKeyHeader]:
        return _unique_headers(value)


class HTTPParamIn(BaseModel):
    name: str = Field(pattern=PLACEHOLDER_NAME)
    description: str = Field(min_length=1, max_length=500)

    @field_validator("description", mode="before")
    @classmethod
    def strip_description(cls, value: Any) -> Any:
        return _strip(value)


class HTTPToolSave(BaseModel):
    name: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    description: str = Field(min_length=1, max_length=2000, description="What the model reads.")
    method: str = Field(min_length=1, max_length=16, pattern=r"^[A-Z]+$")
    url: str = Field(
        min_length=1,
        max_length=2048,
        description="The full URL. {name} marks a parameter in the path or the query.",
    )
    body: str | None = Field(
        default=None,
        max_length=20_000,
        description="A JSON body. {name} marks a parameter. Sent as application/json.",
    )
    params: list[HTTPParamIn] = Field(
        max_length=20, description="One for each {name} in the URL and the body."
    )

    @field_validator("name", "description", "url", "body", mode="before")
    @classmethod
    def strip_values(cls, value: Any) -> Any:
        return _strip(value) or None if isinstance(value, str) else value

    @field_validator("method", mode="before")
    @classmethod
    def upper_method(cls, value: Any) -> Any:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def check_request(self) -> Self:
        names = [param.name for param in self.params]
        if len(set(names)) != len(names):
            raise ValueError("duplicate parameter")
        check_template(self.url, self.body, set(names))
        return self

    def to_domain(self) -> RemoteTool:
        return RemoteTool.http(
            self.name,
            self.description,
            HTTPRequest(
                method=self.method,
                url=self.url,
                body=self.body,
                params=tuple(HTTPParam(param.name, param.description) for param in self.params),
            ),
        )


class HTTPToolTest(BaseModel):
    arguments: dict[Annotated[str, Field(pattern=PLACEHOLDER_NAME)], str] = Field(
        max_length=20, description="A sample value for each parameter."
    )


class HTTPTestOut(BaseModel):
    status: int = Field(description="The HTTP status that the API returned.")
    body: str = Field(description="The response body, shortened when it is long.")

    @classmethod
    def from_domain(cls, result: HTTPTestResult) -> Self:
        return cls(status=result.status, body=result.body)


def _strip(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


def _printable_secret(value: SecretStr) -> SecretStr:
    if not printable_ascii(value.get_secret_value()):
        raise ValueError("input values are printable ASCII")
    return value


_BUILTIN_INPUT_NAME = Annotated[str, Field(min_length=1, max_length=40, pattern=INPUT_NAME)]
_BUILTIN_INPUT_VALUE = Annotated[
    SecretStr,
    BeforeValidator(_strip),
    Field(min_length=1, max_length=8192),
    AfterValidator(_printable_secret),
]


class MCPBuiltinKeySave(BaseModel):
    inputs: dict[_BUILTIN_INPUT_NAME, _BUILTIN_INPUT_VALUE] = Field(
        max_length=10,
        description="A value for each input of the built-in connector, and no other names. "
        "The API never returns them.",
    )


class MCPBuiltinConnect(MCPBuiltinKeySave):
    builtin: str = Field(
        min_length=1, max_length=100, pattern=BUILTIN_ID, description="A built-in connector id."
    )
    name: str = Field(min_length=1, max_length=100)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value: Any) -> Any:
        return _strip(value)


class BuiltinInputOut(BaseModel):
    name: str
    label: str
    secret: bool = Field(description="Show a password field.")
    help: str | None


class BuiltinSkillOut(BaseModel):
    name: str
    description: str


class BuiltinConnectorOut(BaseModel):
    id: str
    title: str = Field(description="Also the default connection name.")
    description: str
    url: str
    inputs: list[BuiltinInputOut]
    setup: list[str] = Field(description="Steps to do before connecting, in order.")
    skills: list[BuiltinSkillOut]

    @classmethod
    def from_domain(cls, connector: BuiltinConnector) -> Self:
        return cls(
            id=connector.id,
            title=connector.title,
            description=connector.description,
            url=connector.url,
            inputs=[
                BuiltinInputOut(
                    name=item.name, label=item.label, secret=item.secret, help=item.help
                )
                for item in connector.inputs
            ],
            setup=list(connector.setup),
            skills=[
                BuiltinSkillOut(name=skill.name, description=skill.description)
                for skill in connector.skills
            ],
        )


class BuiltinConnectorsOut(BaseModel):
    connectors: list[BuiltinConnectorOut] = Field(description="Ordered by title.")

    @classmethod
    def from_domain(cls, catalog: BuiltinCatalog) -> Self:
        return cls(
            connectors=[BuiltinConnectorOut.from_domain(item) for item in catalog.connectors]
        )


class HTTPParamOut(BaseModel):
    name: str
    description: str


class HTTPRequestOut(BaseModel):
    method: str
    url: str
    body: str | None
    params: list[HTTPParamOut]

    @classmethod
    def from_domain(cls, request: HTTPRequest) -> Self:
        return cls(
            method=request.method,
            url=request.url,
            body=request.body,
            params=[HTTPParamOut(name=p.name, description=p.description) for p in request.params],
        )


class MCPToolOut(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]
    enabled: bool
    read_only: bool = Field(description="The server marks this tool as read-only.")
    ask: bool = Field(description="Each call needs a person's approval in Slack.")
    request: HTTPRequestOut | None = Field(description="The request of an HTTP tool.")


class MCPConnectionOut(BaseModel):
    id: str
    name: str
    url: str
    authorized_by: str | None
    authorized_by_name: str | None
    connected: bool
    enabled: bool = Field(description="Saved enable setting for this connector.")
    all_channels: bool
    channels: list[str]
    tools: list[MCPToolOut]
    status: MCPStatus
    review_required: bool
    revision: int
    auth: MCPAuthMethod | None = Field(
        description="How the account is connected: OAuth sign-in or a key. Null when not connected."
    )
    header_names: list[str] = Field(
        description="Header names of a saved key. The API never returns header values."
    )
    builtin: str | None = Field(
        description="The id of the built-in connector that this connection was made from."
    )
    kind: ConnectorKind = Field(description="An MCP server, or HTTP tools that admins define.")

    @classmethod
    def from_domain(cls, saved: MCPConnection) -> MCPConnectionOut:
        return cls(
            id=str(saved.id),
            name=saved.name,
            url=saved.url,
            authorized_by=str(saved.authorized_by) if saved.authorized_by else None,
            authorized_by_name=saved.authorized_by_name,
            connected=saved.credentials is not None,
            enabled=saved.enabled,
            all_channels=saved.all_channels,
            channels=list(saved.channels),
            tools=[
                MCPToolOut(
                    name=tool.name,
                    description=tool.description,
                    input_schema=tool.input_schema,
                    enabled=saved.allowed_tools.get(tool.name) == tool.fingerprint,
                    read_only=tool.read_only,
                    ask=tool.name in saved.ask_tools,
                    request=HTTPRequestOut.from_domain(tool.request) if tool.request else None,
                )
                for tool in saved.tools
            ],
            status=saved.status,
            review_required=saved.review_required,
            revision=saved.revision,
            auth=saved.auth,
            header_names=list(saved.header_names),
            builtin=saved.builtin,
            kind=saved.kind,
        )


class MCPConnectionsOut(BaseModel):
    connections: list[MCPConnectionOut]


class MCPChannelOut(BaseModel):
    id: str
    name: str


class MCPChannelsOut(BaseModel):
    channels: list[MCPChannelOut]
    next_cursor: str | None

    @classmethod
    def from_domain(cls, page: MCPChannelPage) -> Self:
        return cls(
            channels=[MCPChannelOut(**channel) for channel in page.channels],
            next_cursor=page.next_cursor,
        )


class MCPClientMetadata(BaseModel):
    client_id: str
    client_name: str
    redirect_uris: list[str]
    grant_types: list[str]
    response_types: list[str]
    token_endpoint_auth_method: str


_SKILL_CONTENT_DESCRIPTION = (
    "The full SKILL.md file: YAML frontmatter with name and description, then Markdown "
    "instructions."
)


def _storable(content: str) -> str:
    if not storable(content):
        raise ValueError("characters that cannot be saved")
    return content


_SKILL_CONTENT = Annotated[
    str,
    Field(min_length=1, max_length=SKILL_CONTENT_MAX_CHARS, description=_SKILL_CONTENT_DESCRIPTION),
    AfterValidator(_storable),
]


class SkillCreate(BaseModel):
    content: _SKILL_CONTENT


class SkillUpdate(BaseModel):
    revision: int = Field(ge=1)
    content: _SKILL_CONTENT
    published: bool
    all_channels: bool = Field(description="All Slack channels, also channels added later.")
    channels: list[_CHANNEL] = Field(max_length=500)

    @field_validator("channels")
    @classmethod
    def normalize_channels(cls, value: list[str]) -> list[str]:
        return sorted(set(value))


class SkillSummaryOut(BaseModel):
    id: str
    name: str
    description: str
    published: bool
    all_channels: bool
    channels: list[str]
    source_url: str | None = Field(
        description="The Slack thread that Hondo drafted the skill from."
    )
    revision: int
    updated_at: datetime

    @classmethod
    def from_domain(cls, skill: Skill) -> Self:
        return cls(
            id=str(skill.id),
            name=skill.name,
            description=skill.description,
            published=skill.published,
            all_channels=skill.all_channels,
            channels=list(skill.channels),
            source_url=SlackStyle().message_link(skill.source_channel, skill.source_ts)
            if skill.source_channel is not None and skill.source_ts is not None
            else None,
            revision=skill.revision,
            updated_at=skill.updated_at,
        )


class SkillOut(SkillSummaryOut):
    content: str = Field(description=_SKILL_CONTENT_DESCRIPTION)

    @classmethod
    def from_domain(cls, skill: Skill) -> Self:
        return cls(**SkillSummaryOut.from_domain(skill).model_dump(), content=skill.content)


class SkillsOut(BaseModel):
    skills: list[SkillSummaryOut]

    @classmethod
    def from_domain(cls, skills: list[Skill]) -> Self:
        return cls(skills=[SkillSummaryOut.from_domain(skill) for skill in skills])


class ThreadSummaryOut(BaseModel):
    id: int
    private: bool = Field(description="A private channel: no channel, link or text.")
    channel: str | None = Field(description="The channel name.")
    url: str | None = Field(description="The thread in Slack.")
    text: str | None
    updated_at: datetime

    @classmethod
    def from_domain(cls, summary: StoredSummary, info: ChannelInfo | None) -> Self:
        if summary.private or not is_public(info):
            return cls(
                id=summary.id,
                private=True,
                channel=None,
                url=None,
                text=None,
                updated_at=summary.updated_at,
            )
        return cls(
            id=summary.id,
            private=False,
            channel=info.name or summary.channel,
            url=SlackStyle().message_link(summary.channel, summary.thread_ts),
            text=summary.text,
            updated_at=summary.updated_at,
        )


class ThreadSummariesOut(BaseModel):
    summaries: list[ThreadSummaryOut]
    next_offset: int | None

    @classmethod
    def from_domain(
        cls,
        summaries: list[StoredSummary],
        channels: dict[str, ChannelInfo | None],
        next_offset: int | None,
    ) -> Self:
        return cls(
            summaries=[
                ThreadSummaryOut.from_domain(summary, channels.get(summary.channel))
                for summary in summaries
            ],
            next_offset=next_offset,
        )


def _current_utc_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _month_window(month: str) -> tuple[datetime, datetime]:
    """Half-open UTC window for a YYYY-MM month, as [start, next month)."""
    year, index = (int(part) for part in month.split("-"))
    start = datetime(year, index, 1, tzinfo=UTC)
    end = (
        datetime(year + 1, 1, 1, tzinfo=UTC)
        if index == 12
        else datetime(year, index + 1, 1, tzinfo=UTC)
    )
    return start, end


class UsageQuery(BaseModel):
    month: str = Field(
        default_factory=_current_utc_month,
        pattern=_MONTH_PATTERN,
        description="UTC calendar month in YYYY-MM format.",
    )

    # The pattern admits any four-digit year, but datetime stops at 9999, so
    # the window is built during validation: an unbuildable month is a bad
    # request, not a 500 from inside the route.
    @model_validator(mode="after")
    def check_month_is_in_range(self) -> Self:
        try:
            _month_window(self.month)
        except ValueError as error:
            raise ValueError("month is outside the supported range") from error
        return self

    @property
    def window(self) -> tuple[datetime, datetime]:
        return _month_window(self.month)


class ActivityQuery(BaseModel):
    limit: int = Field(default=50, ge=1, le=200)


class ThreadSummaryQuery(BaseModel):
    q: str = Field(
        default="",
        max_length=200,
        description="Words to search for, as the agent searches. Empty lists all summaries.",
    )
    offset: int = Field(default=0, ge=0, le=100_000, description="Ignored for a search.")

    @field_validator("q", mode="before")
    @classmethod
    def strip_query(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @property
    def words(self) -> list[str]:
        return self.q.split()


# Response models: the store speaks plain dataclasses; pydantic lives at the
# HTTP edge only, where it buys OpenAPI schemas and a pinned wire format.
# UUIDs go out as plain strings.


class UserOut(BaseModel):
    id: str
    email: str
    display_name: str
    avatar_url: str | None

    @classmethod
    def from_domain(cls, user: User) -> Self:
        return cls(
            id=str(user.id),
            email=user.email,
            display_name=user.display_name,
            avatar_url=user.avatar_url,
        )


class TeamOut(BaseModel):
    id: str = Field(description="Slack team ID of the signed-in Slack account.")
    name: str
    workspace_id: str | None = Field(description="Hondo's workspace in this team, if installed.")
    is_admin: bool
    admins: list[str] = Field(
        description="Names of admins to ask for access; empty unless installed and not an admin."
    )
    slack: Literal["ok", "reinstall_required", "unavailable"] = Field(
        description=(
            "ok: Slack answered. reinstall_required: Slack refused Hondo's token; an "
            "admin's install restores it. unavailable: Slack did not answer, so "
            "access is refused for now."
        )
    )

    @classmethod
    def from_domain(cls, team: TeamAccess) -> Self:
        return cls(
            id=team.team_id,
            name=team.team_name,
            workspace_id=str(team.workspace_id) if team.workspace_id is not None else None,
            is_admin=team.is_admin,
            admins=team.admins,
            slack=team.slack,
        )


class MeResponse(BaseModel):
    """The SPA bootstrap payload."""

    user: UserOut
    team: TeamOut

    @classmethod
    def from_domain(cls, user: User, team: TeamAccess) -> Self:
        return cls(user=UserOut.from_domain(user), team=TeamOut.from_domain(team))


class ProviderModelOut(BaseModel):
    id: str
    name: str
    reads_images: bool

    @classmethod
    def from_domain(cls, model: ProviderModel) -> Self:
        return cls(id=model.id, name=model.name, reads_images=model.reads_images)


class ProviderModelsResponse(BaseModel):
    models: list[ProviderModelOut]

    @classmethod
    def from_domain(cls, models: list[ProviderModel]) -> Self:
        return cls(models=[ProviderModelOut.from_domain(model) for model in models])


class MentionOnlyChannels(BaseModel):
    """Channels where Hondo answers only messages that mention it."""

    channels: list[_CHANNEL] = Field(max_length=500)

    @field_validator("channels")
    @classmethod
    def normalize_channels(cls, value: list[str]) -> list[str]:
        return sorted(set(value))

    @classmethod
    def from_domain(cls, channels: tuple[str, ...]) -> Self:
        return cls(channels=list(channels))


class ListeningChannel(BaseModel):
    channel: _CHANNEL
    goal: str = Field(
        min_length=1,
        max_length=1000,
        description="What Hondo listens for in the channel, and how it helps.",
    )

    @field_validator("goal", mode="before")
    @classmethod
    def strip_goal(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class ListeningChannels(BaseModel):
    """Channels where Hondo also reads messages that do not mention it."""

    channels: list[ListeningChannel] = Field(max_length=500)

    @field_validator("channels")
    @classmethod
    def unique_channels(cls, value: list[ListeningChannel]) -> list[ListeningChannel]:
        if len({item.channel for item in value}) != len(value):
            raise ValueError("each channel can have only one goal")
        return sorted(value, key=lambda item: item.channel)

    @classmethod
    def from_domain(cls, goals: dict[str, str]) -> Self:
        return cls(
            channels=[
                ListeningChannel(channel=channel, goal=goal) for channel, goal in goals.items()
            ]
        )

    def to_domain(self) -> dict[str, str]:
        return {item.channel: item.goal for item in self.channels}


class AgentModelRequest(BaseModel):
    connection_id: UUID
    model_name: str = Field(min_length=1, max_length=200, description="ID from the model catalog.")

    @field_validator("model_name", mode="before")
    @classmethod
    def strip_model(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value


class AgentModelResponse(BaseModel):
    source: Literal["unconfigured", "connection"]
    ready: bool
    connection_id: str | None
    provider: str | None
    model_name: str | None

    @classmethod
    def from_domain(cls, selection: AgentModel | None, *, ready: bool) -> Self:
        if selection is None:
            return cls(
                source="unconfigured",
                ready=False,
                connection_id=None,
                provider=None,
                model_name=None,
            )
        return cls(
            source="connection",
            ready=ready,
            connection_id=str(selection.connection_id),
            provider=selection.provider,
            model_name=selection.model_name,
        )


class _ProviderConnectionRequest(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(min_length=1, max_length=100)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("api_key", mode="before", check_fields=False)
    @classmethod
    def strip_api_key(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        value = value.strip()
        if not value.isascii() or not value.isprintable() or any(c.isspace() for c in value):
            raise ValueError("invalid key")
        return value


class ProviderConnectionUpdate(_ProviderConnectionRequest):
    api_key: SecretStr | None = Field(
        default=None, min_length=1, max_length=65_536, description="Replacement API key."
    )


class ProviderConnectionCreate(_ProviderConnectionRequest):
    provider: ProviderId
    api_key: SecretStr = Field(min_length=1, max_length=65_536, description="Provider API key.")


class ApiErrorResponse(BaseModel):
    detail: str


class ProviderConnectionOut(BaseModel):
    id: str
    name: str
    provider: str
    check_status: Literal["valid", "rejected"]
    checked_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, connection: ProviderConnection) -> Self:
        return cls(
            id=str(connection.id),
            name=connection.name,
            provider=connection.provider,
            check_status=connection.check_status,
            checked_at=connection.checked_at,
            updated_at=connection.updated_at,
        )


class ProviderOut(BaseModel):
    id: ProviderId
    name: str
    description: str
    api_key_url: str

    @classmethod
    def from_domain(cls, provider: ProviderDefinition) -> Self:
        return cls(
            id=provider.id,
            name=provider.name,
            description=provider.description,
            api_key_url=provider.api_key_url,
        )


class ProvidersResponse(BaseModel):
    providers: list[ProviderOut]
    connections: list[ProviderConnectionOut]

    @classmethod
    def from_domain(
        cls, catalog: tuple[ProviderDefinition, ...], connections: list[ProviderConnection]
    ) -> Self:
        return cls(
            providers=[ProviderOut.from_domain(item) for item in catalog],
            connections=[ProviderConnectionOut.from_domain(item) for item in connections],
        )


class WorkspaceOut(BaseModel):
    id: str
    slack_team_id: str
    name: str
    status: str

    @classmethod
    def from_domain(cls, workspace: Workspace) -> Self:
        return cls(
            id=str(workspace.id),
            slack_team_id=workspace.slack_team_id,
            name=workspace.name,
            status=workspace.status,
        )


class WorkspacesResponse(BaseModel):
    workspaces: list[WorkspaceOut]

    @classmethod
    def from_domain(cls, workspaces: list[Workspace]) -> Self:
        return cls(workspaces=[WorkspaceOut.from_domain(workspace) for workspace in workspaces])


class UsageModelOut(BaseModel):
    model: str
    provider: str
    input_tokens: int
    output_tokens: int
    requests: int
    cost_usd: Decimal | None

    @classmethod
    def from_domain(cls, line: RatedLine) -> Self:
        return cls(
            model=line.resource,
            provider=line.provider,
            input_tokens=line.quantity_in,
            output_tokens=line.quantity_out,
            requests=line.requests,
            cost_usd=line.cost_usd,
        )


class UsageResponse(BaseModel):
    workspace_id: str
    month: str
    models: list[UsageModelOut]
    total_cost_usd: Decimal
    has_unpriced: bool

    @classmethod
    def from_domain(cls, workspace_id: UUID, month: str, usage: RatedUsage) -> Self:
        # Most expensive first, because that's what anyone opening this looks
        # for; unpriced lines have no place in that order, so they go last.
        priced = sorted(
            (line for line in usage.lines if line.cost_usd is not None),
            key=lambda line: (-cast(Decimal, line.cost_usd), line.resource),
        )
        unpriced = sorted(
            (line for line in usage.lines if line.cost_usd is None),
            key=lambda line: line.resource,
        )
        return cls(
            workspace_id=str(workspace_id),
            month=month,
            models=[UsageModelOut.from_domain(line) for line in [*priced, *unpriced]],
            total_cost_usd=usage.total_cost_usd,
            has_unpriced=usage.has_unpriced,
        )


class ActivityItemOut(BaseModel):
    channel: str
    thread_root: str
    state: str
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_domain(cls, item: WorkspaceActivity) -> Self:
        return cls(
            channel=item.channel,
            thread_root=item.thread_root,
            state=item.state,
            created_at=item.created_at,
            updated_at=item.updated_at,
        )


class ActivityResponse(BaseModel):
    items: list[ActivityItemOut]

    @classmethod
    def from_domain(cls, items: list[WorkspaceActivity]) -> Self:
        return cls(items=[ActivityItemOut.from_domain(item) for item in items])


class SetupResponse(BaseModel):
    provider_connected: bool = Field(description="A provider key that its last check accepted.")
    model_selected: bool = Field(description="A model that Hondo can answer with now.")
    tool_connected: bool = Field(description="A connector that an admin turned on.")
    answered_in_channel: bool = Field(description="Hondo has answered in a Slack channel.")
    complete: bool

    @classmethod
    def from_domain(cls, facts: SetupFacts, *, model_selected: bool) -> Self:
        return cls(
            provider_connected=facts.provider_connected,
            model_selected=model_selected,
            tool_connected=facts.tool_connected,
            answered_in_channel=facts.answered_in_channel,
            complete=facts.provider_connected
            and model_selected
            and facts.tool_connected
            and facts.answered_in_channel,
        )


SLACK_USER_ID_PATTERN = r"^[UW][A-Z0-9]+$"


class AdminOut(BaseModel):
    slack_user_id: str
    name: str | None = Field(description="Name in Slack; null when Slack has no answer.")
    avatar_url: str | None
    status: Literal["active", "inactive", "unknown"] = Field(
        description=(
            "active: an active member of the team. inactive: deactivated, a guest, or no "
            "longer in the team. unknown: Slack did not answer."
        )
    )
    added_by: str | None = Field(
        description=(
            "Name of the admin who added this person; null for the installer, or "
            "when the adding admin is not known."
        )
    )
    created_at: datetime

    @classmethod
    def from_domain(cls, admin: Admin) -> Self:
        return cls(
            slack_user_id=admin.slack_user_id,
            name=admin.name,
            avatar_url=admin.avatar_url,
            status=admin.status,
            added_by=admin.added_by,
            created_at=admin.created_at,
        )


class AdminsResponse(BaseModel):
    admins: list[AdminOut]

    @classmethod
    def from_domain(cls, admins: list[Admin]) -> Self:
        return cls(admins=[AdminOut.from_domain(admin) for admin in admins])


class SlackMemberOut(BaseModel):
    slack_user_id: str
    name: str = Field(description="The label to show: the display name, else the real name.")
    real_name: str | None = Field(description="Slack's full name, for search.")
    handle: str | None = Field(description="Slack's username, for search.")
    avatar_url: str | None

    @classmethod
    def from_domain(cls, member: SlackMember) -> Self:
        return cls(
            slack_user_id=member.id,
            name=member.name,
            real_name=member.real_name,
            handle=member.handle,
            avatar_url=member.avatar_url,
        )


class SlackMembersResponse(BaseModel):
    members: list[SlackMemberOut] = Field(
        description="Active members on this page; a page can be short or empty."
    )
    next_cursor: str | None

    @classmethod
    def from_domain(cls, page: SlackMemberPage) -> Self:
        return cls(
            members=[SlackMemberOut.from_domain(member) for member in page.members],
            next_cursor=page.next_cursor,
        )


class AdminAddRequest(BaseModel):
    model_config = {"extra": "forbid"}

    slack_user_id: str = Field(
        max_length=32,
        pattern=SLACK_USER_ID_PATTERN,
        description="Slack user ID of the team member to add.",
    )
