from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Annotated, Any, Literal, Protocol
from uuid import UUID

import structlog
from fastapi import Depends, HTTPException, Request
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from hondo.api.deps import current_user
from hondo.api.store import ControlStore, TeamWorkspace, User, Workspace
from hondo.chat import ChannelInfo
from hondo.crypto import TokenCipher, TokenCipherError
from hondo.slack import SlackChatClient

logger = structlog.get_logger("hondo.api.access")

# How long Slack's answer about a person is reused before a decision asks again.
# It bounds how long a deactivated person or a demoted Slack admin keeps access.
CHECK_MAX_AGE = timedelta(minutes=10)
_SLACK_TIMEOUT_SECONDS = 5
# The no-access page names a few admins to ask; it does not need all of them.
_ADMIN_NAMES_LIMIT = 5


@dataclass(frozen=True, slots=True)
class SlackMember:
    id: str
    name: str
    admin: bool
    active: bool
    avatar_url: str | None = None
    real_name: str | None = None
    handle: str | None = None


@dataclass(frozen=True, slots=True)
class SlackMemberPage:
    members: list[SlackMember]
    next_cursor: str | None


class SlackDirectoryError(Exception):
    """Slack did not answer a directory request."""


class SlackTokenRejected(SlackDirectoryError):
    """Slack refused the workspace's bot token. Only a reinstall fixes this."""


class SlackRateLimited(SlackDirectoryError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("ratelimited")
        self.retry_after = retry_after


# Errors that mean the stored bot token no longer works, e.g. Hondo was removed
# from Slack. Unlike an outage they do not go away, so they must not keep the
# last known access open.
_TOKEN_ERRORS = frozenset(
    {
        "account_inactive",
        "invalid_auth",
        "missing_scope",
        "not_authed",
        "token_expired",
        "token_revoked",
    }
)


class SlackDirectory(Protocol):
    """The test seam for Slack's user directory."""

    async def member(self, token: str, team_id: str, slack_user_id: str) -> SlackMember | None:
        """The member as seen from the team, or None when Slack has no such user."""
        ...

    async def members(self, token: str, team_id: str, cursor: str | None) -> SlackMemberPage:
        """One page of the team's member list, as Slack sees each person."""
        ...

    async def channel(self, token: str, channel: str) -> ChannelInfo | None:
        """The channel as the agent sees it, or None when Slack did not answer."""
        ...


def _in_team(payload: Mapping[str, Any], team_id: str) -> bool:
    # In an Enterprise org, users.info also answers for people who left this
    # team but stay in another one; only a listed membership counts.
    if payload.get("team_id") == team_id:
        return True
    enterprise = payload.get("enterprise_user")
    teams = enterprise.get("teams") if isinstance(enterprise, Mapping) else None
    return isinstance(teams, list) and team_id in teams


def _text(value: object) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def member_from_payload(payload: Any, team_id: str) -> SlackMember:
    if not isinstance(payload, Mapping) or not isinstance(payload.get("id"), str):
        raise SlackDirectoryError("users.info returned no user")
    profile = payload.get("profile")
    profile = profile if isinstance(profile, Mapping) else {}
    real_name = _text(payload.get("real_name"))
    handle = _text(payload.get("name"))
    avatar = next(
        (
            value
            for value in (profile.get("image_72"), profile.get("image_48"))
            if isinstance(value, str) and value
        ),
        None,
    )
    return SlackMember(
        id=payload["id"],
        name=_text(profile.get("display_name")) or real_name or handle or payload["id"],
        avatar_url=avatar,
        real_name=real_name,
        handle=handle,
        admin=any(
            payload.get(flag) is True for flag in ("is_admin", "is_owner", "is_primary_owner")
        ),
        active=payload["id"] != "USLACKBOT"
        and _in_team(payload, team_id)
        and not any(
            payload.get(flag) is True
            for flag in ("deleted", "is_bot", "is_restricted", "is_ultra_restricted")
        ),
    )


def _directory_error(exc: SlackApiError) -> SlackDirectoryError:
    error = exc.response.get("error")
    if error in _TOKEN_ERRORS:
        return SlackTokenRejected(error)
    if exc.response.status_code == 429:
        headers = {key.lower(): value for key, value in exc.response.headers.items()}
        try:
            retry_after = max(1, int(headers.get("retry-after", "60")))
        except (TypeError, ValueError):
            retry_after = 60
        return SlackRateLimited(retry_after)
    return SlackDirectoryError(error or "slack error")


class SlackWebDirectory:
    def __init__(self) -> None:
        self._chat: tuple[str, SlackChatClient] | None = None

    async def channel(self, token: str, channel: str) -> ChannelInfo | None:
        if self._chat is None or self._chat[0] != token:
            web = AsyncWebClient(token=token, timeout=_SLACK_TIMEOUT_SECONDS)
            self._chat = (token, SlackChatClient(web))
        return await self._chat[1].channel_info(channel)

    async def member(self, token: str, team_id: str, slack_user_id: str) -> SlackMember | None:
        client = AsyncWebClient(token=token, timeout=_SLACK_TIMEOUT_SECONDS)
        try:
            async with asyncio.timeout(_SLACK_TIMEOUT_SECONDS):
                response = await client.users_info(user=slack_user_id)
        except SlackApiError as exc:
            if exc.response.get("error") == "user_not_found":
                return None
            raise _directory_error(exc) from exc
        except Exception as exc:
            raise SlackDirectoryError(type(exc).__name__) from exc
        return member_from_payload(response.get("user"), team_id)

    async def members(self, token: str, team_id: str, cursor: str | None) -> SlackMemberPage:
        client = AsyncWebClient(token=token, timeout=_SLACK_TIMEOUT_SECONDS)
        try:
            async with asyncio.timeout(_SLACK_TIMEOUT_SECONDS):
                response = await client.users_list(limit=200, cursor=cursor)
        except SlackApiError as exc:
            raise _directory_error(exc) from exc
        except Exception as exc:
            raise SlackDirectoryError(type(exc).__name__) from exc
        payload = response.get("members")
        if not isinstance(payload, list):
            raise SlackDirectoryError("users.list returned no members")
        metadata = response.get("response_metadata")
        next_cursor = metadata.get("next_cursor") if isinstance(metadata, Mapping) else None
        return SlackMemberPage(
            members=[member_from_payload(item, team_id) for item in payload],
            next_cursor=next_cursor if isinstance(next_cursor, str) and next_cursor else None,
        )


# "ok": Slack answered. "reinstall_required": Slack refused Hondo's token.
# "unavailable": Slack did not answer, so access is refused for now.
SlackState = Literal["ok", "reinstall_required", "unavailable"]


@dataclass(frozen=True, slots=True)
class TeamAccess:
    team_id: str
    team_name: str
    workspace_id: UUID | None
    is_admin: bool
    admins: list[str]
    slack: SlackState


@dataclass(frozen=True, slots=True)
class _Answer:
    at: float
    member: SlackMember | None


class SlackMemberCache:
    """Slack's recent answers about people, per API process.

    Nothing about a person's Slack role is kept in the database. A decision
    uses an answer younger than CHECK_MAX_AGE or asks Slack again, and it fails
    closed when Slack does not answer.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._answers: dict[tuple[str, str], _Answer] = {}

    def get(self, team_id: str, slack_user_id: str) -> _Answer | None:
        answer = self._answers.get((team_id, slack_user_id))
        if answer is None or self._clock() - answer.at >= CHECK_MAX_AGE.total_seconds():
            return None
        return answer

    def put(self, team_id: str, slack_user_id: str, member: SlackMember | None) -> None:
        self._answers[(team_id, slack_user_id)] = _Answer(at=self._clock(), member=member)

    def forget(self, team_id: str, slack_user_id: str) -> None:
        self._answers.pop((team_id, slack_user_id), None)


@dataclass(frozen=True, slots=True)
class _Decision:
    team: TeamWorkspace | None
    admin: bool = False
    # Slack confirmed an active member of the team; only they see whom to ask.
    member: bool = False
    slack: SlackState = "ok"


class AdminAccess:
    """Decides who administers a workspace.

    An admin is an active member of the workspace's Slack team who is on its
    admin list or is a Slack owner or admin. A user is one Slack account, so
    they can administer at most one workspace: their own team's.
    """

    def __init__(
        self,
        store: ControlStore,
        cipher: TokenCipher,
        directory: SlackDirectory,
        cache: SlackMemberCache,
    ) -> None:
        self.store = store
        self.cipher = cipher
        self.directory = directory
        self.cache = cache

    def _token(self, bot_token_encrypted: str) -> str:
        try:
            return self.cipher.decrypt(bot_token_encrypted)
        except TokenCipherError as exc:
            raise SlackDirectoryError("bot token could not be decrypted") from exc

    async def lookup(
        self, bot_token_encrypted: str, team_id: str, slack_user_id: str
    ) -> SlackMember | None:
        """Asks Slack now and keeps the answer. Raises when Slack can't answer."""

        member = await self.directory.member(
            self._token(bot_token_encrypted), team_id, slack_user_id
        )
        self.cache.put(team_id, slack_user_id, member)
        return member

    async def member(self, team: TeamWorkspace, slack_user_id: str) -> SlackMember | None:
        """Slack's answer from the last CHECK_MAX_AGE, or a new one."""

        team_id = team.workspace.slack_team_id
        answer = self.cache.get(team_id, slack_user_id)
        if answer is not None:
            return answer.member
        return await self.lookup(team.bot_token_encrypted, team_id, slack_user_id)

    async def member_page(self, team: TeamWorkspace, cursor: str | None) -> SlackMemberPage:
        return await self.directory.members(
            self._token(team.bot_token_encrypted), team.workspace.slack_team_id, cursor
        )

    async def channels(
        self, team: TeamWorkspace, channels: set[str]
    ) -> dict[str, ChannelInfo | None]:
        """What Slack says about each channel now. Unknown channels map to None."""

        try:
            token = self._token(team.bot_token_encrypted)
        except SlackDirectoryError:
            return dict.fromkeys(channels)
        ordered = sorted(channels)
        infos = await asyncio.gather(*(self.directory.channel(token, item) for item in ordered))
        return dict(zip(ordered, infos, strict=True))

    async def _decide(self, user: User) -> _Decision:
        team = await self.store.team_workspace(user.slack_team_id)
        if team is None:
            return _Decision(team=None)
        try:
            member = await self.member(team, user.slack_user_id)
        except SlackTokenRejected as exc:
            logger.warning("slack rejected the workspace token", error=str(exc))
            return _Decision(team=team, slack="reinstall_required")
        except SlackDirectoryError as exc:
            logger.warning("slack status check failed", error=str(exc))
            return _Decision(team=team, slack="unavailable")
        if member is None or not member.active:
            return _Decision(team=team)
        admin = member.admin or await self.store.is_listed_admin(
            team.workspace.id, user.slack_user_id
        )
        return _Decision(team=team, admin=admin, member=True)

    async def administered(self, user: User) -> TeamWorkspace | None:
        """The workspace this user administers, with its bot token, if any."""

        decision = await self._decide(user)
        return decision.team if decision.admin else None

    async def workspace(self, user: User) -> Workspace | None:
        """The workspace this user administers, if any."""

        team = await self.administered(user)
        return team.workspace if team is not None else None

    async def is_admin(self, user: User, workspace_id: UUID) -> bool:
        workspace = await self.workspace(user)
        return workspace is not None and workspace.id == workspace_id

    async def team(self, user: User) -> TeamAccess:
        decision = await self._decide(user)
        team = decision.team
        admins: list[str] = []
        if team is not None and decision.member and not decision.admin:
            admins = await self._admin_names(team)
        return TeamAccess(
            team_id=user.slack_team_id,
            # The install stored the name Slack gave it, which sign-in may lack.
            team_name=team.workspace.name if team is not None else user.slack_team_name,
            workspace_id=team.workspace.id if team is not None else None,
            is_admin=decision.admin,
            admins=admins,
            slack=decision.slack,
        )

    async def _admin_names(self, team: TeamWorkspace) -> list[str]:
        names: list[str] = []
        for slack_user_id in await self.store.list_admin_ids(team.workspace.id):
            try:
                member = await self.member(team, slack_user_id)
            except SlackDirectoryError as exc:
                logger.warning("slack admin lookup failed", error=str(exc))
                break
            if member is not None and member.active:
                names.append(member.name)
            if len(names) == _ADMIN_NAMES_LIMIT:
                break
        return names


def admin_access(request: Request) -> AdminAccess:
    return AdminAccess(
        request.app.state.store,
        request.app.state.provider_cipher,
        request.app.state.slack_directory,
        request.app.state.slack_members,
    )


async def team_admin(
    workspace_id: UUID,
    user: Annotated[User, Depends(current_user)],
    access: Annotated[AdminAccess, Depends(admin_access)],
) -> TeamWorkspace:
    """The workspace in the path with its bot token, for its admins only."""

    team = await access.administered(user)
    if team is None or team.workspace.id != workspace_id:
        raise HTTPException(status_code=404, detail="not_found")
    return team


async def workspace_admin(
    workspace_id: UUID,
    user: Annotated[User, Depends(current_user)],
    access: Annotated[AdminAccess, Depends(admin_access)],
) -> Workspace:
    """The workspace in the path, for its admins only; everyone else gets a 404."""

    return (await team_admin(workspace_id, user, access)).workspace
