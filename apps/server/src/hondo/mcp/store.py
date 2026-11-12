from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, replace
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from asyncpg.pool import PoolConnectionProxy

from hondo.mcp.models import (
    REFRESH_LEASE,
    MCPConnection,
    MCPError,
    MCPSelection,
    MCPStatus,
    RemoteTool,
    ToolApproval,
)
from hondo.store.postgres import PostgresStore, _on_home_loop

_CONNECTION_SELECT = """SELECT c.*, u.display_name AS authorized_by_name
    FROM mcp_connections c JOIN workspaces w ON w.id=c.workspace_id
    LEFT JOIN users u ON u.id=c.authorized_by"""


def connection_from_row(row: asyncpg.Record) -> MCPConnection:
    return MCPConnection(
        id=row["id"],
        workspace_id=row["workspace_id"],
        name=row["name"],
        url=row["url"],
        authorized_by=row["authorized_by"],
        enabled=row["enabled"],
        all_channels=row["all_channels"],
        channels=tuple(row["channels"]),
        tools=tuple(RemoteTool.from_json(tool) for tool in json.loads(row["tools"])),
        allowed_tools=json.loads(row["allowed_tools"]),
        status=row["status"],
        revision=row["revision"],
        credential_version=row["credential_version"],
        updated_at=row["updated_at"],
        credentials=row["credentials"],
        authorized_by_name=row.get("authorized_by_name"),
        review_required=row["review_required"],
        auth=row["auth"],
        header_names=tuple(row["header_names"]),
        builtin=row["builtin"],
        ask_tools=tuple(row["ask_tools"]),
        kind=row["kind"],
    )


def approval_from_row(row: asyncpg.Record) -> ToolApproval:
    return ToolApproval(
        id=row["id"],
        workflow_id=row["workflow_id"],
        team=row["team"],
        channel=row["channel"],
        message_ts=row["message_ts"],
        status=row["status"],
        decided_by=row["decided_by"],
        decided_at=row["decided_at"],
    )


def _rerunnable(call: asyncpg.Record, args_hash: str) -> bool:
    # A read stopped before its result was saved changed nothing, so it can run again.
    return call["read_only"] and call["status"] == "started" and call["args_hash"] == args_hash


async def record_audit(
    db: PoolConnectionProxy, saved: MCPConnection, actor_id: UUID, action: str
) -> None:
    await db.execute(
        "INSERT INTO mcp_audit_events (workspace_id,connection_id,actor_id,action,revision,details) "
        "VALUES ($1,$2,$3,$4,$5,$6::jsonb)",
        saved.workspace_id,
        saved.id,
        actor_id,
        action,
        saved.revision,
        json.dumps(
            {
                "connection_id": str(saved.id),
                "name": saved.name,
                "enabled": saved.enabled,
                "all_channels": saved.all_channels,
                "channels": saved.channels,
                "allowed_tools": saved.allowed_tools,
                "ask_tools": saved.ask_tools,
                "authorized_by": str(saved.authorized_by) if saved.authorized_by else None,
                "credential_version": saved.credential_version,
                "auth": saved.auth,
                "header_names": saved.header_names,
                "builtin": saved.builtin,
                "kind": saved.kind,
            }
        ),
    )


class MCPStore(PostgresStore):
    @_on_home_loop
    async def workspace_token(self, workspace_id: UUID) -> str | None:
        return await self._connected_pool.fetchval(
            "SELECT bot_token_encrypted FROM workspaces WHERE id=$1 AND status='active'",
            workspace_id,
        )

    @_on_home_loop
    async def list_connections(self, workspace_id: UUID) -> list[MCPConnection]:
        rows = await self._connected_pool.fetch(
            _CONNECTION_SELECT + " WHERE c.workspace_id=$1 ORDER BY c.name, c.id", workspace_id
        )
        return [connection_from_row(row) for row in rows]

    @_on_home_loop
    async def get_connection(
        self, workspace_id: UUID, connection_id: UUID, *, active_team: str | None = None
    ) -> MCPConnection:
        row = await self._connected_pool.fetchrow(
            _CONNECTION_SELECT
            + """ WHERE c.workspace_id=$1 AND c.id=$2 AND ($3::text IS NULL OR
                (w.slack_team_id=$3 AND w.status='active'))""",
            workspace_id,
            connection_id,
            active_team,
        )
        if row is None:
            raise MCPError(
                "connection_access_changed" if active_team else "connection_not_found", 404
            )
        return connection_from_row(row)

    @_on_home_loop
    async def callable_tools(
        self,
        team: str,
        channel: str,
        selections: list[MCPSelection],
        *,
        listed_only: bool = False,
    ) -> set[tuple[UUID, str]]:
        """The selected tools that this channel can call now, as (connection id, tool name).
        listed_only leaves out connections that reach the channel only through
        all_channels."""
        requested = [
            {
                "id": str(item.connection_id),
                "workspace_id": str(item.workspace_id),
                "credential_version": item.credential_version,
                "name": tool.name,
                "fingerprint": tool.fingerprint,
            }
            for item in selections
            for tool in item.tools
        ]
        if not requested:
            return set()
        # The same sign-in as at the run's start: a disconnect deletes the call
        # journal, so a recovered run could send a call a second time.
        rows = await self._connected_pool.fetch(
            """SELECT expected.id, expected.name
            FROM jsonb_to_recordset($1::jsonb) AS expected
                (id uuid, workspace_id uuid, credential_version integer, name text,
                fingerprint text)
            JOIN mcp_connections c ON c.id=expected.id AND c.workspace_id=expected.workspace_id
            JOIN workspaces w ON w.id=c.workspace_id
            WHERE w.slack_team_id=$2 AND w.status='active'
                AND c.enabled AND c.credentials <> ''
                AND c.credential_version=expected.credential_version
                AND ((c.all_channels AND NOT $4) OR $3=ANY(c.channels))
                AND c.allowed_tools ->> expected.name = expected.fingerprint""",
            json.dumps(requested),
            team,
            channel,
            listed_only,
        )
        return {(row["id"], row["name"]) for row in rows}

    @_on_home_loop
    async def builtin_on(
        self, team: str, channel: str, builtin: str, *, listed_only: bool = False
    ) -> bool:
        """Whether an enabled connection made from this built-in has an approved tool
        in this channel. listed_only works as in callable_tools."""
        return await self._connected_pool.fetchval(
            """SELECT EXISTS (SELECT 1
            FROM mcp_connections c JOIN workspaces w ON w.id=c.workspace_id
            WHERE w.slack_team_id=$1 AND w.status='active' AND c.builtin=$3
                AND c.enabled AND c.credentials <> '' AND c.status <> 'sign_in_required'
                AND c.allowed_tools <> '{}'::jsonb
                AND ((c.all_channels AND NOT $4) OR $2=ANY(c.channels)))""",
            team,
            channel,
            builtin,
            listed_only,
        )

    @_on_home_loop
    async def reload_credentials(self, saved: MCPConnection) -> MCPConnection:
        row = await self._connected_pool.fetchrow(
            "SELECT credentials, credential_version, status FROM mcp_connections "
            "WHERE workspace_id=$1 AND id=$2",
            saved.workspace_id,
            saved.id,
        )
        if row is None:
            raise MCPError("connection_not_found", 404)
        return replace(
            saved,
            credentials=row["credentials"],
            credential_version=row["credential_version"],
            status=row["status"],
        )

    @_on_home_loop
    async def create_connection(
        self, workspace_id: UUID, name: str, url: str, user_id: UUID
    ) -> MCPConnection:
        try:
            async with self._connected_pool.acquire() as db, db.transaction():
                row = await db.fetchrow(
                    "INSERT INTO mcp_connections (id, workspace_id, name, url) "
                    "VALUES ($1,$2,$3,$4) RETURNING *",
                    uuid4(),
                    workspace_id,
                    name,
                    url,
                )
                assert row is not None
                await record_audit(db, connection_from_row(row), user_id, "created")
        except asyncpg.UniqueViolationError as exc:
            raise MCPError("connection_name_taken", 409) from exc
        assert row is not None
        return await self.get_connection(workspace_id, row["id"])

    @_on_home_loop
    async def create_builtin_connection(
        self,
        workspace_id: UUID,
        user_id: UUID,
        *,
        name: str,
        url: str,
        builtin: str,
        credentials: str,
        header_names: list[str],
        tools: list[RemoteTool],
    ) -> MCPConnection:
        try:
            async with self._connected_pool.acquire() as db, db.transaction():
                row = await db.fetchrow(
                    """INSERT INTO mcp_connections (id, workspace_id, name, url, builtin, auth,
                    credentials, header_names, authorized_by, credential_version, status, tools,
                    review_required)
                    VALUES ($1,$2,$3,$4,$5,'key',$6,$7,$8,1,'ready',$9::jsonb,$10) RETURNING *""",
                    uuid4(),
                    workspace_id,
                    name,
                    url,
                    builtin,
                    credentials,
                    header_names,
                    user_id,
                    json.dumps([asdict(tool) for tool in tools]),
                    bool(tools),
                )
                assert row is not None
                await record_audit(db, connection_from_row(row), user_id, "created")
        except asyncpg.UniqueViolationError as exc:
            raise MCPError("connection_name_taken", 409) from exc
        return await self.get_connection(workspace_id, row["id"])

    @_on_home_loop
    async def create_http_connection(
        self,
        workspace_id: UUID,
        user_id: UUID,
        *,
        name: str,
        url: str,
        credentials: str,
        header_names: list[str],
    ) -> MCPConnection:
        try:
            async with self._connected_pool.acquire() as db, db.transaction():
                row = await db.fetchrow(
                    """INSERT INTO mcp_connections (id, workspace_id, name, url, kind, auth,
                    credentials, header_names, authorized_by, credential_version, status)
                    VALUES ($1,$2,$3,$4,'http','key',$5,$6,$7,1,'ready') RETURNING *""",
                    uuid4(),
                    workspace_id,
                    name,
                    url,
                    credentials,
                    header_names,
                    user_id,
                )
                assert row is not None
                await record_audit(db, connection_from_row(row), user_id, "created")
        except asyncpg.UniqueViolationError as exc:
            raise MCPError("connection_name_taken", 409) from exc
        return await self.get_connection(workspace_id, row["id"])

    @_on_home_loop
    async def save_policy(
        self,
        workspace_id: UUID,
        connection_id: UUID,
        user_id: UUID,
        *,
        revision: int,
        name: str,
        enabled: bool,
        all_channels: bool,
        channels: list[str],
        allowed_tools: list[str],
        ask_tools: Sequence[str] = (),
        review_tools: bool = False,
    ) -> MCPConnection:
        async with self._connected_pool.acquire() as db, db.transaction():
            row = await db.fetchrow(
                "SELECT * FROM mcp_connections WHERE workspace_id=$1 AND id=$2 FOR UPDATE",
                workspace_id,
                connection_id,
            )
            if row is None:
                raise MCPError("connection_not_found", 404)
            saved = connection_from_row(row)
            if saved.revision != revision:
                raise MCPError("connection_changed", 409)
            catalog = {tool.name: tool.fingerprint for tool in saved.tools}
            if not set(allowed_tools) <= catalog.keys():
                raise MCPError("tools_changed", 409)
            approvals = {name: catalog[name] for name in allowed_tools}
            if enabled and (
                not saved.credentials or not approvals or not (all_channels or channels)
            ):
                raise MCPError("connection_not_ready", 409)
            try:
                row = await db.fetchrow(
                    """UPDATE mcp_connections SET name=$3, enabled=$4, all_channels=$5,
                    channels=$6, allowed_tools=$7::jsonb, revision=revision+1, updated_at=now(),
                    review_required=review_required AND NOT $8, ask_tools=$9
                    WHERE workspace_id=$1 AND id=$2 RETURNING *""",
                    workspace_id,
                    connection_id,
                    name,
                    enabled,
                    all_channels,
                    channels,
                    json.dumps(approvals),
                    review_tools,
                    sorted(set(ask_tools) & approvals.keys()),
                )
            except asyncpg.UniqueViolationError as exc:
                raise MCPError("connection_name_taken", 409) from exc
            assert row is not None
            await record_audit(db, connection_from_row(row), user_id, "policy_updated")
            row = await db.fetchrow(
                _CONNECTION_SELECT + " WHERE c.workspace_id=$1 AND c.id=$2",
                workspace_id,
                connection_id,
            )
        assert row is not None
        return connection_from_row(row)

    @_on_home_loop
    async def save_catalog(self, saved: MCPConnection, tools: list[RemoteTool]) -> None:
        catalog = {tool.name: tool.fingerprint for tool in tools}
        approvals = {
            name: fp for name, fp in saved.allowed_tools.items() if catalog.get(name) == fp
        }
        result = await self._connected_pool.execute(
            """UPDATE mcp_connections SET tools=$3::jsonb, allowed_tools=$4::jsonb,
            ask_tools=ARRAY(SELECT name FROM unnest(ask_tools) AS name WHERE $4::jsonb ? name),
            status=CASE WHEN status='sign_in_required' THEN status ELSE 'ready' END,
            review_required=review_required OR $5,
            revision=revision+CASE WHEN $5 THEN 1 ELSE 0 END, updated_at=now()
            WHERE id=$1 AND revision=$2""",
            saved.id,
            saved.revision,
            json.dumps([asdict(tool) for tool in tools]),
            json.dumps(approvals),
            catalog != {tool.name: tool.fingerprint for tool in saved.tools},
        )
        if result == "UPDATE 0":
            raise MCPError("connection_changed", 409)

    @_on_home_loop
    async def begin_oauth(
        self, saved: MCPConnection, user_id: UUID, session_hash: str, state_hash: str, payload: str
    ) -> None:
        async with self._connected_pool.acquire() as db, db.transaction():
            await db.execute("SELECT id FROM mcp_connections WHERE id=$1 FOR UPDATE", saved.id)
            await db.execute("DELETE FROM mcp_oauth_attempts WHERE expires_at < now()")
            await db.execute("DELETE FROM mcp_oauth_attempts WHERE connection_id=$1", saved.id)
            await db.execute(
                """INSERT INTO mcp_oauth_attempts
                (state_hash, connection_id, user_id, session_hash, credential_version, payload)
                VALUES ($1,$2,$3,$4,$5,$6)""",
                state_hash,
                saved.id,
                user_id,
                session_hash,
                saved.credential_version,
                payload,
            )

    @_on_home_loop
    async def consume_oauth(
        self, state_hash: str, user_id: UUID, session_hash: str
    ) -> dict[str, Any]:
        row = await self._connected_pool.fetchrow(
            """DELETE FROM mcp_oauth_attempts WHERE state_hash=$1 AND user_id=$2
            AND session_hash=$3 AND expires_at>now() RETURNING *""",
            state_hash,
            user_id,
            session_hash,
        )
        if row is None:
            raise MCPError("authorization_expired", 400)
        return dict(row)

    @_on_home_loop
    async def authorize(
        self,
        connection_id: UUID,
        credential_version: int,
        user_id: UUID,
        credentials: str,
    ) -> MCPConnection:
        async with self._connected_pool.acquire() as db, db.transaction():
            row = await db.fetchrow(
                """UPDATE mcp_connections SET credentials=$4, auth='oauth', header_names='{}',
                authorized_by=$3, credential_version=credential_version+1, status='ready',
                revision=revision+1, updated_at=now(), refresh_claim=NULL, refresh_until=NULL
                WHERE id=$1 AND credential_version=$2 RETURNING *""",
                connection_id,
                credential_version,
                user_id,
                credentials,
            )
            if row is None:
                raise MCPError("connection_changed", 409)
            await record_audit(db, connection_from_row(row), user_id, "authorized")
        return connection_from_row(row)

    @_on_home_loop
    async def save_key(
        self,
        workspace_id: UUID,
        connection_id: UUID,
        user_id: UUID,
        credentials: str,
        header_names: list[str],
    ) -> MCPConnection:
        async with self._connected_pool.acquire() as db, db.transaction():
            row = await db.fetchrow(
                """UPDATE mcp_connections SET credentials=$4, auth='key', header_names=$5,
                authorized_by=$3, credential_version=credential_version+1, status='ready',
                revision=revision+1, updated_at=now(), refresh_claim=NULL, refresh_until=NULL
                WHERE workspace_id=$1 AND id=$2 RETURNING *""",
                workspace_id,
                connection_id,
                user_id,
                credentials,
                header_names,
            )
            if row is None:
                raise MCPError("connection_not_found", 404)
            await record_audit(db, connection_from_row(row), user_id, "authorized")
        return connection_from_row(row)

    @_on_home_loop
    async def disconnect(self, workspace_id: UUID, connection_id: UUID, user_id: UUID) -> None:
        async with self._connected_pool.acquire() as db, db.transaction():
            row = await db.fetchrow(
                """UPDATE mcp_connections SET credentials=NULL, auth=NULL, header_names='{}',
                enabled=false, allowed_tools='{}', ask_tools='{}',
                status='sign_in_required', credential_version=credential_version+1,
                revision=revision+1, updated_at=now(), refresh_claim=NULL, refresh_until=NULL
                WHERE workspace_id=$1 AND id=$2 RETURNING *""",
                workspace_id,
                connection_id,
            )
            if row is None:
                raise MCPError("connection_not_found", 404)
            await db.execute("DELETE FROM mcp_oauth_attempts WHERE connection_id=$1", connection_id)
            await db.execute("DELETE FROM mcp_calls WHERE connection_id=$1", connection_id)
            await record_audit(db, connection_from_row(row), user_id, "disconnected")

    @_on_home_loop
    async def delete_connection(
        self, workspace_id: UUID, connection_id: UUID, user_id: UUID
    ) -> None:
        async with self._connected_pool.acquire() as db, db.transaction():
            row = await db.fetchrow(
                "SELECT * FROM mcp_connections WHERE workspace_id=$1 AND id=$2 FOR UPDATE",
                workspace_id,
                connection_id,
            )
            if row is not None:
                await record_audit(db, connection_from_row(row), user_id, "deleted")
                await db.execute("DELETE FROM mcp_connections WHERE id=$1", connection_id)

    @_on_home_loop
    async def snapshot(self, team: str) -> tuple[MCPSelection, ...]:
        rows = await self._connected_pool.fetch(
            """SELECT c.* FROM mcp_connections c JOIN workspaces w ON w.id=c.workspace_id
            WHERE w.slack_team_id=$1 AND w.status='active' AND c.enabled
            AND c.credentials IS NOT NULL ORDER BY c.id""",
            team,
        )
        selections = []
        for row in rows:
            saved = connection_from_row(row)
            # Every call would fail, so the run gets no tools, only the reason.
            signed_out = saved.status == "sign_in_required"
            selections.append(
                MCPSelection(
                    saved.id,
                    saved.workspace_id,
                    saved.credential_version,
                    saved.name,
                    saved.all_channels,
                    saved.channels,
                    tuple(
                        tool
                        for tool in saved.tools
                        if not signed_out and saved.allowed_tools.get(tool.name) == tool.fingerprint
                    ),
                    sign_in_required=signed_out,
                    builtin=saved.builtin,
                    ask_tools=saved.ask_tools,
                    kind=saved.kind,
                )
            )
        return tuple(selections)

    @_on_home_loop
    async def record_status(self, saved: MCPConnection, status: MCPStatus) -> None:
        await self._connected_pool.execute(
            """UPDATE mcp_connections SET status=$3 WHERE id=$1 AND credentials=$2
            AND (status<>'sign_in_required' OR $3='sign_in_required')""",
            saved.id,
            saved.credentials,
            status,
        )

    @_on_home_loop
    async def claim_refresh(self, saved: MCPConnection, claim: UUID) -> bool:
        # An expired lease may have consumed a rotating refresh token. Require reconnect.
        await self._connected_pool.execute(
            """UPDATE mcp_connections SET status='sign_in_required'
            WHERE id=$1 AND refresh_claim IS NOT NULL AND refresh_until < now()""",
            saved.id,
        )
        return bool(
            await self._connected_pool.fetchval(
                """UPDATE mcp_connections SET refresh_claim=$3, refresh_until=now()+$4::interval
            WHERE id=$1 AND credentials=$2 AND refresh_claim IS NULL
            AND status<>'sign_in_required' RETURNING true""",
                saved.id,
                saved.credentials,
                claim,
                REFRESH_LEASE,
            )
        )

    @_on_home_loop
    async def release_refresh(self, saved: MCPConnection, claim: UUID) -> None:
        await self._connected_pool.execute(
            """UPDATE mcp_connections SET refresh_claim=NULL, refresh_until=NULL
            WHERE id=$1 AND credentials=$2 AND refresh_claim=$3""",
            saved.id,
            saved.credentials,
            claim,
        )

    @_on_home_loop
    async def finish_refresh(
        self, saved: MCPConnection, claim: UUID, credentials: str | None
    ) -> bool:
        return bool(
            await self._connected_pool.fetchval(
                """UPDATE mcp_connections SET credentials=coalesce($4,credentials),
            status=CASE WHEN $4::text IS NULL THEN 'sign_in_required' ELSE 'ready' END,
            refresh_claim=NULL, refresh_until=NULL
            WHERE id=$1 AND credentials=$2 AND refresh_claim=$3 RETURNING true""",
                saved.id,
                saved.credentials,
                claim,
                credentials,
            )
        )

    @_on_home_loop
    async def call_run_ids(self, after: str, limit: int = 100) -> list[str]:
        rows = await self._connected_pool.fetch(
            "SELECT DISTINCT run_id FROM mcp_calls WHERE run_id>$1 ORDER BY run_id LIMIT $2",
            after,
            limit,
        )
        return [row["run_id"] for row in rows]

    @_on_home_loop
    async def delete_run_calls(self, run_ids: list[str]) -> int:
        result = await self._connected_pool.execute(
            "DELETE FROM mcp_calls WHERE run_id=ANY($1::text[])", run_ids
        )
        return int(result.split()[-1])

    @_on_home_loop
    async def recover_call(self, call_id: str, args_hash: str) -> str | None:
        row = await self._connected_pool.fetchrow("SELECT * FROM mcp_calls WHERE id=$1", call_id)
        if row is None or _rerunnable(row, args_hash):
            return None
        if row["args_hash"] != args_hash or row["status"] != "complete":
            raise MCPError("action_result_unknown", 409)
        return row["result_encrypted"]

    @_on_home_loop
    async def start_call(
        self,
        call_id: str,
        connection_id: UUID,
        run_id: str,
        tool_name: str,
        args_hash: str,
        channel: str,
        requested_by: str | None,
        *,
        read_only: bool = False,
    ) -> tuple[bool, str | None]:
        async with self._connected_pool.acquire() as db, db.transaction():
            await db.execute("SELECT id FROM mcp_connections WHERE id=$1 FOR UPDATE", connection_id)
            # An uncertain write blocks more actions on this connection in the same run.
            uncertain = await db.fetchval(
                "SELECT 1 FROM mcp_calls WHERE connection_id=$1 AND run_id=$2 "
                "AND status='started' AND NOT read_only AND id<>$3",
                connection_id,
                run_id,
                call_id,
            )
            if uncertain:
                raise MCPError("earlier_action_unconfirmed", 409)
            inserted = await db.fetchval(
                """INSERT INTO mcp_calls
                (id,connection_id,run_id,tool_name,args_hash,channel,requested_by,read_only)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT (id) DO NOTHING RETURNING true""",
                call_id,
                connection_id,
                run_id,
                tool_name,
                args_hash,
                channel,
                requested_by,
                read_only,
            )
            if inserted:
                return True, None
            row = await db.fetchrow("SELECT * FROM mcp_calls WHERE id=$1", call_id)
            if row is not None and _rerunnable(row, args_hash):
                return True, None
            if row is None or row["args_hash"] != args_hash or row["status"] != "complete":
                raise MCPError("action_result_unknown", 409)
            return False, row["result_encrypted"]

    @_on_home_loop
    async def finish_call(self, call_id: str, result: str) -> None:
        await self._connected_pool.execute(
            "UPDATE mcp_calls SET status='complete',result_encrypted=$2 WHERE id=$1",
            call_id,
            result,
        )

    @_on_home_loop
    async def open_approval(
        self, approval_id: str, workflow_id: str, team: str, channel: str, timeout: float
    ) -> ToolApproval:
        """Create the approval once; a repeated call returns the saved row."""
        await self._connected_pool.execute(
            """INSERT INTO tool_approvals (id, workflow_id, team, channel, expires_at)
            VALUES ($1,$2,$3,$4,now() + make_interval(secs => $5)) ON CONFLICT (id) DO NOTHING""",
            approval_id,
            workflow_id,
            team,
            channel,
            timeout,
        )
        approval = await self.get_approval(approval_id)
        assert approval is not None
        return approval

    @_on_home_loop
    async def set_approval_message(self, approval_id: str, message_ts: str) -> None:
        await self._connected_pool.execute(
            "UPDATE tool_approvals SET message_ts=$2 WHERE id=$1", approval_id, message_ts
        )

    @_on_home_loop
    async def get_approval(self, approval_id: str) -> ToolApproval | None:
        row = await self._connected_pool.fetchrow(
            "SELECT * FROM tool_approvals WHERE id=$1", approval_id
        )
        return None if row is None else approval_from_row(row)

    @_on_home_loop
    async def decide_approval(
        self, approval_id: str, user: str, *, approved: bool
    ) -> ToolApproval | None:
        """Record the first decision before the approval expires; None when too late."""
        row = await self._connected_pool.fetchrow(
            """UPDATE tool_approvals SET status=$3, decided_by=$2, decided_at=now()
            WHERE id=$1 AND status='pending' AND expires_at > now() RETURNING *""",
            approval_id,
            user,
            "approved" if approved else "rejected",
        )
        return None if row is None else approval_from_row(row)

    @_on_home_loop
    async def settle_approval(self, approval_id: str) -> ToolApproval:
        """The final decision. A pending approval expires now."""
        row = await self._connected_pool.fetchrow(
            """UPDATE tool_approvals SET status='expired', decided_at=now()
            WHERE id=$1 AND status='pending' RETURNING *""",
            approval_id,
        )
        approval = (
            approval_from_row(row) if row is not None else await self.get_approval(approval_id)
        )
        assert approval is not None
        return approval
