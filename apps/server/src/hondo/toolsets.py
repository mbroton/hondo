"""Toolset wrappers for optional connectors."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Self

import structlog
from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai.messages import InstructionPart
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool, WrapperToolset

logger = structlog.get_logger("hondo.toolsets")

_UNAVAILABLE_NOTICE = (
    "The {name} connector is unavailable right now, so none of its tools are "
    "loaded for this conversation turn. If the answer would normally draw on "
    "{name}, tell the user plainly that {name} is currently unreachable. Do "
    "not guess at or invent what it would have returned."
)

# Connect and discovery failures share one budget: a connector gets at most
# this many network attempts per run before it fails open for the rest of it.
_MAX_DISCOVERY_FAILURES = 2


def prepare_sequential_tools(
    ctx: RunContext[Any], tools: list[ToolDefinition]
) -> list[ToolDefinition]:
    # CodeMode exposes these as synchronous calls. Deferred futures can resume
    # in a different order after a crash, which breaks DBOS step replay.
    return [replace(tool, sequential=True) for tool in tools]


@dataclass
class OptionalToolset(WrapperToolset[AgentDepsT]):
    """Let a run continue when an external connector is unavailable."""

    name: str
    _unavailable: bool = field(default=False, init=False, repr=False)
    _entered: bool = field(default=False, init=False, repr=False)
    _discovery_failures: int = field(default=0, init=False, repr=False)

    # DBOS calls inherited visit_and_replace before any run, so its state reset is harmless.

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> AbstractToolset[AgentDepsT]:
        return replace(self, wrapped=await self.wrapped.for_run(ctx))

    async def for_run_step(self, ctx: RunContext[AgentDepsT]) -> AbstractToolset[AgentDepsT]:
        new_wrapped = await self.wrapped.for_run_step(ctx)
        if new_wrapped is self.wrapped:
            return self

        # Enter/exit happen on the for_run instance held by the run's exit
        # stack, never on step copies; the carried flags only keep the
        # fail-open state consistent across steps.
        new = replace(self, wrapped=new_wrapped)
        new._unavailable = self._unavailable
        new._entered = self._entered
        new._discovery_failures = self._discovery_failures
        return new

    async def __aenter__(self) -> Self:
        try:
            await self.wrapped.__aenter__()
        except Exception:
            # Consumes one attempt from the shared budget; get_tools gets the
            # remaining one via the wrapped toolset's lazy reconnect.
            self._discovery_failures += 1
            logger.warning(
                "mcp connector failed to connect; will retry at tool discovery",
                connector=self.name,
                exc_info=True,
            )
        else:
            self._entered = True
        return self

    async def __aexit__(self, *args: Any) -> bool | None:
        if not self._entered:
            return None

        self._entered = False
        try:
            return await self.wrapped.__aexit__(*args)
        except Exception:
            logger.warning(
                "mcp connector teardown failed; ignoring",
                connector=self.name,
                exc_info=True,
            )
            return None

    async def get_tools(self, ctx: RunContext[AgentDepsT]) -> dict[str, ToolsetTool[AgentDepsT]]:
        if self._unavailable:
            return {}

        while True:
            try:
                return await self.wrapped.get_tools(ctx)
            except Exception:
                self._discovery_failures += 1
                if self._discovery_failures >= _MAX_DISCOVERY_FAILURES:
                    logger.warning(
                        "mcp connector tool discovery failed; continuing without its tools",
                        connector=self.name,
                        exc_info=True,
                    )
                    self._unavailable = True
                    return {}
                logger.warning(
                    "mcp connector tool discovery failed; retrying once",
                    connector=self.name,
                    exc_info=True,
                )

    async def get_instructions(
        self, ctx: RunContext[AgentDepsT]
    ) -> str | InstructionPart | Sequence[str | InstructionPart] | None:
        if self._unavailable:
            return _UNAVAILABLE_NOTICE.format(name=self.name)

        try:
            return await self.wrapped.get_instructions(ctx)
        except Exception:
            # This step's tools were already fetched successfully, so the
            # connector is not unavailable — drop its instructions only.
            logger.warning(
                "mcp connector instructions fetch failed; skipping them",
                connector=self.name,
                exc_info=True,
            )
            return None
