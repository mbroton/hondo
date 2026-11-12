from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response

from hondo.api.access import AdminAccess, admin_access, team_admin
from hondo.api.admins import WorkspaceAdmins
from hondo.api.deps import control_store, current_user
from hondo.api.schemas import (
    SLACK_USER_ID_PATTERN,
    AdminAddRequest,
    AdminsResponse,
    ApiErrorResponse,
    SlackMembersResponse,
)
from hondo.api.store import ControlStore, TeamWorkspace, User

router = APIRouter()


def workspace_admins(
    store: Annotated[ControlStore, Depends(control_store)],
    access: Annotated[AdminAccess, Depends(admin_access)],
) -> WorkspaceAdmins:
    return WorkspaceAdmins(store, access)


_NOT_FOUND: dict[int | str, dict[str, Any]] = {
    401: {"model": ApiErrorResponse, "description": "Sign in required."},
    404: {"model": ApiErrorResponse, "description": "Not found."},
}
_SLACK_UNAVAILABLE: dict[int | str, dict[str, Any]] = {
    429: {"model": ApiErrorResponse, "description": "Slack is limiting requests; see Retry-After."},
    503: {"model": ApiErrorResponse, "description": "Slack did not answer. Try again later."},
}


@router.get("/workspaces/{workspace_id}/admins", responses=_NOT_FOUND)
async def list_admins(
    team: Annotated[TeamWorkspace, Depends(team_admin)],
    admins: Annotated[WorkspaceAdmins, Depends(workspace_admins)],
) -> AdminsResponse:
    return AdminsResponse.from_domain(await admins.list(team))


@router.get(
    "/workspaces/{workspace_id}/slack-members",
    responses={**_NOT_FOUND, **_SLACK_UNAVAILABLE},
)
async def list_slack_members(
    team: Annotated[TeamWorkspace, Depends(team_admin)],
    admins: Annotated[WorkspaceAdmins, Depends(workspace_admins)],
    cursor: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
) -> SlackMembersResponse:
    return SlackMembersResponse.from_domain(await admins.members(team, cursor))


@router.post(
    "/workspaces/{workspace_id}/admins",
    status_code=204,
    responses={
        **_NOT_FOUND,
        **_SLACK_UNAVAILABLE,
        400: {"model": ApiErrorResponse, "description": "Not an active member of the team."},
    },
)
async def add_admin(
    body: AdminAddRequest,
    team: Annotated[TeamWorkspace, Depends(team_admin)],
    user: Annotated[User, Depends(current_user)],
    admins: Annotated[WorkspaceAdmins, Depends(workspace_admins)],
) -> Response:
    match await admins.add(team, body.slack_user_id, user):
        case "member_unavailable":
            raise HTTPException(status_code=400, detail="member_unavailable")
        case "no_access":
            raise HTTPException(status_code=404, detail="not_found")
        case "added":
            return Response(status_code=204)


@router.delete(
    "/workspaces/{workspace_id}/admins/{slack_user_id}",
    status_code=204,
    responses={
        **_NOT_FOUND,
        **_SLACK_UNAVAILABLE,
        409: {"model": ApiErrorResponse, "description": "The last listed admin stays."},
    },
)
async def remove_admin(
    slack_user_id: Annotated[str, Path(max_length=32, pattern=SLACK_USER_ID_PATTERN)],
    team: Annotated[TeamWorkspace, Depends(team_admin)],
    user: Annotated[User, Depends(current_user)],
    admins: Annotated[WorkspaceAdmins, Depends(workspace_admins)],
) -> Response:
    match await admins.remove(team, slack_user_id, user):
        case "not_listed" | "no_access":
            raise HTTPException(status_code=404, detail="not_found")
        case "last_admin":
            raise HTTPException(status_code=409, detail="last_admin")
        case "removed":
            return Response(status_code=204)
