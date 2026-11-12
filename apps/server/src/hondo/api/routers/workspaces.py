from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from hondo.api.access import AdminAccess, admin_access, workspace_admin
from hondo.api.deps import control_store, current_user
from hondo.api.provider_connections import ProviderConnections
from hondo.api.routers.providers import provider_connections
from hondo.api.schemas import (
    ActivityQuery,
    ActivityResponse,
    SetupResponse,
    UsageQuery,
    UsageResponse,
    WorkspacesResponse,
)
from hondo.api.store import ControlStore, User, Workspace
from hondo.usage import rate_usage_rows

router = APIRouter()


@router.get(
    "/workspaces",
    responses={401: {"description": "Unauthorized"}},
)
async def list_workspaces(
    user: Annotated[User, Depends(current_user)],
    access: Annotated[AdminAccess, Depends(admin_access)],
) -> WorkspacesResponse:
    workspace = await access.workspace(user)
    return WorkspacesResponse.from_domain([] if workspace is None else [workspace])


@router.get(
    "/workspaces/{workspace_id}/usage",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Workspace not found"},
    },
)
async def workspace_usage(
    request: Request,
    query: Annotated[UsageQuery, Query()],
    workspace: Annotated[Workspace, Depends(workspace_admin)],
) -> UsageResponse:
    store: ControlStore = request.app.state.store
    start, end = query.window
    rows = await store.list_model_usage(workspace.slack_team_id, start, end)
    return UsageResponse.from_domain(workspace.id, query.month, rate_usage_rows(rows))


@router.get(
    "/workspaces/{workspace_id}/activity",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Workspace not found"},
    },
)
async def workspace_activity(
    request: Request,
    query: Annotated[ActivityQuery, Query()],
    workspace: Annotated[Workspace, Depends(workspace_admin)],
) -> ActivityResponse:
    store: ControlStore = request.app.state.store
    items = await store.list_workspace_activity(workspace.slack_team_id, query.limit)
    return ActivityResponse.from_domain(items)


@router.get(
    "/workspaces/{workspace_id}/setup",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Workspace not found"},
    },
)
async def get_workspace_setup(
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    store: Annotated[ControlStore, Depends(control_store)],
    providers: Annotated[ProviderConnections, Depends(provider_connections)],
) -> SetupResponse:
    facts = await store.setup_facts(workspace.id, workspace.slack_team_id)
    selection = await store.get_agent_model(workspace.id)
    return SetupResponse.from_domain(facts, model_selected=await providers.model_ready(selection))
