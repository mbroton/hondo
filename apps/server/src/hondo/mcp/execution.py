from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any

import structlog
from dbos import DBOS
from pydantic_ai import RunContext

from hondo.context import current_scope
from hondo.crypto import TokenCipher
from hondo.mcp.auth import MCPAuth
from hondo.mcp.http import ArgumentError, build_request, response_result, send
from hondo.mcp.models import MCPConnection, MCPError, MCPSelection, RemoteTool
from hondo.mcp.network import public_client
from hondo.mcp.oauth import MCPOAuth, digest
from hondo.mcp.outcomes import (
    CallOutcome,
    NotExecuted,
    ReadFailed,
    Received,
    RequestEvent,
    Unconfirmed,
    call_failure,
    error_details,
    outcome_result,
)
from hondo.mcp.remote import MCPRemote
from hondo.mcp.sessions import MCPSessions, OpenSession
from hondo.mcp.store import MCPStore
from hondo.runtime.model_selection import RunModel

logger = structlog.get_logger("hondo.mcp")

# One call, including the session that it opens if the run has none yet.
CALL_TIMEOUT = 45

NOT_ALLOWED = (
    "This tool is not available in this channel now. No action was sent. Do not retry it. "
    "If the task needs it, say that an admin can check access in Connectors."
)


@dataclass
class CallAttempt:
    call_id: str
    store: MCPStore
    cipher: TokenCipher
    outcome: CallOutcome = field(
        default_factory=lambda: NotExecuted(MCPError("connection_unavailable"))
    )
    claim: asyncio.Task[tuple[bool, str | None]] | None = None
    read_only: bool = False

    def observe(self, event: RequestEvent) -> None:
        if isinstance(self.outcome, Received):
            return
        self.outcome = (
            Unconfirmed(MCPError("action_result_unknown"))
            if event == "dispatch"
            else NotExecuted(MCPError("connection_unavailable"))
        )

    def fail(self, error: BaseException) -> None:
        if isinstance(self.outcome, Received):
            return
        failure = call_failure(error)
        if failure.error.code == "action_result_unknown":
            self.outcome = Unconfirmed(failure.error)
        elif isinstance(self.outcome, NotExecuted):
            self.outcome = NotExecuted(failure.error)
        else:
            self.outcome = failure

    async def settle(self) -> None:
        if self.read_only and isinstance(self.outcome, Unconfirmed):
            self.outcome = ReadFailed()
        if self.claim is None:
            return
        try:
            fresh, _ = await self.claim
        except Exception:
            return
        if fresh and not isinstance(self.outcome, Unconfirmed):
            await self.store.finish_call(
                self.call_id, self.cipher.encrypt(json.dumps(outcome_result(self.outcome)))
            )


class MCPExecutor:
    def __init__(self, store: MCPStore, cipher: TokenCipher, base_url: str) -> None:
        self.store, self.cipher, self.base_url = store, cipher, base_url
        self.remote = MCPRemote()

    @asynccontextmanager
    async def connect(self, saved: MCPConnection) -> AsyncIterator[OpenSession]:
        async with public_client() as client:
            auth = MCPAuth(MCPOAuth(self.store, self.cipher, client, self.base_url), saved)
            # The run keeps this session between calls. CALL_TIMEOUT bounds each call.
            async with self.remote.session(saved.url, auth, timeout=None) as session:
                yield OpenSession(session, auth)

    async def execute(
        self,
        selection: MCPSelection,
        name: str,
        tool: RemoteTool,
        arguments: dict[str, Any],
        ctx: RunContext[RunModel],
        sessions: MCPSessions,
    ) -> dict[str, Any]:
        scope = current_scope()
        run_id = DBOS.workflow_id or ctx.run_id or scope.event.event_id
        call_id = digest(
            json.dumps(
                [
                    run_id,
                    ctx.run_id,
                    ctx.run_step,
                    ctx.tool_call_id,
                    str(selection.connection_id),
                    name,
                ]
            )
        )
        args_hash = digest(json.dumps(arguments, sort_keys=True, separators=(",", ":")))
        try:
            recorded = await self.store.recover_call(call_id, args_hash)
        except MCPError as exc:
            return outcome_result(call_failure(exc))
        if recorded is not None:
            return json.loads(self.cipher.decrypt(recorded))
        # Access is checked once, before the call: a change made later stops
        # later calls, not this one.
        allowed = await self.store.callable_tools(
            scope.key.team,
            scope.channel,
            [replace(selection, tools=(tool,))],
            listed_only=ctx.deps.shared_channel,
        )
        if (selection.connection_id, tool.name) not in allowed:
            return {"error": NOT_ALLOWED}

        attempt = CallAttempt(call_id, self.store, self.cipher)
        try:
            saved = await self.store.get_connection(
                selection.workspace_id, selection.connection_id, active_team=scope.key.team
            )
            if saved.credential_version != selection.credential_version:
                return {"error": NOT_ALLOWED}
            claim = partial(
                self.store.start_call,
                call_id,
                saved.id,
                run_id,
                tool.name,
                args_hash,
                scope.channel,
                scope.requester,
            )
            if saved.kind == "http":
                refused = await self._send_http(saved, tool, arguments, attempt, claim)
                if refused is not None:
                    return refused
            else:
                # Each call lists the tools again: a server can change a tool without
                # telling the client, and a changed tool needs a new review.
                async with (
                    asyncio.timeout(CALL_TIMEOUT),
                    sessions.lease(saved, self.remote.tools) as (held, catalog),
                ):
                    opened = held.opened
                    live = next(
                        (
                            item
                            for item in catalog
                            if item.name == tool.name and item.fingerprint == tool.fingerprint
                        ),
                        None,
                    )
                    if live is None:
                        await self.store.save_catalog(saved, catalog)
                        return {"error": "This tool changed. Ask an admin to review it before use."}
                    attempt.read_only = live.read_only
                    attempt.claim = asyncio.create_task(claim(read_only=live.read_only))
                    fresh, recorded = await asyncio.shield(attempt.claim)
                    if not fresh:
                        assert recorded is not None
                        attempt.outcome = Received(json.loads(self.cipher.decrypt(recorded)))
                    else:
                        # Stays set until the next call: the session can still send
                        # this request after the call stops waiting for it.
                        opened.auth.on_request = attempt.observe
                        response = await held.send(
                            self.remote.call_tool(opened.session, tool.name, arguments=arguments)
                        )
                        attempt.outcome = Received(
                            response.model_dump(mode="json", exclude_none=True)
                        )
        except BaseException as exc:
            attempt.fail(exc)
            if not isinstance(exc, Exception):
                try:
                    await asyncio.shield(attempt.settle())
                except Exception as cleanup_error:
                    logger.warning("mcp call cleanup failed", errors=error_details(cleanup_error))
                raise
            logger.warning("mcp request failed", errors=error_details(exc))
        # The network deadline and teardown cannot cancel a received result's save.
        await asyncio.shield(attempt.settle())
        return outcome_result(attempt.outcome)

    async def _send_http(
        self,
        saved: MCPConnection,
        tool: RemoteTool,
        arguments: dict[str, Any],
        attempt: CallAttempt,
        claim: Callable[..., Coroutine[Any, Any, tuple[bool, str | None]]],
    ) -> dict[str, Any] | None:
        """Send the approved definition; a returned error means nothing was sent."""
        live = next(
            (
                item
                for item in saved.tools
                if item.name == tool.name and item.fingerprint == tool.fingerprint
            ),
            None,
        )
        if live is None or live.request is None or not saved.credentials:
            return {"error": "This tool changed. Ask an admin to review it before use."}
        try:
            build_request(live.request, arguments)
        except ArgumentError as exc:
            return {"error": f"{exc} No request was sent."}
        headers = json.loads(self.cipher.decrypt(saved.credentials))["headers"]
        attempt.claim = asyncio.create_task(claim(read_only=False))
        fresh, recorded = await asyncio.shield(attempt.claim)
        if not fresh:
            assert recorded is not None
            attempt.outcome = Received(json.loads(self.cipher.decrypt(recorded)))
            return None
        attempt.observe("dispatch")
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                response = await send(live.request, arguments, headers)
        except MCPError as exc:
            if exc.code != "response_too_large":
                raise
            attempt.outcome = Received(
                {
                    "error": "The request was sent, but the response is larger than 2 MB, "
                    "so Hondo did not read it. Ask for less data if the API allows it."
                }
            )
            return None
        attempt.outcome = Received(response_result(response, headers.values()))
        return None
