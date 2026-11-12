from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from hondo.api.access import workspace_admin
from hondo.api.deps import control_store
from hondo.api.provider_connections import ProviderConnections
from hondo.api.routers.providers import provider_connections
from hondo.api.schemas import (
    AgentModelRequest,
    AgentModelResponse,
    ApiErrorResponse,
    ListeningChannels,
    MentionOnlyChannels,
)
from hondo.api.store import ControlStore, Workspace

router = APIRouter()

_READ_ERRORS: dict[int | str, dict[str, Any]] = {
    401: {"model": ApiErrorResponse, "description": "Sign in required."},
    404: {"model": ApiErrorResponse, "description": "Not found."},
}
_CHANNEL_ERRORS: dict[int | str, dict[str, Any]] = {
    **_READ_ERRORS,
    409: {
        "model": ApiErrorResponse,
        "description": "A channel cannot be mention-only and listening at the same time.",
    },
    422: {"model": ApiErrorResponse},
}
_WRITE_ERRORS: dict[int | str, dict[str, Any]] = {
    **_READ_ERRORS,
    400: {"model": ApiErrorResponse, "description": "Model is unavailable."},
    409: {"model": ApiErrorResponse, "description": "The requested model source is unavailable."},
    503: {"model": ApiErrorResponse, "description": "Try again later."},
}


@router.get("/workspaces/{workspace_id}/agent/model", responses=_READ_ERRORS)
async def get_model(
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[ProviderConnections, Depends(provider_connections)],
) -> AgentModelResponse:
    selection = await service.store.get_agent_model(workspace.id)
    return AgentModelResponse.from_domain(selection, ready=await service.model_ready(selection))


@router.put("/workspaces/{workspace_id}/agent/model", status_code=204, responses=_WRITE_ERRORS)
async def set_model(
    body: AgentModelRequest,
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[ProviderConnections, Depends(provider_connections)],
) -> None:
    await service.select_model(workspace.id, body.connection_id, body.model_name)


@router.delete("/workspaces/{workspace_id}/agent/model", status_code=204, responses=_WRITE_ERRORS)
async def clear_model(
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    service: Annotated[ProviderConnections, Depends(provider_connections)],
) -> None:
    await service.clear_model(workspace.id)


@router.get("/workspaces/{workspace_id}/agent/mention-only", responses=_READ_ERRORS)
async def get_mention_only(
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    store: Annotated[ControlStore, Depends(control_store)],
) -> MentionOnlyChannels:
    return MentionOnlyChannels.from_domain(await store.get_mention_only(workspace.id))


@router.put(
    "/workspaces/{workspace_id}/agent/mention-only",
    status_code=204,
    responses=_CHANNEL_ERRORS,
)
async def set_mention_only(
    body: MentionOnlyChannels,
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    store: Annotated[ControlStore, Depends(control_store)],
) -> None:
    if not await store.set_mention_only(workspace.id, body.channels):
        raise HTTPException(status_code=409, detail="channel_conflict")


@router.get("/workspaces/{workspace_id}/agent/listening", responses=_READ_ERRORS)
async def get_listening(
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    store: Annotated[ControlStore, Depends(control_store)],
) -> ListeningChannels:
    return ListeningChannels.from_domain(await store.get_listening(workspace.id))


@router.put(
    "/workspaces/{workspace_id}/agent/listening",
    status_code=204,
    responses=_CHANNEL_ERRORS,
)
async def set_listening(
    body: ListeningChannels,
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    store: Annotated[ControlStore, Depends(control_store)],
) -> None:
    if not await store.set_listening(workspace.id, body.to_domain()):
        raise HTTPException(status_code=409, detail="channel_conflict")
