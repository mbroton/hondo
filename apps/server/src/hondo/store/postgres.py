from __future__ import annotations

import asyncio
import functools
import json
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import asdict
from typing import Any, cast
from uuid import UUID

import asyncpg
import structlog

from hondo.agent_settings import DEFAULT_AGENT_NAME, AgentModel
from hondo.chat import ChatFile
from hondo.models import (
    HISTORY_SCHEMA_VERSION,
    Event,
    RunNotice,
    Session,
    SessionKey,
    SessionState,
    SteerMail,
    SteerPriority,
    WorkspaceRecord,
)
from hondo.skills import SKILLS_PER_WORKSPACE, parse_skill
from hondo.store.base import (
    RunSnapshotMetadata,
    SkillDraft,
    SkillDraftOutcome,
    SkillSummary,
    SummaryMatch,
    SummarySearch,
    SummaryView,
    ThreadSummary,
)
from hondo.store.migrate import upgrade_to_head

logger = structlog.get_logger("hondo.store.postgres")


def _on_home_loop[F: Callable[..., Coroutine[Any, Any, Any]]](method: F) -> F:
    """Marshal a store call onto the loop the pool was created on.

    asyncpg pools are loop-affine, but queued DBOS workflows execute on their
    own event loop; steps calling the store from there hop back to the loop
    `connect()` ran on (which serve() keeps running).
    """

    @functools.wraps(method)
    async def wrapper(self: PostgresStore, *args: Any, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        if self._loop is None or loop is self._loop:
            return await method(self, *args, **kwargs)
        future = asyncio.run_coroutine_threadsafe(method(self, *args, **kwargs), self._loop)
        return await asyncio.wrap_future(future)

    return cast("F", wrapper)


async def search_summary_rows(
    pool: asyncpg.Pool, view: SummaryView, words: Sequence[str], limit: int
) -> list[asyncpg.Record]:
    """The summary search of the agent, also used by the dashboard. The best
    matches first, each with its whole text, a snippet around the matched
    words, and the number of all matches."""
    return await pool.fetch(
        """
        WITH words AS (
            SELECT query
            FROM unnest($4::text[]) AS word, phraseto_tsquery('english', word) AS query
            -- A stop word ("the") gives an empty query, which matches nothing.
            WHERE numnode(query) > 0
        ),
        any_word AS (
            SELECT string_agg('(' || query::text || ')', ' | ')::tsquery AS query FROM words
        )
        SELECT s.id, s.channel, s.thread_ts, s.private, s.text, s.updated_at,
               ts_headline('english', s.text, any_word.query,
                           'MaxFragments=1, MaxWords=35, MinWords=15, StartSel="", StopSel=""') AS snippet,
               count(*) OVER () AS total
        FROM thread_summaries s, any_word
        WHERE s.team = $1
          AND (s.channel = $2 OR ($3 AND NOT s.private))
          AND s.search @@ any_word.query
        ORDER BY (SELECT count(*) FROM words WHERE s.search @@ words.query) DESC,
                 ts_rank(s.search, any_word.query) DESC,
                 s.updated_at DESC
        LIMIT $5
        """,
        view.team,
        view.channel,
        view.include_public,
        list(words),
        limit,
    )


class PostgresStore:
    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: asyncpg.Pool | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def dsn(self) -> str:
        """The connection URL; serve() reuses it for the DBOS system database."""
        return self._dsn

    @property
    def _connected_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("PostgresStore is not connected; call connect() first")
        return self._pool

    async def connect(
        self,
        *,
        run_migrations: bool = True,
        pool_min_size: int = 10,
        pool_max_size: int = 10,
    ) -> None:
        if self._pool is None:
            self._loop = asyncio.get_running_loop()
            self._pool = await asyncpg.create_pool(
                self._dsn,
                min_size=pool_min_size,
                max_size=pool_max_size,
            )
            if run_migrations:
                await self.migrate()

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None
            self._loop = None

    async def migrate(self) -> None:
        """Apply pending Alembic migrations (raw SQL, shipped in the package).

        Runs in a thread: Alembic is synchronous (SQLAlchemy + psycopg).
        Concurrent workers serialize on an advisory lock inside the migration
        transaction.
        """
        await asyncio.to_thread(upgrade_to_head, self._dsn)

    # --- workspaces ------------------------------------------------------------

    @_on_home_loop
    async def get_installed_workspace(self) -> WorkspaceRecord | None:
        row = await self._connected_pool.fetchrow("SELECT * FROM workspaces LIMIT 1")
        return None if row is None else self._workspace_from_row(row)

    @_on_home_loop
    async def get_agent_model(self, team: str) -> AgentModel | None:
        row = await self._connected_pool.fetchrow(
            """
            SELECT
                provider_connections.id AS connection_id,
                provider_connections.provider,
                agents.model_name,
                agents.reads_images,
                workspaces.id AS workspace_id
            FROM workspaces
            JOIN agents
              ON agents.workspace_id = workspaces.id
             AND agents.name = $2
            JOIN provider_connections
              ON provider_connections.workspace_id = workspaces.id
             AND provider_connections.id = agents.provider_connection_id
            WHERE workspaces.slack_team_id = $1
            """,
            team,
            DEFAULT_AGENT_NAME,
        )
        if row is None:
            return None
        return AgentModel(
            connection_id=row["connection_id"],
            workspace_id=row["workspace_id"],
            provider=row["provider"],
            model_name=row["model_name"],
            reads_images=row["reads_images"],
        )

    @_on_home_loop
    async def mention_only_channel(self, team: str, channel: str) -> bool:
        value = await self._connected_pool.fetchval(
            """
            SELECT $2 = ANY(mention_only_channels)
            FROM workspaces
            WHERE slack_team_id = $1
            """,
            team,
            channel,
        )
        return value is True

    @_on_home_loop
    async def listening_goal(self, team: str, channel: str) -> str | None:
        value = await self._connected_pool.fetchval(
            "SELECT listening_channels ->> $2 FROM workspaces WHERE slack_team_id = $1",
            team,
            channel,
        )
        return value if isinstance(value, str) else None

    @_on_home_loop
    async def get_provider_key(self, workspace_id: UUID, connection_id: UUID) -> str | None:
        return await self._connected_pool.fetchval(
            """
            SELECT api_key_encrypted
            FROM provider_connections
            WHERE workspace_id = $1 AND id = $2
            """,
            workspace_id,
            connection_id,
        )

    # --- event log -------------------------------------------------------------

    @_on_home_loop
    async def append_event(self, event: Event) -> bool:
        status = await self._connected_pool.execute(
            """
            INSERT INTO events
                (event_id, kind, team, channel, ts, user_id, thread_ts,
                 text, is_dm, mentions_bot, files, payload)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
            ON CONFLICT (event_id) DO NOTHING
            """,
            event.event_id,
            event.kind.value,
            event.team,
            event.channel,
            event.ts,
            event.user,
            event.thread_ts,
            event.text,
            event.is_dm,
            event.mentions_bot,
            _files_json(event.files),
            json.dumps(event.payload),
        )
        return status == "INSERT 0 1"

    @_on_home_loop
    async def get_event(self, event_id: str) -> Event | None:
        row = await self._connected_pool.fetchrow(
            "SELECT * FROM events WHERE event_id = $1", event_id
        )
        if row is None:
            return None
        return self._event_from_row(row)

    # --- sessions ---------------------------------------------------------------

    @_on_home_loop
    async def ensure_session(self, key: SessionKey) -> None:
        await self._connected_pool.execute(
            """
            INSERT INTO sessions (team, channel, thread_root, state)
            VALUES ($1, $2, $3, 'idle')
            ON CONFLICT (team, channel, thread_root) DO NOTHING
            """,
            key.team,
            key.channel,
            key.thread_root,
        )

    @_on_home_loop
    async def get_session(self, key: SessionKey) -> Session | None:
        row = await self._connected_pool.fetchrow(
            """
            SELECT * FROM sessions
            WHERE team = $1 AND channel = $2 AND thread_root = $3
            """,
            key.team,
            key.channel,
            key.thread_root,
        )
        if row is None:
            return None
        return Session(
            key=SessionKey(
                team=row["team"], channel=row["channel"], thread_root=row["thread_root"]
            ),
            state=SessionState(row["state"]),
            roster=json.loads(row["roster"]) if row["roster"] else {},
            last_event_id=row["last_event_id"],
            context=json.loads(row["context"]) if row["context"] else {},
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @_on_home_loop
    async def save_session(self, session: Session) -> None:
        await self._upsert_session(self._connected_pool, session)

    @_on_home_loop
    async def merge_context_key(self, key: SessionKey, name: str, patch: dict[str, Any]) -> None:
        await self._connected_pool.execute(
            """
            UPDATE sessions
            SET context = jsonb_set(
                    context,
                    ARRAY[$4],
                    COALESCE(context->$4, '{}'::jsonb) || $5::jsonb
                ),
                updated_at = now()
            WHERE team = $1 AND channel = $2 AND thread_root = $3
            """,
            key.team,
            key.channel,
            key.thread_root,
            name,
            json.dumps(patch),
        )

    @_on_home_loop
    async def get_session_messages(
        self, key: SessionKey, *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        rows = await self._connected_pool.fetch(
            """
            SELECT message FROM (
                SELECT id, message
                FROM session_messages
                WHERE team = $1
                  AND channel = $2
                  AND thread_root = $3
                  AND history_version = $4
                ORDER BY id DESC
                LIMIT $5
            ) AS tail
            ORDER BY id
            """,
            key.team,
            key.channel,
            key.thread_root,
            HISTORY_SCHEMA_VERSION,
            limit,
        )
        return [json.loads(row["message"]) for row in rows]

    @_on_home_loop
    async def append_session_messages(
        self, key: SessionKey, *, run_id: str, messages: list[dict[str, Any]]
    ) -> None:
        if not messages:
            return
        async with self._connected_pool.acquire() as conn, conn.transaction():
            await self._insert_messages(conn, key, run_id=run_id, messages=messages)

    @_on_home_loop
    async def save_session_run(
        self,
        session: Session,
        *,
        run_id: str,
        messages: list[dict[str, Any]],
        clear_context_entries: tuple[str, list[str]] | None = None,
    ) -> None:
        # One transaction: committed messages the session row doesn't acknowledge
        # would resurface in later loads as history no run answered against.
        async with self._connected_pool.acquire() as conn, conn.transaction():
            await self._insert_messages(conn, session.key, run_id=run_id, messages=messages)
            await self._upsert_session(conn, session)
            if clear_context_entries is not None:
                name, entry_keys = clear_context_entries
                # Entry-level clear: an entry merged while this run was live
                # (a sweep announcing another dead workflow) survives. The key
                # itself is dropped once its last entry goes.
                await conn.execute(
                    """
                    UPDATE sessions SET context = CASE
                        WHEN COALESCE(context->$4, '{}'::jsonb) - $5::text[] = '{}'::jsonb
                            THEN context - $4
                        ELSE jsonb_set(context, ARRAY[$4], (context->$4) - $5::text[])
                    END
                    WHERE team = $1 AND channel = $2 AND thread_root = $3
                    """,
                    session.key.team,
                    session.key.channel,
                    session.key.thread_root,
                    name,
                    entry_keys,
                )
                await conn.execute(
                    "DELETE FROM run_snapshots WHERE workflow_id = ANY($1::text[])",
                    entry_keys,
                )

    @staticmethod
    async def _insert_messages(
        conn: asyncpg.pool.PoolConnectionProxy[asyncpg.Record],
        key: SessionKey,
        *,
        run_id: str,
        messages: list[dict[str, Any]],
    ) -> None:
        for seq, message in enumerate(messages):
            await conn.execute(
                """
                INSERT INTO session_messages
                    (team, channel, thread_root, run_id, seq, history_version, message)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (run_id, seq) DO NOTHING
                """,
                key.team,
                key.channel,
                key.thread_root,
                run_id,
                seq,
                HISTORY_SCHEMA_VERSION,
                json.dumps(message),
            )

    @staticmethod
    async def _upsert_session(
        executor: asyncpg.Pool | asyncpg.pool.PoolConnectionProxy[asyncpg.Record],
        session: Session,
    ) -> None:
        # Context is deliberately absent so per-key failure writes survive success upserts.
        await executor.execute(
            """
            INSERT INTO sessions
                (team, channel, thread_root, state, roster, last_event_id)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (team, channel, thread_root) DO UPDATE SET
                state = EXCLUDED.state,
                roster = EXCLUDED.roster,
                last_event_id = EXCLUDED.last_event_id,
                updated_at = now()
            """,
            session.key.team,
            session.key.channel,
            session.key.thread_root,
            session.state.value,
            json.dumps(session.roster),
            session.last_event_id,
        )

    # --- run snapshots ---------------------------------------------------------

    @_on_home_loop
    async def save_run_snapshot(
        self, workflow_id: str, key: SessionKey, payload: dict[str, Any]
    ) -> None:
        await self._connected_pool.execute(
            """
            INSERT INTO run_snapshots
                (workflow_id, team, channel, thread_root, history_version, payload)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (workflow_id) DO UPDATE SET
                team = EXCLUDED.team,
                channel = EXCLUDED.channel,
                thread_root = EXCLUDED.thread_root,
                history_version = EXCLUDED.history_version,
                payload = EXCLUDED.payload
            """,
            workflow_id,
            key.team,
            key.channel,
            key.thread_root,
            HISTORY_SCHEMA_VERSION,
            json.dumps(payload),
        )

    @_on_home_loop
    async def list_run_snapshots(self, workflow_ids: Sequence[str]) -> list[RunSnapshotMetadata]:
        rows = await self._connected_pool.fetch(
            """
            SELECT workflow_id, created_at
            FROM run_snapshots
            WHERE workflow_id = ANY($1::text[])
              AND history_version = $2
            """,
            list(workflow_ids),
            HISTORY_SCHEMA_VERSION,
        )
        return [
            RunSnapshotMetadata(
                workflow_id=row["workflow_id"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    @_on_home_loop
    async def get_run_snapshots(self, workflow_ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        rows = await self._connected_pool.fetch(
            """
            SELECT workflow_id, payload
            FROM run_snapshots
            WHERE workflow_id = ANY($1::text[])
              AND history_version = $2
            """,
            list(workflow_ids),
            HISTORY_SCHEMA_VERSION,
        )
        return {row["workflow_id"]: json.loads(row["payload"]) for row in rows}

    @_on_home_loop
    async def delete_run_snapshots_older_than(self, max_age_seconds: float) -> int:
        count = await self._connected_pool.fetchval(
            """
            WITH deleted AS (
                DELETE FROM run_snapshots
                WHERE created_at < now() - $1 * interval '1 second'
                RETURNING 1
            )
            SELECT count(*) FROM deleted
            """,
            max_age_seconds,
        )
        return int(count)

    # --- thread summaries ---------------------------------------------------------

    @_on_home_loop
    async def search_summaries(
        self, view: SummaryView, words: Sequence[str], limit: int
    ) -> SummarySearch:
        rows = await search_summary_rows(self._connected_pool, view, words, limit)
        return SummarySearch(
            matches=[
                SummaryMatch(
                    id=row["id"],
                    channel=row["channel"],
                    thread_ts=row["thread_ts"],
                    snippet=row["snippet"],
                )
                for row in rows
            ],
            total=rows[0]["total"] if rows else 0,
        )

    @_on_home_loop
    async def get_summary(self, view: SummaryView, summary_id: int) -> ThreadSummary | None:
        row = await self._connected_pool.fetchrow(
            """
            SELECT id, channel, thread_ts, text, covered_ts, created_at, updated_at
            FROM thread_summaries
            WHERE id = $1 AND team = $2 AND (channel = $3 OR ($4 AND NOT private))
            """,
            summary_id,
            view.team,
            view.channel,
            view.include_public,
        )
        return ThreadSummary(**dict(row)) if row is not None else None

    @_on_home_loop
    async def thread_summary(self, key: SessionKey) -> ThreadSummary | None:
        row = await self._connected_pool.fetchrow(
            """
            SELECT id, channel, thread_ts, text, covered_ts, created_at, updated_at
            FROM thread_summaries
            WHERE team = $1 AND channel = $2 AND thread_ts = $3
            """,
            key.team,
            key.channel,
            key.thread_root,
        )
        return ThreadSummary(**dict(row)) if row is not None else None

    @_on_home_loop
    async def save_summary(
        self, key: SessionKey, *, private: bool, text: str, covered_ts: str
    ) -> bool:
        saved = await self._connected_pool.fetchval(
            """
            INSERT INTO thread_summaries (team, channel, thread_ts, private, text, covered_ts)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (team, channel, thread_ts) DO UPDATE SET
                -- Two jobs can overlap, and one of them can read the channel
                -- before it became private.
                private = thread_summaries.private OR EXCLUDED.private,
                text = EXCLUDED.text,
                covered_ts = EXCLUDED.covered_ts,
                updated_at = now()
            WHERE thread_summaries.covered_ts < EXCLUDED.covered_ts
            RETURNING id
            """,
            key.team,
            key.channel,
            key.thread_root,
            private,
            text,
            covered_ts,
        )
        return saved is not None

    # --- skills -------------------------------------------------------------------

    @_on_home_loop
    async def channel_skills(
        self, team: str, channel: str, *, listed_only: bool = False
    ) -> list[SkillSummary]:
        rows = await self._connected_pool.fetch(
            """
            SELECT s.name, s.description
            FROM skills s JOIN workspaces w ON w.id = s.workspace_id
            WHERE w.slack_team_id = $1 AND s.published
              AND ((s.all_channels AND NOT $3) OR $2 = ANY(s.channels))
            ORDER BY s.name
            """,
            team,
            channel,
            listed_only,
        )
        return [SkillSummary(name=row["name"], description=row["description"]) for row in rows]

    @_on_home_loop
    async def read_skill(
        self, team: str, channel: str, name: str, *, listed_only: bool = False
    ) -> str | None:
        return await self._connected_pool.fetchval(
            """
            SELECT s.content
            FROM skills s JOIN workspaces w ON w.id = s.workspace_id
            WHERE w.slack_team_id = $1 AND s.name = $3 AND s.published
              AND ((s.all_channels AND NOT $4) OR $2 = ANY(s.channels))
            """,
            team,
            channel,
            name,
            listed_only,
        )

    @_on_home_loop
    async def save_thread_draft(
        self, team: str, channel: str, thread_ts: str, content: str, token: str | None
    ) -> SkillDraft:
        header = parse_skill(content)
        async with self._connected_pool.acquire() as conn, conn.transaction():
            # Serializes skill creation in the workspace, so the API and the
            # bot together cannot pass the limit.
            workspace_id = await conn.fetchval(
                "SELECT id FROM workspaces WHERE slack_team_id = $1 FOR UPDATE", team
            )
            if workspace_id is None:
                raise RuntimeError("the team has no workspace")
            current = await conn.fetchrow(
                """
                SELECT id, published, draft_token FROM skills
                WHERE workspace_id = $1 AND source_channel = $2 AND source_ts = $3
                FOR UPDATE
                """,
                workspace_id,
                channel,
                thread_ts,
            )
            current_id = current["id"] if current is not None else None
            # Recovery runs the tool again after this write committed. Doing it
            # again would undo admin edits saved since then.
            if current is not None and token is not None and current["draft_token"] == token:
                return SkillDraft(SkillDraftOutcome.SAVED, workspace_id, current_id)
            if current is not None and current["published"]:
                return SkillDraft(SkillDraftOutcome.PUBLISHED, workspace_id, current_id)
            if current is None:
                count = await conn.fetchval(
                    "SELECT count(*) FROM skills WHERE workspace_id = $1", workspace_id
                )
                if count >= SKILLS_PER_WORKSPACE:
                    return SkillDraft(SkillDraftOutcome.LIMIT, workspace_id, None)
            try:
                async with conn.transaction():
                    if current_id is not None:
                        await conn.execute(
                            """
                            UPDATE skills SET name = $2, description = $3, content = $4,
                                draft_token = $5, updated_by = NULL, revision = revision + 1,
                                updated_at = now()
                            WHERE id = $1
                            """,
                            current_id,
                            header.name,
                            header.description,
                            content,
                            token,
                        )
                        skill_id = current_id
                    else:
                        skill_id = await conn.fetchval(
                            """
                            INSERT INTO skills (workspace_id, name, description, content,
                                channels, source_channel, source_ts, draft_token)
                            VALUES ($1, $2, $3, $4, $5, $6, $7, $8) RETURNING id
                            """,
                            workspace_id,
                            header.name,
                            header.description,
                            content,
                            [channel],
                            channel,
                            thread_ts,
                            token,
                        )
            except asyncpg.UniqueViolationError as error:
                if error.constraint_name != "skills_workspace_name_key":
                    raise
                return SkillDraft(SkillDraftOutcome.NAME_TAKEN, workspace_id, current_id)
            return SkillDraft(SkillDraftOutcome.SAVED, workspace_id, skill_id)

    # --- steer ledger -----------------------------------------------------------

    @_on_home_loop
    async def insert_mail(
        self,
        key: SessionKey,
        event: Event,
        priority: SteerPriority,
        *,
        target_workflow_id: str | None,
    ) -> SteerMail | None:
        async with self._connected_pool.acquire() as conn, conn.transaction():
            row = await conn.fetchrow(
                """
                INSERT INTO steer_mail
                    (team, channel, thread_root, event_id, user_id, text, files, ts,
                     priority, target_workflow_id)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                ON CONFLICT (event_id) DO NOTHING
                RETURNING *
                """,
                key.team,
                key.channel,
                key.thread_root,
                event.event_id,
                event.user,
                event.text,
                _files_json(event.files),
                event.ts,
                priority.value,
                target_workflow_id,
            )
            if row is None:
                # Redelivery of an already-steered event. Consumed: the reply was
                # handled, nothing to deliver. Unconsumed: retarget and let the
                # caller re-send (sends are idempotent per event).
                row = await conn.fetchrow(
                    """
                    UPDATE steer_mail SET target_workflow_id = $2
                    WHERE event_id = $1 AND consumed_at IS NULL
                    RETURNING *
                    """,
                    event.event_id,
                    target_workflow_id,
                )
                if row is None:
                    return None
        return self._mail_from_row(row)

    @_on_home_loop
    async def mail_for_event(self, event_id: str) -> SteerMail | None:
        row = await self._connected_pool.fetchrow(
            "SELECT * FROM steer_mail WHERE event_id = $1", event_id
        )
        if row is None:
            return None
        return self._mail_from_row(row)

    @_on_home_loop
    async def mail_claimed_by(self, workflow_id: str) -> list[SteerMail]:
        rows = await self._connected_pool.fetch(
            """
            SELECT * FROM steer_mail
            WHERE claimed_by IS NOT NULL
              AND starts_with(claimed_by, $1 || ':')
            ORDER BY id
            """,
            workflow_id,
        )
        return [self._mail_from_row(row) for row in rows]

    @_on_home_loop
    async def unconsumed_mail(self, key: SessionKey) -> list[SteerMail]:
        rows = await self._connected_pool.fetch(
            """
            SELECT *
            FROM steer_mail
            WHERE team = $1
              AND channel = $2
              AND thread_root = $3
              AND consumed_at IS NULL
            ORDER BY id
            """,
            key.team,
            key.channel,
            key.thread_root,
        )
        return [self._mail_from_row(row) for row in rows]

    @_on_home_loop
    async def stale_unconsumed_mail(self, min_age_seconds: float) -> list[SteerMail]:
        rows = await self._connected_pool.fetch(
            """
            SELECT *
            FROM steer_mail
            WHERE consumed_at IS NULL
              AND created_at < now() - $1 * interval '1 second'
            ORDER BY id
            """,
            min_age_seconds,
        )
        return [self._mail_from_row(row) for row in rows]

    @_on_home_loop
    async def consume_mail(self, mail_ids: Sequence[int]) -> None:
        if not mail_ids:
            return
        await self._connected_pool.execute(
            """
            UPDATE steer_mail SET consumed_at = now()
            WHERE id = ANY($1::bigint[]) AND consumed_at IS NULL
            """,
            list(mail_ids),
        )

    @_on_home_loop
    async def waiting_message_ages(
        self, key: SessionKey, event_id: str
    ) -> tuple[float, float] | None:
        row = await self._connected_pool.fetchrow(
            """
            SELECT extract(epoch FROM now() - min(arrived_at))::float8 AS since_first,
                   extract(epoch FROM now() - max(arrived_at))::float8 AS since_last
            FROM (
                SELECT received_at AS arrived_at FROM events WHERE event_id = $4
                UNION ALL
                SELECT created_at FROM steer_mail
                WHERE team = $1 AND channel = $2 AND thread_root = $3
                  AND consumed_at IS NULL
            ) AS waiting
            """,
            key.team,
            key.channel,
            key.thread_root,
            event_id,
        )
        if row is None or row["since_first"] is None:
            return None
        return row["since_first"], row["since_last"]

    @_on_home_loop
    async def claim_mail(self, key: SessionKey, *, token: str | None) -> list[SteerMail]:
        # One statement: consume-and-return is a single snapshot, and the token
        # re-selects rows an earlier execution of the same claim already
        # consumed — the crash-replay case documented on the protocol.
        rows = await self._connected_pool.fetch(
            """
            WITH claimed AS (
                UPDATE steer_mail
                SET consumed_at = COALESCE(consumed_at, now()), claimed_by = $4
                WHERE team = $1 AND channel = $2 AND thread_root = $3
                  AND (consumed_at IS NULL
                       OR ($4::text IS NOT NULL AND claimed_by = $4::text))
                RETURNING *
            )
            SELECT * FROM claimed ORDER BY ts, id
            """,
            key.team,
            key.channel,
            key.thread_root,
            token,
        )
        return [self._mail_from_row(row) for row in rows]

    # --- usage events -----------------------------------------------------------

    @_on_home_loop
    async def record_usage(self, rows: Sequence[dict[str, Any]]) -> None:
        if not rows:
            return
        async with self._connected_pool.acquire() as conn, conn.transaction():
            for row in rows:
                await conn.execute(
                    """
                    INSERT INTO usage_events
                        (team, kind, provider, resource, quantity_in, quantity_out,
                         metadata, channel, thread_root, workflow_id)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (workflow_id, kind, resource) DO UPDATE SET
                        provider = EXCLUDED.provider,
                        quantity_in = EXCLUDED.quantity_in,
                        quantity_out = EXCLUDED.quantity_out,
                        metadata = EXCLUDED.metadata
                    """,
                    row["team"],
                    row["kind"],
                    row["provider"],
                    row["resource"],
                    row["quantity_in"],
                    row["quantity_out"],
                    json.dumps(row["metadata"]),
                    row["channel"],
                    row["thread_root"],
                    row["workflow_id"],
                )

    # --- run notices ------------------------------------------------------------

    @_on_home_loop
    async def record_run_notice(self, workflow_id: str, text: str) -> bool:
        row = await self._connected_pool.fetchrow(
            """
            INSERT INTO run_notices (workflow_id, text) VALUES ($1, $2)
            ON CONFLICT DO NOTHING
            RETURNING workflow_id
            """,
            workflow_id,
            text,
        )
        return row is not None

    @_on_home_loop
    async def run_notice_recorded(self, workflow_id: str) -> bool:
        return bool(
            await self._connected_pool.fetchval(
                "SELECT EXISTS (SELECT 1 FROM run_notices WHERE workflow_id = $1)",
                workflow_id,
            )
        )

    @_on_home_loop
    async def take_pending_run_notices(self, max_attempts: int) -> list[RunNotice]:
        rows = await self._connected_pool.fetch(
            """
            WITH taken AS (
                UPDATE run_notices SET attempts = attempts + 1
                WHERE posted_at IS NULL AND attempts < $1
                RETURNING workflow_id, text, created_at
            )
            SELECT workflow_id, text FROM taken ORDER BY created_at
            """,
            max_attempts,
        )
        return [RunNotice(workflow_id=row["workflow_id"], text=row["text"]) for row in rows]

    @_on_home_loop
    async def mark_run_notice_posted(self, workflow_id: str) -> None:
        await self._connected_pool.execute(
            "UPDATE run_notices SET posted_at = now() WHERE workflow_id = $1",
            workflow_id,
        )

    # --- row mapping ------------------------------------------------------------

    @staticmethod
    def _workspace_from_row(row: asyncpg.Record) -> WorkspaceRecord:
        return WorkspaceRecord(
            id=row["id"],
            slack_team_id=row["slack_team_id"],
            name=row["name"],
            status=row["status"],
            bot_token_encrypted=row["bot_token_encrypted"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _event_from_row(row: asyncpg.Record) -> Event:
        payload = row["payload"]
        if not isinstance(payload, dict):
            payload = json.loads(payload)
        return Event(
            event_id=row["event_id"],
            kind=row["kind"],
            team=row["team"],
            channel=row["channel"],
            ts=row["ts"],
            user=row["user_id"],
            thread_ts=row["thread_ts"],
            text=row["text"],
            is_dm=row["is_dm"],
            mentions_bot=row["mentions_bot"],
            files=_files_from_row(row["files"]),
            payload=payload,
        )

    @staticmethod
    def _mail_from_row(row: asyncpg.Record) -> SteerMail:
        return SteerMail(
            id=row["id"],
            key=SessionKey(
                team=row["team"], channel=row["channel"], thread_root=row["thread_root"]
            ),
            event_id=row["event_id"],
            user=row["user_id"],
            text=row["text"],
            files=_files_from_row(row["files"]),
            ts=row["ts"],
            priority=SteerPriority(row["priority"]),
            target_workflow_id=row["target_workflow_id"],
            consumed=row["consumed_at"] is not None,
        )


def _files_json(files: Sequence[ChatFile]) -> str:
    return json.dumps([asdict(file) for file in files])


def _files_from_row(value: Any) -> tuple[ChatFile, ...]:
    items = json.loads(value) if isinstance(value, str) else value
    return tuple(ChatFile(**item) for item in items)
