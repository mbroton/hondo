from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

type MCPStatus = Literal["ready", "sign_in_required", "unavailable"]
type MCPAuthMethod = Literal["oauth", "key"]
type ConnectorKind = Literal["mcp", "http"]

REFRESH_LEASE = timedelta(seconds=60)


class MCPError(Exception):
    def __init__(
        self,
        code: str,
        status: int = 503,
        *,
        retry_after: int | None = None,
    ) -> None:
        self.code = code
        self.status = status
        self.retry_after = retry_after
        super().__init__(code)


@dataclass(frozen=True)
class MCPChannelPage:
    channels: list[dict[str, str]]
    next_cursor: str | None


@dataclass(frozen=True)
class MCPOAuthResult:
    workspace_id: UUID
    connection_id: UUID
    outcome: Literal["connected", "cancelled", "authorization_failed"]


@dataclass(frozen=True)
class HTTPParam:
    name: str
    description: str


@dataclass(frozen=True)
class HTTPRequest:
    """One HTTP request that an admin defined. The model fills only the {params}."""

    method: str
    url: str
    body: str | None
    params: tuple[HTTPParam, ...]


@dataclass(frozen=True)
class RemoteTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    fingerprint: str
    read_only: bool = False
    request: HTTPRequest | None = None

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> RemoteTool:
        request = data.get("request")
        if request is not None:
            request = HTTPRequest(
                method=request["method"],
                url=request["url"],
                body=request["body"],
                params=tuple(HTTPParam(**param) for param in request["params"]),
            )
        return cls(**{**data, "request": request})

    @classmethod
    def http(cls, name: str, description: str, request: HTTPRequest) -> RemoteTool:
        """The tool that sends an HTTP request. The fingerprint covers the whole definition."""
        input_schema = {
            "type": "object",
            "properties": {
                param.name: {"type": "string", "description": param.description}
                for param in request.params
            },
            "required": [param.name for param in request.params],
            "additionalProperties": False,
        }
        encoded = json.dumps(
            {
                "name": name,
                "description": description,
                "method": request.method,
                "url": request.url,
                "body": request.body,
                "params": [[param.name, param.description] for param in request.params],
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            name=name,
            description=description,
            input_schema=input_schema,
            fingerprint=hashlib.sha256(encoded.encode()).hexdigest(),
            request=request,
        )

    @classmethod
    def from_definition(cls, definition: dict[str, Any]) -> RemoteTool:
        name = definition["name"]
        description = definition.get("description") or ""
        input_schema = definition["inputSchema"]
        annotations = definition.get("annotations") or {}
        # Only what the model sees and what a call sends. A server release
        # that changes other fields (title, annotations, _meta) must not
        # remove the tool's approval.
        encoded = json.dumps(
            {"name": name, "description": description, "inputSchema": input_schema},
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(
            name=name,
            description=description,
            input_schema=input_schema,
            fingerprint=hashlib.sha256(encoded.encode()).hexdigest(),
            read_only=annotations.get("readOnlyHint") is True,
        )


@dataclass(frozen=True)
class MCPConnection:
    id: UUID
    workspace_id: UUID
    name: str
    url: str
    authorized_by: UUID | None
    enabled: bool
    all_channels: bool
    channels: tuple[str, ...]
    tools: tuple[RemoteTool, ...]
    allowed_tools: dict[str, str]
    status: MCPStatus
    revision: int
    credential_version: int
    updated_at: datetime
    credentials: str | None = field(repr=False)
    authorized_by_name: str | None = None
    review_required: bool = False
    auth: MCPAuthMethod | None = None
    header_names: tuple[str, ...] = ()
    builtin: str | None = None
    ask_tools: tuple[str, ...] = ()
    kind: ConnectorKind = "mcp"


@dataclass(frozen=True)
class MCPSelection:
    connection_id: UUID
    workspace_id: UUID
    credential_version: int
    name: str
    all_channels: bool
    channels: tuple[str, ...]
    tools: tuple[RemoteTool, ...]
    sign_in_required: bool = False
    builtin: str | None = None
    ask_tools: tuple[str, ...] = ()
    kind: ConnectorKind = "mcp"


@dataclass(frozen=True)
class HTTPTestResult:
    status: int
    body: str


type ApprovalStatus = Literal["pending", "approved", "rejected", "expired"]


@dataclass(frozen=True)
class ToolApproval:
    id: str
    workflow_id: str
    team: str
    channel: str
    message_ts: str | None
    status: ApprovalStatus
    decided_by: str | None
    decided_at: datetime | None
