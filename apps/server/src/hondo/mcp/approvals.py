"""Ask a person in the thread before a connector tool call runs.

A tool that needs approval has the kind "unapproved", so pydantic-ai stops
the call, also inside run_code, and asks `handle_deferred_tool_calls`. That
hook runs in workflow code: the post and the decision are recorded steps, and
the wait is a durable DBOS receive. A recovered run replays the recorded
steps and waits only for the rest of the time.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from dbos import DBOS
from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import DeferredToolRequests, DeferredToolResults, ToolDenied

from hondo.context import current_scope
from hondo.mcp.oauth import digest
from hondo.mcp.runtime import selections, tool_name
from hondo.mcp.store import MCPStore
from hondo.models import Event, EventKind
from hondo.runtime.durable import notify, recv, step, step_token
from hondo.runtime.model_selection import RunModel
from hondo.users import UserDirectory

logger = structlog.get_logger("hondo.approvals")

APPROVAL_TIMEOUT = 30 * 60.0
APPROVE_ACTION = "approval_approve"
REJECT_ACTION = "approval_reject"

_ARGUMENTS = 12
_VALUE_CHARS = 200


class ApprovalChat(Protocol):
    async def post_approval(
        self, *, channel: str, thread_ts: str, approval_id: str, text: str
    ) -> str | None:
        """Post the request with Approve and Reject buttons; returns the message ts."""
        ...

    async def close_approval(
        self, *, channel: str, message_ref: str, text: str, status: str
    ) -> None:
        """Replace the buttons with the status line."""
        ...

    async def is_member(self, *, channel: str, user_id: str) -> bool: ...


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def describe(label: str, arguments: dict[str, Any]) -> str:
    """The request as Slack mrkdwn: the tool, then one short line per argument."""
    lines = []
    for key, value in list(arguments.items())[:_ARGUMENTS]:
        shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        shown = " ".join(shown.split())
        if len(shown) > _VALUE_CHARS:
            shown = shown[: _VALUE_CHARS - 1] + "…"
        lines.append(f"{key}: {shown}")
    if len(arguments) > _ARGUMENTS:
        lines.append(f"and {len(arguments) - _ARGUMENTS} more")
    body = "\n".join(lines) if lines else "No arguments"
    return f"*Run {_escape(label)}?*\n```{_escape(body).replace('```', "'''")}```"


def _clock(moment: datetime) -> str:
    epoch = int(moment.timestamp())
    return f"<!date^{epoch}^{{time}}|{moment.astimezone(UTC):%H:%M} UTC>"


class ToolApprovals(AbstractCapability[RunModel]):
    def __init__(
        self,
        store: MCPStore,
        chat: ApprovalChat,
        directory: UserDirectory,
        timeout: float = APPROVAL_TIMEOUT,
    ) -> None:
        self.store = store
        self.chat = chat
        self.directory = directory
        self.timeout = timeout

    async def handle_deferred_tool_calls(
        self, ctx: RunContext[RunModel], *, requests: DeferredToolRequests
    ) -> DeferredToolResults | None:
        if not requests.approvals:
            return None
        results = DeferredToolResults()
        for call in requests.approvals:
            results.approvals[call.tool_call_id] = await self._ask(ctx, call)
        return results

    async def _ask(self, ctx: RunContext[RunModel], call: ToolCallPart) -> bool | ToolDenied:
        scope = current_scope()
        label = next(
            (
                f"{selection.name}: {tool.name}"
                for selection in selections(ctx)
                for tool in selection.tools
                if tool_name(selection, tool) == call.tool_name
            ),
            call.tool_name,
        )
        args = call.args_as_dict()
        arguments = args.get("arguments")
        text = describe(label, arguments if isinstance(arguments, dict) else args)
        try:
            opened = await step(
                "approval:open", self._open, scope.key.team, scope.channel, scope.thread_root, text
            )
        except Exception:
            logger.warning("approval request not posted", tool=label, exc_info=True)
            opened = {"ts": None}
        if opened["ts"] is None:
            return ToolDenied(
                "Hondo could not ask for approval in Slack, so the call did not run. "
                "Continue without it."
            )
        logger.info("tool call waits for approval", tool=label, approval_id=opened["id"])
        with scope.wait.waiting():
            await recv(f"approval:{opened['id']}", self.timeout)
        outcome = await step("approval:outcome", self._outcome, opened["id"])
        logger.info("tool call approval settled", approval_id=opened["id"], **outcome)
        try:
            await step(
                "approval:close",
                self.chat.close_approval,
                channel=scope.channel,
                message_ref=opened["ts"],
                text=text,
                status=outcome["line"],
            )
        except Exception:
            logger.warning("approval status not shown", exc_info=True)
        if outcome["status"] == "approved":
            return True
        if outcome["status"] == "rejected":
            return ToolDenied(
                f"Rejected by {outcome['name']}. The call did not run. Continue without it."
            )
        minutes = round(self.timeout / 60)
        return ToolDenied(
            f"No one approved this call in {minutes} minutes, so it did not run. "
            "Continue without it."
        )

    async def _open(self, team: str, channel: str, thread_ts: str, text: str) -> dict[str, Any]:
        token = step_token()
        workflow_id = DBOS.workflow_id
        if token is None or workflow_id is None:
            raise RuntimeError("an approval needs a workflow step")
        approval = await self.store.open_approval(
            digest(token)[:32], workflow_id, team, channel, self.timeout
        )
        ts = approval.message_ts
        if ts is None:
            ts = await self.chat.post_approval(
                channel=channel, thread_ts=thread_ts, approval_id=approval.id, text=text
            )
            if ts is not None:
                await self.store.set_approval_message(approval.id, ts)
        return {"id": approval.id, "ts": ts}

    async def _outcome(self, approval_id: str) -> dict[str, Any]:
        approval = await self.store.settle_approval(approval_id)
        if approval.status == "expired":
            minutes = round(self.timeout / 60)
            return {
                "status": "expired",
                "name": None,
                "line": f"Expired: no answer in {minutes} minutes",
            }
        user = approval.decided_by or ""
        name = await self.directory.name(user) or user
        assert approval.decided_at is not None
        verb = "Approved" if approval.status == "approved" else "Rejected"
        return {
            "status": approval.status,
            "name": name,
            "line": f"{verb} by <@{user}> · {_clock(approval.decided_at)}",
        }

    async def decide(self, event: Event) -> None:
        """Record a click on Approve or Reject and wake the waiting run.

        The click must come from the saved approval's channel and message, and
        the clicker must be a member of that channel.
        """
        approval_id = event.payload.get("approval_id")
        if event.kind is not EventKind.INTERACTION or not isinstance(approval_id, str):
            return
        approval = await self.store.get_approval(approval_id)
        if (
            approval is None
            or approval.team != event.team
            or approval.channel != event.channel
            or approval.message_ts is None
            or approval.message_ts != event.payload.get("message_ts")
            or event.user is None
        ):
            return
        if not await self.chat.is_member(channel=approval.channel, user_id=event.user):
            logger.warning(
                "approval click from outside the channel ignored",
                approval_id=approval_id,
                user=event.user,
            )
            return
        decided = await self.store.decide_approval(
            approval_id, event.user, approved=event.payload.get("approved") is True
        )
        if decided is None:
            logger.info("late approval click ignored", approval_id=approval_id, user=event.user)
            return
        await notify(decided.workflow_id, f"approval:{decided.id}")
