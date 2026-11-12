from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from fastapi.responses import RedirectResponse

from hondo.api.access import AdminAccess, admin_access, workspace_admin
from hondo.api.deps import builtin_catalog, current_user, session_hash, session_user
from hondo.api.schemas import (
    ApiErrorResponse,
    BuiltinConnectorsOut,
    HTTPConnectionCreate,
    HTTPTestOut,
    HTTPToolSave,
    HTTPToolTest,
    MCPBuiltinConnect,
    MCPBuiltinKeySave,
    MCPChannelsOut,
    MCPClientMetadata,
    MCPConnectionCreate,
    MCPConnectionOut,
    MCPConnectionsOut,
    MCPKeySave,
    MCPOAuthRedirect,
    MCPOAuthStart,
    MCPPolicyUpdate,
)
from hondo.api.store import User, Workspace
from hondo.mcp.catalog import BuiltinCatalog
from hondo.mcp.models import MCPError
from hondo.mcp.service import MCPService, remote_errors
from hondo.mcp.store import MCPStore

router = APIRouter()
ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ApiErrorResponse} for code in (400, 401, 403, 404, 409, 422, 429, 503)
}


def mcp_service(request: Request) -> MCPService:
    return request.app.state.mcp_service


def mcp_store(request: Request) -> MCPStore:
    return request.app.state.mcp_store


@router.get("/workspaces/{workspace_id}/mcp-connections", responses=ERRORS)
async def list_connections(
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    store: Annotated[MCPStore, Depends(mcp_store)],
) -> MCPConnectionsOut:
    return MCPConnectionsOut(
        connections=[
            MCPConnectionOut.from_domain(item)
            for item in await store.list_connections(workspace.id)
        ]
    )


@router.post("/workspaces/{workspace_id}/mcp-connections", status_code=201, responses=ERRORS)
async def create_connection(
    body: MCPConnectionCreate,
    user: Annotated[User, Depends(current_user)],
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[MCPService, Depends(mcp_service)],
) -> MCPConnectionOut:
    return MCPConnectionOut.from_domain(
        await service.store.create_connection(workspace.id, body.name, body.url, user.id)
    )


@router.get("/workspaces/{workspace_id}/builtin-connectors", responses=ERRORS)
async def list_builtin_connectors(
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    catalog: Annotated[BuiltinCatalog, Depends(builtin_catalog)],
) -> BuiltinConnectorsOut:
    return BuiltinConnectorsOut.from_domain(catalog)


@router.post(
    "/workspaces/{workspace_id}/mcp-connections/builtin", status_code=201, responses=ERRORS
)
async def connect_builtin(
    body: MCPBuiltinConnect,
    user: Annotated[User, Depends(current_user)],
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[MCPService, Depends(mcp_service)],
    catalog: Annotated[BuiltinCatalog, Depends(builtin_catalog)],
) -> MCPConnectionOut:
    return MCPConnectionOut.from_domain(
        await service.connect_builtin(
            workspace.id,
            user.id,
            catalog,
            body.builtin,
            body.name,
            {name: value.get_secret_value() for name, value in body.inputs.items()},
        )
    )


@router.post("/workspaces/{workspace_id}/mcp-connections/http", status_code=201, responses=ERRORS)
async def create_http_connection(
    body: HTTPConnectionCreate,
    user: Annotated[User, Depends(current_user)],
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[MCPService, Depends(mcp_service)],
) -> MCPConnectionOut:
    return MCPConnectionOut.from_domain(
        await service.create_http(
            workspace.id,
            user.id,
            body.name,
            body.url,
            {header.name: header.value.get_secret_value() for header in body.headers},
        )
    )


_HTTP_TOOLS = "/workspaces/{workspace_id}/mcp-connections/{connection_id}/http-tools"
_TOOL_NAME = Annotated[str, Path(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")]


@router.post(_HTTP_TOOLS, status_code=201, responses=ERRORS)
async def add_http_tool(
    connection_id: UUID,
    body: HTTPToolSave,
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[MCPService, Depends(mcp_service)],
) -> MCPConnectionOut:
    return MCPConnectionOut.from_domain(
        await service.save_http_tool(workspace.id, connection_id, None, body.to_domain())
    )


@router.put(_HTTP_TOOLS + "/{tool_name}", responses=ERRORS)
async def save_http_tool(
    connection_id: UUID,
    tool_name: _TOOL_NAME,
    body: HTTPToolSave,
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[MCPService, Depends(mcp_service)],
) -> MCPConnectionOut:
    return MCPConnectionOut.from_domain(
        await service.save_http_tool(workspace.id, connection_id, tool_name, body.to_domain())
    )


@router.delete(_HTTP_TOOLS + "/{tool_name}", responses=ERRORS)
async def delete_http_tool(
    connection_id: UUID,
    tool_name: _TOOL_NAME,
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[MCPService, Depends(mcp_service)],
) -> MCPConnectionOut:
    return MCPConnectionOut.from_domain(
        await service.delete_http_tool(workspace.id, connection_id, tool_name)
    )


@router.post(_HTTP_TOOLS + "/{tool_name}/test", responses=ERRORS)
async def test_http_tool(
    connection_id: UUID,
    tool_name: _TOOL_NAME,
    body: HTTPToolTest,
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[MCPService, Depends(mcp_service)],
) -> HTTPTestOut:
    return HTTPTestOut.from_domain(
        await service.test_http_tool(workspace.id, connection_id, tool_name, body.arguments)
    )


@router.put("/workspaces/{workspace_id}/mcp-connections/{connection_id}", responses=ERRORS)
async def update_connection(
    connection_id: UUID,
    body: MCPPolicyUpdate,
    user: Annotated[User, Depends(current_user)],
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[MCPService, Depends(mcp_service)],
) -> MCPConnectionOut:
    return MCPConnectionOut.from_domain(
        await service.store.save_policy(
            workspace.id,
            connection_id,
            user.id,
            **body.model_dump(),
        )
    )


@router.post(
    "/workspaces/{workspace_id}/mcp-connections/{connection_id}/authorize", responses=ERRORS
)
async def authorize_connection(
    connection_id: UUID,
    body: MCPOAuthStart,
    user: Annotated[User, Depends(current_user)],
    session: Annotated[str | None, Depends(session_hash)],
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[MCPService, Depends(mcp_service)],
) -> MCPOAuthRedirect:
    if session is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    saved = await service.store.get_connection(workspace.id, connection_id)
    async with remote_errors():
        url = await service.oauth.start(
            saved,
            user.id,
            session,
            client_id=body.client_id,
            client_secret=body.client_secret.get_secret_value() if body.client_secret else None,
            scope=body.scope,
        )
    return MCPOAuthRedirect(url=url)


@router.put("/workspaces/{workspace_id}/mcp-connections/{connection_id}/key", responses=ERRORS)
async def save_key(
    connection_id: UUID,
    body: MCPKeySave,
    user: Annotated[User, Depends(current_user)],
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[MCPService, Depends(mcp_service)],
) -> MCPConnectionOut:
    return MCPConnectionOut.from_domain(
        await service.save_key(
            workspace.id,
            connection_id,
            user.id,
            {header.name: header.value.get_secret_value() for header in body.headers},
        )
    )


@router.put(
    "/workspaces/{workspace_id}/mcp-connections/{connection_id}/builtin-key", responses=ERRORS
)
async def save_builtin_key(
    connection_id: UUID,
    body: MCPBuiltinKeySave,
    user: Annotated[User, Depends(current_user)],
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[MCPService, Depends(mcp_service)],
    catalog: Annotated[BuiltinCatalog, Depends(builtin_catalog)],
) -> MCPConnectionOut:
    return MCPConnectionOut.from_domain(
        await service.save_builtin_key(
            workspace.id,
            connection_id,
            user.id,
            catalog,
            {name: value.get_secret_value() for name, value in body.inputs.items()},
        )
    )


@router.get(
    "/mcp/oauth/callback", status_code=303, response_class=RedirectResponse, responses=ERRORS
)
async def oauth_callback(
    request: Request,
    user: Annotated[User | None, Depends(session_user)],
    session: Annotated[str | None, Depends(session_hash)],
    access: Annotated[AdminAccess, Depends(admin_access)],
    service: Annotated[MCPService, Depends(mcp_service)],
    state: Annotated[str, Query(min_length=1, max_length=256)],
    code: Annotated[str | None, Query(max_length=8192)] = None,
    iss: Annotated[str | None, Query(max_length=2048)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> RedirectResponse:
    params = {"mcp": "authorization_failed"}
    destination = "/connectors"
    try:
        if user is None or session is None:
            raise MCPError("authorization_expired", 400)
        signed_in = user

        async def is_admin(workspace_id: UUID) -> bool:
            return await access.is_admin(signed_in, workspace_id)

        result = await service.callback(
            is_admin,
            user.id,
            session,
            state,
            code,
            iss,
            error,
        )
        destination = f"/workspaces/{result.workspace_id}/connectors/{result.connection_id}"
        params["mcp"] = result.outcome
    except MCPError:
        pass
    return RedirectResponse(
        f"{request.app.state.config.api.app_url}{destination}?{urlencode(params)}",
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.get("/mcp/client-metadata", responses=ERRORS)
async def client_metadata(
    service: Annotated[MCPService, Depends(mcp_service)],
) -> MCPClientMetadata:
    return MCPClientMetadata.model_validate(service.oauth.client_metadata())


@router.post("/workspaces/{workspace_id}/mcp-connections/{connection_id}/refresh", responses=ERRORS)
async def refresh_connection(
    connection_id: UUID,
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[MCPService, Depends(mcp_service)],
) -> MCPConnectionOut:
    return MCPConnectionOut.from_domain(await service.refresh_catalog(workspace.id, connection_id))


@router.post(
    "/workspaces/{workspace_id}/mcp-connections/{connection_id}/disconnect",
    responses=ERRORS,
)
async def disconnect_connection(
    connection_id: UUID,
    user: Annotated[User, Depends(current_user)],
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[MCPService, Depends(mcp_service)],
) -> MCPConnectionOut:
    await service.store.disconnect(workspace.id, connection_id, user.id)
    return MCPConnectionOut.from_domain(
        await service.store.get_connection(workspace.id, connection_id)
    )


@router.delete(
    "/workspaces/{workspace_id}/mcp-connections/{connection_id}", status_code=204, responses=ERRORS
)
async def delete_connection(
    connection_id: UUID,
    user: Annotated[User, Depends(current_user)],
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[MCPService, Depends(mcp_service)],
) -> Response:
    await service.store.delete_connection(workspace.id, connection_id, user.id)
    return Response(status_code=204)


@router.get("/workspaces/{workspace_id}/mcp-channels", responses=ERRORS)
async def list_channels(
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[MCPService, Depends(mcp_service)],
    cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
) -> MCPChannelsOut:
    return MCPChannelsOut.from_domain(await service.channels(workspace.id, cursor))
