from __future__ import annotations

import asyncio
from typing import Any

import structlog
from dbos import DBOS, StepOptions
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import (
    ApprovalRequired,
    CallDeferred,
    ModelRetry,
    SkipModelRequest,
    SkipToolExecution,
    SkipToolValidation,
    ToolRetryError,
)
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.tools import RunContext, ToolDefinition

logger = structlog.get_logger("hondo.agent.tool_boundary")

# Intentional control-flow exceptions — pydantic-ai's own signaling between
# tools and the agent graph. These must never be flattened into error text.
_PASSTHROUGH_EXCEPTIONS = (
    ApprovalRequired,
    CallDeferred,
    ModelRetry,
    SkipModelRequest,
    SkipToolExecution,
    SkipToolValidation,
    ToolRetryError,
)

# These tools run again on recovery to rebuild in-memory state (the sandbox
# variables, the task list and its card). Their external calls are recorded
# as their own steps, and an outer timer could change the replay path.
_REPLAYED_TOOLS = frozenset({"run_code", "write_todos"})

# How long a cancelled tool task gets to unwind before it is abandoned.
_TOOL_CANCEL_GRACE_SECONDS = 5.0


class ToolBoundary(AbstractCapability[Any]):
    """Record and bound external calls; replay stateful tools to restore their state."""

    def __init__(self, tool_timeout: float = 60.0) -> None:
        self.tool_timeout = tool_timeout

    async def wrap_tool_execute(
        self,
        ctx: RunContext[Any],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: Any,
        handler: Any,
    ) -> Any:
        """Bound external calls and record outcomes for recovery.

        Scripts and the task list replay outside recorded steps to restore their state.
        Framework control-flow exceptions must reach the agent unchanged.
        """

        async def bounded(handler_args: Any) -> Any:
            task = asyncio.ensure_future(handler(handler_args))
            done, _ = await asyncio.wait({task}, timeout=self.tool_timeout)
            if done:
                return task.result()
            task.cancel()
            done_after_cancel, _ = await asyncio.wait({task}, timeout=_TOOL_CANCEL_GRACE_SECONDS)
            if not done_after_cancel:
                logger.error(
                    "tool cancellation hung; task abandoned as a zombie",
                    tool=tool_def.name,
                )
            logger.warning(
                "tool timed out; error returned to the model",
                tool=tool_def.name,
                timeout=self.tool_timeout,
            )
            return (
                f"Error: tool '{tool_def.name}' timed out after "
                f"{self.tool_timeout:.0f}s. It may be temporarily unavailable."
            )

        try:
            if tool_def.name in _REPLAYED_TOOLS and DBOS.workflow_id is not None:
                return await handler(args)
            if DBOS.workflow_id is None:
                return await bounded(args)
            return await DBOS.run_step_async(
                StepOptions(name=f"tool:{tool_def.name}"), bounded, args
            )
        except _PASSTHROUGH_EXCEPTIONS:
            raise
        except Exception as exc:
            logger.warning(
                "tool failed; error returned to the model",
                tool=tool_def.name,
                exc_info=True,
            )
            return f"Error: tool '{tool_def.name}' failed: {exc}"
