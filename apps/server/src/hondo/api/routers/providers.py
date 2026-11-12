from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from hondo.api.access import workspace_admin
from hondo.api.provider_connections import PROVIDERS, ProviderConnections
from hondo.api.schemas import (
    ApiErrorResponse,
    ProviderConnectionCreate,
    ProviderConnectionOut,
    ProviderConnectionUpdate,
    ProviderModelsResponse,
    ProvidersResponse,
)
from hondo.api.store import Workspace

router = APIRouter()


def provider_connections(request: Request) -> ProviderConnections:
    return ProviderConnections(
        request.app.state.store,
        request.app.state.provider_cipher,
        request.app.state.provider_client,
        runtime_schema=request.app.state.config.runtime.system_schema,
    )


_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ApiErrorResponse, "description": "Sign in required."},
    404: {"model": ApiErrorResponse, "description": "Not found."},
    400: {"model": ApiErrorResponse, "description": "Key rejected."},
    409: {
        "model": ApiErrorResponse,
        "description": "Connection unavailable, in use, or name already taken.",
    },
    503: {"model": ApiErrorResponse, "description": "Try again later."},
}


@router.get(
    "/workspaces/{workspace_id}/providers",
    responses={
        401: {"model": ApiErrorResponse, "description": "Sign in required."},
        404: {"model": ApiErrorResponse, "description": "Not found."},
    },
)
async def list_providers(
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[ProviderConnections, Depends(provider_connections)],
) -> ProvidersResponse:
    return ProvidersResponse.from_domain(
        PROVIDERS, await service.store.list_provider_connections(workspace.id)
    )


@router.get(
    "/workspaces/{workspace_id}/provider-connections/{connection_id}/models",
    responses=_ERRORS,
)
async def list_models(
    connection_id: UUID,
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[ProviderConnections, Depends(provider_connections)],
) -> ProviderModelsResponse:
    return ProviderModelsResponse.from_domain(await service.models(workspace.id, connection_id))


@router.post(
    "/workspaces/{workspace_id}/provider-connections",
    status_code=201,
    responses=_ERRORS,
)
async def create_connection(
    body: ProviderConnectionCreate,
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[ProviderConnections, Depends(provider_connections)],
) -> ProviderConnectionOut:
    return ProviderConnectionOut.from_domain(
        await service.create(workspace.id, body.provider, body.name, body.api_key)
    )


@router.patch(
    "/workspaces/{workspace_id}/provider-connections/{connection_id}",
    responses=_ERRORS,
)
async def update_connection(
    connection_id: UUID,
    body: ProviderConnectionUpdate,
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[ProviderConnections, Depends(provider_connections)],
) -> ProviderConnectionOut:
    return ProviderConnectionOut.from_domain(
        await service.update(workspace.id, connection_id, body.name, body.api_key)
    )


@router.post(
    "/workspaces/{workspace_id}/provider-connections/{connection_id}/check",
    status_code=204,
    responses=_ERRORS,
)
async def check_connection(
    connection_id: UUID,
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[ProviderConnections, Depends(provider_connections)],
) -> None:
    await service.check(workspace.id, connection_id)


@router.delete(
    "/workspaces/{workspace_id}/provider-connections/{connection_id}",
    status_code=204,
    responses=_ERRORS,
)
async def delete_connection(
    connection_id: UUID,
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[ProviderConnections, Depends(provider_connections)],
) -> Response:
    await service.disconnect(workspace.id, connection_id)
    return Response(status_code=204)
