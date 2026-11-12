from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Literal
from uuid import UUID

import asyncpg
from dbos import DBOSClient

from hondo.agent_settings import DEFAULT_AGENT_NAME, AgentModel
from hondo.skills import SKILLS_PER_WORKSPACE, SkillError, parse_skill
from hondo.store.base import SummaryView
from hondo.store.migrate import upgrade_to_head
from hondo.store.postgres import search_summary_rows


@dataclass(frozen=True, slots=True)
class User:
    """One Slack account. The email is profile data, never a link between accounts."""

    id: UUID
    email: str
    display_name: str
    avatar_url: str | None
    slack_team_id: str
    slack_user_id: str
    slack_team_name: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Workspace:
    id: UUID
    slack_team_id: str
    name: str
    status: str


@dataclass(frozen=True, slots=True)
class SlackStatus:
    """What Slack said about a person just now."""

    admin: bool
    active: bool


@dataclass(frozen=True, slots=True)
class TeamWorkspace:
    workspace: Workspace
    bot_token_encrypted: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class WorkspaceAdmin:
    slack_user_id: str
    # Display name of the admin who added this one; None for the installer.
    added_by: str | None
    created_at: datetime


type AdminRemoval = Literal["removed", "not_listed", "last_admin", "no_access"]


@dataclass(frozen=True, slots=True)
class AdminCaller:
    """The admin making a change, checked again under the workspace lock."""

    slack_user_id: str
    # Slack owners and admins have access without being listed.
    slack_admin: bool


@dataclass(frozen=True, slots=True)
class WorkspaceActivity:
    channel: str
    thread_root: str
    state: str
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class SetupFacts:
    provider_connected: bool
    tool_connected: bool
    # Only a run that succeeds saves messages; a failed run and the "choose a
    # model" notice save none.
    answered_in_channel: bool


@dataclass(frozen=True, slots=True)
class ProviderConnection:
    id: UUID
    name: str
    provider: str
    api_key_encrypted: str = field(repr=False)
    check_status: Literal["valid", "rejected"]
    checked_at: datetime
    updated_at: datetime


class ProviderConnectionNameTaken(Exception):
    pass


@dataclass(frozen=True, slots=True)
class WorkspaceInstalled:
    workspace_id: UUID
    created: bool


@dataclass(frozen=True, slots=True)
class InstallRefused:
    """The workspace exists and the installer is not one of its admins."""


@dataclass(frozen=True, slots=True)
class OtherWorkspaceInstalled:
    """The deployment already serves another Slack workspace."""


type InstallResult = WorkspaceInstalled | InstallRefused | OtherWorkspaceInstalled


@dataclass(frozen=True, slots=True)
class Skill:
    id: UUID
    name: str
    description: str
    content: str
    published: bool
    all_channels: bool
    channels: tuple[str, ...]
    # The Slack thread that Hondo drafted the skill from.
    source_channel: str | None
    source_ts: str | None
    revision: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredSummary:
    id: int
    channel: str
    thread_ts: str
    private: bool
    text: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class StoredSummaryPage:
    summaries: list[StoredSummary]
    more: bool


def _summary_from_record(record: asyncpg.Record) -> StoredSummary:
    return StoredSummary(
        id=record["id"],
        channel=record["channel"],
        thread_ts=record["thread_ts"],
        private=record["private"],
        text=record["text"],
        updated_at=record["updated_at"],
    )


def _user_from_record(record: asyncpg.Record) -> User:
    return User(
        id=record["id"],
        email=record["email"],
        display_name=record["display_name"],
        avatar_url=record["avatar_url"],
        slack_team_id=record["slack_team_id"],
        slack_user_id=record["slack_user_id"],
        slack_team_name=record["slack_team_name"],
        created_at=record["created_at"],
        updated_at=record["updated_at"],
    )


def _workspace_from_record(record: asyncpg.Record) -> Workspace:
    return Workspace(
        id=record["id"],
        slack_team_id=record["slack_team_id"],
        name=record["name"],
        status=record["status"],
    )


def _skill_from_record(record: asyncpg.Record) -> Skill:
    return Skill(
        id=record["id"],
        name=record["name"],
        description=record["description"],
        content=record["content"],
        published=record["published"],
        all_channels=record["all_channels"],
        channels=tuple(record["channels"]),
        source_channel=record["source_channel"],
        source_ts=record["source_ts"],
        revision=record["revision"],
        updated_at=record["updated_at"],
    )


class ControlStore:
    """API-owned Postgres access, deliberately outside the runtime Store protocol.

    The API and runtime never share connection pools; Postgres is their only seam.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None

    @property
    def dsn(self) -> str:
        return self._dsn

    @property
    def _connected_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("ControlStore is not connected; call connect() first")
        return self._pool

    async def connect(
        self,
        *,
        run_migrations: bool = True,
        pool_min_size: int = 2,
        pool_max_size: int = 5,
    ) -> None:
        if self._pool is not None:
            return
        pool = await asyncpg.create_pool(
            self._dsn,
            min_size=pool_min_size,
            max_size=pool_max_size,
        )
        # A failed connect must leave the store unconnected: the caller's
        # cleanup only runs after connect() returns, so the pool would leak.
        try:
            if run_migrations:
                await asyncio.to_thread(upgrade_to_head, self._dsn)
        except BaseException:
            await pool.close()
            raise
        self._pool = pool

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def ping(self) -> None:
        await self._connected_pool.execute("SELECT 1")

    async def list_provider_connections(self, workspace_id: UUID) -> list[ProviderConnection]:
        records = await self._connected_pool.fetch(
            "SELECT * FROM provider_connections WHERE workspace_id = $1 ORDER BY lower(name), id",
            workspace_id,
        )
        return [self._provider_connection(row) for row in records]

    @staticmethod
    def _provider_connection(row: asyncpg.Record) -> ProviderConnection:
        return ProviderConnection(
            id=row["id"],
            name=row["name"],
            provider=row["provider"],
            api_key_encrypted=row["api_key_encrypted"],
            check_status=row["check_status"],
            checked_at=row["checked_at"],
            updated_at=row["updated_at"],
        )

    async def get_provider_connection(
        self, workspace_id: UUID, connection_id: UUID
    ) -> ProviderConnection | None:
        row = await self._connected_pool.fetchrow(
            "SELECT * FROM provider_connections WHERE workspace_id = $1 AND id = $2",
            workspace_id,
            connection_id,
        )
        return self._provider_connection(row) if row else None

    async def create_provider_connection(
        self, workspace_id: UUID, provider: str, name: str, api_key_encrypted: str
    ) -> ProviderConnection:
        try:
            row = await self._connected_pool.fetchrow(
                """
                INSERT INTO provider_connections
                    (workspace_id, provider, name, api_key_encrypted, check_status)
                VALUES ($1, $2, $3, $4, 'valid') RETURNING *
                """,
                workspace_id,
                provider,
                name,
                api_key_encrypted,
            )
        except asyncpg.UniqueViolationError as error:
            if error.constraint_name == "provider_connections_workspace_name_key":
                raise ProviderConnectionNameTaken from None
            raise
        assert row is not None
        return self._provider_connection(row)

    async def update_provider_connection(
        self, workspace_id: UUID, connection_id: UUID, name: str, api_key_encrypted: str | None
    ) -> ProviderConnection | None:
        try:
            row = await self._connected_pool.fetchrow(
                """
                UPDATE provider_connections SET
                    name = $3,
                    api_key_encrypted = COALESCE($4, api_key_encrypted),
                    check_status = CASE WHEN $4 IS NULL THEN check_status ELSE 'valid' END,
                    checked_at = CASE WHEN $4 IS NULL THEN checked_at ELSE now() END,
                    updated_at = now()
                WHERE workspace_id = $1 AND id = $2 RETURNING *
                """,
                workspace_id,
                connection_id,
                name,
                api_key_encrypted,
            )
        except asyncpg.UniqueViolationError as error:
            if error.constraint_name == "provider_connections_workspace_name_key":
                raise ProviderConnectionNameTaken from None
            raise
        return self._provider_connection(row) if row else None

    async def record_provider_check(
        self,
        workspace_id: UUID,
        connection: ProviderConnection,
        status: Literal["valid", "rejected"],
    ) -> None:
        # A slow check must not overwrite the status of a replacement key.
        await self._connected_pool.execute(
            """
            UPDATE provider_connections SET check_status = $4, checked_at = now()
            WHERE workspace_id = $1 AND id = $2 AND api_key_encrypted = $3
            """,
            workspace_id,
            connection.id,
            connection.api_key_encrypted,
            status,
        )

    async def delete_provider_connection(
        self, workspace_id: UUID, connection_id: UUID, *, runtime_schema: str = "hondo_dbos"
    ) -> bool:
        try:
            async with self._connected_pool.acquire() as conn, conn.transaction():
                # Reset takes this lock too. A run that read the old selection
                # before reset is already visible in DBOS when we check here.
                team = await conn.fetchval(
                    "SELECT slack_team_id FROM workspaces WHERE id = $1 FOR UPDATE", workspace_id
                )
                connection_id = await conn.fetchval(
                    "SELECT id FROM provider_connections WHERE workspace_id = $1 AND id = $2",
                    workspace_id,
                    connection_id,
                )
                if team is None or connection_id is None:
                    return True
                assigned = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM agents WHERE provider_connection_id = $1)",
                    connection_id,
                )
                if assigned:
                    return False
                table_exists = await conn.fetchval(
                    "SELECT to_regclass(format('%I.workflow_status', $1::text)) IS NOT NULL",
                    runtime_schema,
                )
                if table_exists and await self._has_live_runs([team], runtime_schema):
                    return False
                await conn.execute(
                    "DELETE FROM provider_connections WHERE workspace_id = $1 AND id = $2",
                    workspace_id,
                    connection_id,
                )
        except asyncpg.ForeignKeyViolationError:
            return False
        return True

    async def _has_live_runs(self, teams: list[str], runtime_schema: str) -> bool:
        def check() -> bool:
            client = DBOSClient(system_database_url=self._dsn, dbos_system_schema=runtime_schema)
            try:
                return bool(
                    client.list_workflows(
                        workflow_id_prefix=[f"{team}:" for team in teams],
                        status=["PENDING", "ENQUEUED"],
                        limit=1,
                        load_input=False,
                        load_output=False,
                    )
                )
            finally:
                client.destroy()

        return await asyncio.to_thread(check)

    async def get_agent_model(self, workspace_id: UUID) -> AgentModel | None:
        record = await self._connected_pool.fetchrow(
            """
            SELECT provider_connections.id AS connection_id, provider_connections.provider,
                   agents.model_name, agents.reads_images
            FROM agents
            JOIN provider_connections
              ON provider_connections.workspace_id = agents.workspace_id
             AND provider_connections.id = agents.provider_connection_id
            WHERE agents.workspace_id = $1 AND agents.name = $2
            """,
            workspace_id,
            DEFAULT_AGENT_NAME,
        )
        if record is None:
            return None
        return AgentModel(
            connection_id=record["connection_id"],
            workspace_id=workspace_id,
            provider=record["provider"],
            model_name=record["model_name"],
            reads_images=record["reads_images"],
        )

    async def set_agent_model(
        self, workspace_id: UUID, connection_id: UUID, model_name: str, *, reads_images: bool
    ) -> bool:
        try:
            record = await self._connected_pool.fetchrow(
                """
                INSERT INTO agents
                    (workspace_id, name, provider_connection_id, model_name, reads_images)
                SELECT workspace_id, $4, id, $3, $5
                FROM provider_connections
                WHERE workspace_id = $1 AND id = $2 AND check_status = 'valid'
                ON CONFLICT (workspace_id, name) DO UPDATE SET
                    provider_connection_id = EXCLUDED.provider_connection_id,
                    model_name = EXCLUDED.model_name,
                    reads_images = EXCLUDED.reads_images,
                    updated_at = now()
                RETURNING id
                """,
                workspace_id,
                connection_id,
                model_name,
                DEFAULT_AGENT_NAME,
                reads_images,
            )
        except asyncpg.ForeignKeyViolationError:
            return False
        return record is not None

    async def clear_agent_model(self, workspace_id: UUID) -> None:
        async with self._connected_pool.acquire() as conn, conn.transaction():
            await conn.fetchval("SELECT id FROM workspaces WHERE id = $1 FOR UPDATE", workspace_id)
            await conn.execute(
                """
                UPDATE agents
                SET provider_connection_id = NULL, model_name = NULL, updated_at = now()
                WHERE workspace_id = $1 AND name = $2
                """,
                workspace_id,
                DEFAULT_AGENT_NAME,
            )

    async def get_mention_only(self, workspace_id: UUID) -> tuple[str, ...]:
        channels = await self._connected_pool.fetchval(
            "SELECT mention_only_channels FROM workspaces WHERE id = $1", workspace_id
        )
        return tuple(channels or ())

    async def set_mention_only(self, workspace_id: UUID, channels: list[str]) -> bool:
        """Save the channels. False, with nothing saved, when one of them is a
        listening channel."""
        saved = await self._connected_pool.fetchval(
            """
            UPDATE workspaces SET mention_only_channels = $2, updated_at = now()
            WHERE id = $1 AND NOT listening_channels ?| $2::text[]
            RETURNING id
            """,
            workspace_id,
            channels,
        )
        return saved is not None

    async def get_listening(self, workspace_id: UUID) -> dict[str, str]:
        goals = await self._connected_pool.fetchval(
            "SELECT listening_channels FROM workspaces WHERE id = $1", workspace_id
        )
        return json.loads(goals) if goals else {}

    async def set_listening(self, workspace_id: UUID, goals: dict[str, str]) -> bool:
        """Save the goals. False, with nothing saved, when one of the channels
        is mention-only."""
        saved = await self._connected_pool.fetchval(
            """
            UPDATE workspaces SET listening_channels = $2::jsonb, updated_at = now()
            WHERE id = $1 AND NOT mention_only_channels && $3::text[]
            RETURNING id
            """,
            workspace_id,
            json.dumps(goals),
            list(goals),
        )
        return saved is not None

    async def list_skills(self, workspace_id: UUID) -> list[Skill]:
        records = await self._connected_pool.fetch(
            "SELECT * FROM skills WHERE workspace_id = $1 ORDER BY name", workspace_id
        )
        return [_skill_from_record(record) for record in records]

    async def get_skill(self, workspace_id: UUID, skill_id: UUID) -> Skill:
        record = await self._connected_pool.fetchrow(
            "SELECT * FROM skills WHERE workspace_id = $1 AND id = $2", workspace_id, skill_id
        )
        if record is None:
            raise SkillError("skill_not_found")
        return _skill_from_record(record)

    async def create_skill(self, workspace_id: UUID, content: str, user_id: UUID) -> Skill:
        header = parse_skill(content)
        try:
            async with self._connected_pool.acquire() as conn, conn.transaction():
                # Serializes skill creation in the workspace, so the API and
                # the bot together cannot pass the limit.
                await conn.execute(
                    "SELECT 1 FROM workspaces WHERE id = $1 FOR UPDATE", workspace_id
                )
                count = await conn.fetchval(
                    "SELECT count(*) FROM skills WHERE workspace_id = $1", workspace_id
                )
                if count >= SKILLS_PER_WORKSPACE:
                    raise SkillError("skill_limit")
                record = await conn.fetchrow(
                    """
                    INSERT INTO skills
                        (workspace_id, name, description, content, created_by, updated_by)
                    VALUES ($1, $2, $3, $4, $5, $5)
                    RETURNING *
                    """,
                    workspace_id,
                    header.name,
                    header.description,
                    content,
                    user_id,
                )
        except asyncpg.UniqueViolationError as error:
            if error.constraint_name != "skills_workspace_name_key":
                raise
            raise SkillError("skill_name_taken") from None
        assert record is not None
        return _skill_from_record(record)

    async def update_skill(
        self,
        workspace_id: UUID,
        skill_id: UUID,
        user_id: UUID,
        *,
        revision: int,
        content: str,
        published: bool,
        all_channels: bool,
        channels: list[str],
    ) -> Skill:
        header = parse_skill(content)
        try:
            async with self._connected_pool.acquire() as conn, conn.transaction():
                saved = await conn.fetchval(
                    "SELECT revision FROM skills WHERE workspace_id = $1 AND id = $2 FOR UPDATE",
                    workspace_id,
                    skill_id,
                )
                if saved is None:
                    raise SkillError("skill_not_found")
                if saved != revision:
                    raise SkillError("skill_changed")
                if published and not (all_channels or channels):
                    raise SkillError("skill_not_ready")
                record = await conn.fetchrow(
                    """
                    UPDATE skills SET
                        name = $3, description = $4, content = $5, published = $6,
                        all_channels = $7, channels = $8, updated_by = $9,
                        revision = revision + 1, updated_at = now()
                    WHERE workspace_id = $1 AND id = $2
                    RETURNING *
                    """,
                    workspace_id,
                    skill_id,
                    header.name,
                    header.description,
                    content,
                    published,
                    all_channels,
                    channels,
                    user_id,
                )
        except asyncpg.UniqueViolationError as error:
            if error.constraint_name != "skills_workspace_name_key":
                raise
            raise SkillError("skill_name_taken") from None
        assert record is not None
        return _skill_from_record(record)

    async def delete_skill(self, workspace_id: UUID, skill_id: UUID) -> None:
        deleted = await self._connected_pool.fetchval(
            "DELETE FROM skills WHERE workspace_id = $1 AND id = $2 RETURNING id",
            workspace_id,
            skill_id,
        )
        if deleted is None:
            raise SkillError("skill_not_found")

    async def upsert_user(
        self,
        *,
        slack_team_id: str,
        slack_user_id: str,
        email: str,
        display_name: str,
        avatar_url: str | None,
        team_name: str,
    ) -> User:
        # Slack is the only source of the profile, so each sign-in refreshes
        # it; an empty or missing hint keeps the value we already have.
        record = await self._connected_pool.fetchrow(
            """
            INSERT INTO users
                (slack_team_id, slack_user_id, email, display_name, avatar_url, slack_team_name)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (slack_team_id, slack_user_id) DO UPDATE SET
                email = EXCLUDED.email,
                display_name = COALESCE(NULLIF(EXCLUDED.display_name, ''), users.display_name),
                avatar_url = COALESCE(EXCLUDED.avatar_url, users.avatar_url),
                slack_team_name = COALESCE(
                    NULLIF(EXCLUDED.slack_team_name, ''), users.slack_team_name
                ),
                updated_at = now()
            RETURNING *
            """,
            slack_team_id,
            slack_user_id,
            email.lower(),
            display_name,
            avatar_url,
            team_name,
        )
        if record is None:
            raise RuntimeError("user upsert returned no row")
        return _user_from_record(record)

    async def create_auth_session(
        self,
        token_hash: str,
        user_id: UUID,
        ttl: timedelta,
    ) -> None:
        await self._connected_pool.execute("DELETE FROM auth_sessions WHERE expires_at < now()")
        await self._connected_pool.execute(
            """
            INSERT INTO auth_sessions (token_hash, user_id, expires_at)
            VALUES ($1, $2, now() + $3::interval)
            """,
            token_hash,
            user_id,
            ttl,
        )

    async def get_session_user(self, token_hash: str) -> User | None:
        record = await self._connected_pool.fetchrow(
            """
            SELECT users.*
            FROM auth_sessions
            JOIN users ON users.id = auth_sessions.user_id
            WHERE auth_sessions.token_hash = $1
              AND auth_sessions.expires_at > now()
            """,
            token_hash,
        )
        return None if record is None else _user_from_record(record)

    async def delete_auth_session(self, token_hash: str) -> None:
        await self._connected_pool.execute(
            "DELETE FROM auth_sessions WHERE token_hash = $1",
            token_hash,
        )

    async def team_workspace(self, slack_team_id: str) -> TeamWorkspace | None:
        record = await self._connected_pool.fetchrow(
            """
            SELECT id, slack_team_id, name, status, bot_token_encrypted
            FROM workspaces WHERE slack_team_id = $1
            """,
            slack_team_id,
        )
        if record is None:
            return None
        return TeamWorkspace(
            workspace=_workspace_from_record(record),
            bot_token_encrypted=record["bot_token_encrypted"],
        )

    async def installed_team_id(self) -> str | None:
        """The Slack team this deployment serves, or None before the first install."""

        return await self._connected_pool.fetchval("SELECT slack_team_id FROM workspaces LIMIT 1")

    async def is_listed_admin(self, workspace_id: UUID, slack_user_id: str) -> bool:
        return await self._connected_pool.fetchval(
            """
            SELECT EXISTS (
                SELECT 1 FROM workspace_admins
                WHERE workspace_id = $1 AND slack_user_id = $2
            )
            """,
            workspace_id,
            slack_user_id,
        )

    async def list_admins(self, workspace_id: UUID) -> list[WorkspaceAdmin]:
        records = await self._connected_pool.fetch(
            """
            SELECT workspace_admins.slack_user_id,
                   workspace_admins.created_at,
                   NULLIF(users.display_name, '') AS added_by
            FROM workspace_admins
            LEFT JOIN users ON users.id = workspace_admins.added_by
            WHERE workspace_admins.workspace_id = $1
            ORDER BY workspace_admins.created_at, workspace_admins.slack_user_id
            """,
            workspace_id,
        )
        return [
            WorkspaceAdmin(
                slack_user_id=record["slack_user_id"],
                added_by=record["added_by"],
                created_at=record["created_at"],
            )
            for record in records
        ]

    async def add_admin(
        self, workspace_id: UUID, slack_user_id: str, caller: AdminCaller, added_by: UUID
    ) -> bool:
        """Adds the admin; False when the caller lost access before the lock."""

        async with self._connected_pool.acquire() as conn, conn.transaction():
            await conn.execute("SELECT 1 FROM workspaces WHERE id = $1 FOR UPDATE", workspace_id)
            if not caller.slack_admin and not await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM workspace_admins
                    WHERE workspace_id = $1 AND slack_user_id = $2
                )
                """,
                workspace_id,
                caller.slack_user_id,
            ):
                return False
            await conn.execute(
                """
                INSERT INTO workspace_admins (workspace_id, slack_user_id, added_by)
                VALUES ($1, $2, $3)
                ON CONFLICT (workspace_id, slack_user_id) DO NOTHING
                """,
                workspace_id,
                slack_user_id,
                added_by,
            )
            return True

    async def remove_admin(
        self, workspace_id: UUID, slack_user_id: str, caller: AdminCaller
    ) -> AdminRemoval:
        async with self._connected_pool.acquire() as conn, conn.transaction():
            # Serializes removals, so two at once cannot leave no listed admin,
            # and an admin removed a moment ago cannot remove anyone else.
            await conn.execute("SELECT 1 FROM workspaces WHERE id = $1 FOR UPDATE", workspace_id)
            listed = {
                record["slack_user_id"]
                for record in await conn.fetch(
                    "SELECT slack_user_id FROM workspace_admins WHERE workspace_id = $1",
                    workspace_id,
                )
            }
            if not caller.slack_admin and caller.slack_user_id not in listed:
                return "no_access"
            if slack_user_id not in listed:
                return "not_listed"
            if len(listed) == 1:
                return "last_admin"
            await conn.execute(
                "DELETE FROM workspace_admins WHERE workspace_id = $1 AND slack_user_id = $2",
                workspace_id,
                slack_user_id,
            )
            return "removed"

    async def list_admin_ids(self, workspace_id: UUID) -> list[str]:
        records = await self._connected_pool.fetch(
            """
            SELECT slack_user_id FROM workspace_admins
            WHERE workspace_id = $1 ORDER BY created_at, slack_user_id
            """,
            workspace_id,
        )
        return [record["slack_user_id"] for record in records]

    async def install_workspace(
        self,
        *,
        slack_team_id: str,
        name: str,
        bot_token_encrypted: str,
        installer_id: str,
        installer_status: SlackStatus | None,
    ) -> InstallResult:
        async with self._connected_pool.acquire() as conn, conn.transaction():
            await conn.execute("LOCK TABLE workspaces IN SHARE ROW EXCLUSIVE MODE")
            if await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM workspaces WHERE slack_team_id <> $1)",
                slack_team_id,
            ):
                return OtherWorkspaceInstalled()
            workspace_id = await conn.fetchval(
                """
                INSERT INTO workspaces (slack_team_id, name, status, bot_token_encrypted)
                VALUES ($1, $2, 'active', $3)
                ON CONFLICT (slack_team_id) DO NOTHING
                RETURNING id
                """,
                slack_team_id,
                name,
                bot_token_encrypted,
            )
            if workspace_id is not None:
                await conn.execute(
                    "INSERT INTO workspace_admins (workspace_id, slack_user_id) VALUES ($1, $2)",
                    workspace_id,
                    installer_id,
                )
                return WorkspaceInstalled(workspace_id=workspace_id, created=True)
            workspace_id = await conn.fetchval(
                "SELECT id FROM workspaces WHERE slack_team_id = $1 FOR UPDATE", slack_team_id
            )
            listed = await conn.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM workspace_admins
                    WHERE workspace_id = $1 AND slack_user_id = $2
                )
                """,
                workspace_id,
                installer_id,
            )
            if installer_status is not None and not installer_status.active:
                # A deactivated or guest account may not replace the token,
                # even when it is on the admin list.
                return InstallRefused()
            slack_admin = installer_status is not None and installer_status.admin
            if not listed and not slack_admin:
                return InstallRefused()
            await conn.execute(
                """
                UPDATE workspaces
                SET name = $2, status = 'active', bot_token_encrypted = $3, updated_at = now()
                WHERE id = $1
                """,
                workspace_id,
                name,
                bot_token_encrypted,
            )
            return WorkspaceInstalled(workspace_id=workspace_id, created=False)

    async def list_model_usage(
        self,
        team: str,
        start: datetime,
        end: datetime,
    ) -> list[dict[str, Any]]:
        """Model-request rows in the window, in rate_usage_rows() input shape.

        Dicts rather than a dataclass on purpose: rate_usage_rows is the single
        path from usage to money, and this is the row shape it already takes.
        """
        records = await self._connected_pool.fetch(
            """
            SELECT kind, provider, resource, quantity_in, quantity_out, metadata
            FROM usage_events
            WHERE team = $1
              AND kind = 'model_request'
              AND created_at >= $2
              AND created_at < $3
            """,
            team,
            start,
            end,
        )
        return [
            {
                "kind": record["kind"],
                "provider": record["provider"],
                "resource": record["resource"],
                "quantity_in": record["quantity_in"],
                "quantity_out": record["quantity_out"],
                "metadata": json.loads(record["metadata"]),
            }
            for record in records
        ]

    async def list_workspace_activity(self, team: str, limit: int) -> list[WorkspaceActivity]:
        records = await self._connected_pool.fetch(
            """
            SELECT channel, thread_root, state, created_at, updated_at
            FROM sessions
            WHERE team = $1
            ORDER BY updated_at DESC
            LIMIT $2
            """,
            team,
            limit,
        )
        return [
            WorkspaceActivity(
                channel=record["channel"],
                thread_root=record["thread_root"],
                state=record["state"],
                created_at=record["created_at"],
                updated_at=record["updated_at"],
            )
            for record in records
        ]

    async def list_thread_summaries(self, team: str, offset: int, limit: int) -> StoredSummaryPage:
        records = await self._connected_pool.fetch(
            """
            SELECT id, channel, thread_ts, private, text, updated_at
            FROM thread_summaries
            WHERE team = $1
            ORDER BY updated_at DESC, id DESC
            OFFSET $2
            LIMIT $3
            """,
            team,
            offset,
            limit + 1,
        )
        return StoredSummaryPage(
            summaries=[_summary_from_record(record) for record in records[:limit]],
            more=len(records) > limit,
        )

    async def search_thread_summaries(
        self, team: str, words: list[str], limit: int
    ) -> list[StoredSummary]:
        """What the agent finds in a public channel: private summaries never match."""
        view = SummaryView(team=team, channel=None, include_public=True)
        rows = await search_summary_rows(self._connected_pool, view, words, limit)
        return [_summary_from_record(row) for row in rows]

    async def delete_thread_summary(self, team: str, summary_id: int) -> bool:
        deleted = await self._connected_pool.fetchval(
            "DELETE FROM thread_summaries WHERE team = $1 AND id = $2 RETURNING id",
            team,
            summary_id,
        )
        return deleted is not None

    async def setup_facts(self, workspace_id: UUID, team: str) -> SetupFacts:
        record = await self._connected_pool.fetchrow(
            """
            SELECT
                EXISTS (
                    SELECT 1 FROM provider_connections
                    WHERE workspace_id = $1 AND check_status = 'valid'
                ) AS provider_connected,
                EXISTS (
                    SELECT 1 FROM mcp_connections WHERE workspace_id = $1 AND enabled
                ) AS tool_connected,
                EXISTS (
                    SELECT 1 FROM session_messages WHERE team = $2 AND channel NOT LIKE 'D%'
                ) AS answered_in_channel
            """,
            workspace_id,
            team,
        )
        assert record is not None
        return SetupFacts(
            provider_connected=record["provider_connected"],
            tool_connected=record["tool_connected"],
            answered_in_channel=record["answered_in_channel"],
        )
