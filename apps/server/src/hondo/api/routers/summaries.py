from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Path, Query

from hondo.api.access import AdminAccess, admin_access, team_admin, workspace_admin
from hondo.api.deps import control_store
from hondo.api.schemas import ApiErrorResponse, ThreadSummariesOut, ThreadSummaryQuery
from hondo.api.store import ControlStore, TeamWorkspace, Workspace
from hondo.chat import is_public

router = APIRouter()
ERRORS: dict[int | str, dict[str, Any]] = {
    code: {"model": ApiErrorResponse} for code in (401, 404, 422)
}
PAGE_SIZE = 25
SEARCH_LIMIT = 50


@router.get("/workspaces/{workspace_id}/thread-summaries", responses=ERRORS)
async def list_thread_summaries(
    query: Annotated[ThreadSummaryQuery, Query()],
    team: Annotated[TeamWorkspace, Depends(team_admin)],
    store: Annotated[ControlStore, Depends(control_store)],
    access: Annotated[AdminAccess, Depends(admin_access)],
) -> ThreadSummariesOut:
    """The newest summaries first, or the best matches of a search. Text and
    thread links show only for public channels."""
    team_id = team.workspace.slack_team_id
    if query.words:
        found = await store.search_thread_summaries(team_id, query.words, SEARCH_LIMIT)
        channels = await access.channels(team, {summary.channel for summary in found})
        visible = [summary for summary in found if is_public(channels[summary.channel])]
        return ThreadSummariesOut.from_domain(visible, channels, next_offset=None)
    page = await store.list_thread_summaries(team_id, query.offset, PAGE_SIZE)
    channels = await access.channels(
        team, {summary.channel for summary in page.summaries if not summary.private}
    )
    return ThreadSummariesOut.from_domain(
        page.summaries, channels, next_offset=query.offset + PAGE_SIZE if page.more else None
    )


@router.delete(
    "/workspaces/{workspace_id}/thread-summaries/{summary_id}", status_code=204, responses=ERRORS
)
async def delete_thread_summary(
    summary_id: Annotated[int, Path(ge=1, le=2**63 - 1)],
    workspace: Annotated[Workspace, Depends(workspace_admin)],
    store: Annotated[ControlStore, Depends(control_store)],
) -> None:
    if not await store.delete_thread_summary(workspace.slack_team_id, summary_id):
        raise HTTPException(status_code=404, detail="summary_not_found")
