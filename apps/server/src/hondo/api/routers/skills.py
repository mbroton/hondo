from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends

from hondo.api.access import workspace_admin
from hondo.api.deps import control_store, current_user
from hondo.api.schemas import ApiErrorResponse, SkillCreate, SkillOut, SkillsOut, SkillUpdate
from hondo.api.store import ControlStore, User, Workspace

router = APIRouter()
ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ApiErrorResponse} for code in (401, 404, 409, 422, 429, 503)
}


@router.get("/workspaces/{workspace_id}/skills", responses=ERRORS)
async def list_skills(
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    store: Annotated[ControlStore, Depends(control_store)],
) -> SkillsOut:
    return SkillsOut.from_domain(await store.list_skills(workspace.id))


@router.post("/workspaces/{workspace_id}/skills", status_code=201, responses=ERRORS)
async def create_skill(
    body: SkillCreate,
    user: Annotated[User, Depends(current_user)],
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    store: Annotated[ControlStore, Depends(control_store)],
) -> SkillOut:
    return SkillOut.from_domain(await store.create_skill(workspace.id, body.content, user.id))


@router.get("/workspaces/{workspace_id}/skills/{skill_id}", responses=ERRORS)
async def get_skill(
    skill_id: UUID,
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    store: Annotated[ControlStore, Depends(control_store)],
) -> SkillOut:
    return SkillOut.from_domain(await store.get_skill(workspace.id, skill_id))


@router.put("/workspaces/{workspace_id}/skills/{skill_id}", responses=ERRORS)
async def update_skill(
    skill_id: UUID,
    body: SkillUpdate,
    user: Annotated[User, Depends(current_user)],
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    store: Annotated[ControlStore, Depends(control_store)],
) -> SkillOut:
    return SkillOut.from_domain(
        await store.update_skill(workspace.id, skill_id, user.id, **body.model_dump())
    )


@router.delete("/workspaces/{workspace_id}/skills/{skill_id}", status_code=204, responses=ERRORS)
async def delete_skill(
    skill_id: UUID,
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    store: Annotated[ControlStore, Depends(control_store)],
) -> None:
    await store.delete_skill(workspace.id, skill_id)
