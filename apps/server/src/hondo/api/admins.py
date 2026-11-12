from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

import structlog

from hondo.api.access import AdminAccess, SlackDirectoryError, SlackMember, SlackMemberPage
from hondo.api.store import AdminCaller, AdminRemoval, ControlStore, TeamWorkspace, User

logger = structlog.get_logger("hondo.api.admins")

type AdminStatus = Literal["active", "inactive", "unknown"]
type AdminAddition = Literal["added", "member_unavailable", "no_access"]


@dataclass(frozen=True, slots=True)
class Admin:
    slack_user_id: str
    name: str | None
    avatar_url: str | None
    status: AdminStatus
    added_by: str | None
    created_at: datetime


class WorkspaceAdmins:
    """The workspace's admin list, with each person as Slack sees them."""

    def __init__(self, store: ControlStore, access: AdminAccess) -> None:
        self.store = store
        self.access = access

    async def list(self, team: TeamWorkspace) -> list[Admin]:
        admins: list[Admin] = []
        slack_answers = True
        for row in await self.store.list_admins(team.workspace.id):
            member: SlackMember | None = None
            status: AdminStatus = "unknown"
            if slack_answers:
                try:
                    member = await self.access.member(team, row.slack_user_id)
                    status = "active" if member is not None and member.active else "inactive"
                except SlackDirectoryError as exc:
                    # One timeout is enough: the remaining rows use only answers
                    # Slack already gave, instead of waiting on Slack each time.
                    logger.warning("slack admin lookup failed", error=str(exc))
                    slack_answers = False
            else:
                answer = self.access.cache.get(team.workspace.slack_team_id, row.slack_user_id)
                if answer is not None:
                    member = answer.member
                    status = "active" if member is not None and member.active else "inactive"
            admins.append(
                Admin(
                    slack_user_id=row.slack_user_id,
                    name=member.name if member is not None else None,
                    avatar_url=member.avatar_url if member is not None else None,
                    status=status,
                    added_by=row.added_by,
                    created_at=row.created_at,
                )
            )
        return admins

    async def members(self, team: TeamWorkspace, cursor: str | None) -> SlackMemberPage:
        page = await self.access.member_page(team, cursor)
        return SlackMemberPage(
            members=[member for member in page.members if member.active],
            next_cursor=page.next_cursor,
        )

    async def add(self, team: TeamWorkspace, slack_user_id: str, caller: User) -> AdminAddition:
        member = await self.access.lookup(
            team.bot_token_encrypted, team.workspace.slack_team_id, slack_user_id
        )
        if member is None or not member.active:
            return "member_unavailable"
        acting = await self._caller(team, caller)
        if acting is None:
            return "no_access"
        added = await self.store.add_admin(team.workspace.id, slack_user_id, acting, caller.id)
        return "added" if added else "no_access"

    async def remove(self, team: TeamWorkspace, slack_user_id: str, caller: User) -> AdminRemoval:
        acting = await self._caller(team, caller)
        if acting is None:
            return "no_access"
        return await self.store.remove_admin(team.workspace.id, slack_user_id, acting)

    async def _caller(self, team: TeamWorkspace, caller: User) -> AdminCaller | None:
        """The caller as Slack sees them now, or None when they are no longer active."""

        # Usually cached by the access check, but it can expire mid-request.
        member = await self.access.member(team, caller.slack_user_id)
        if member is None or not member.active:
            return None
        return AdminCaller(slack_user_id=caller.slack_user_id, slack_admin=member.admin)
