from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, Literal, Self

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (
    InstructionPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import (
    AbstractToolset,
    FunctionToolset,
    PrefixedToolset,
    ToolsetTool,
    WrapperToolset,
)
from pydantic_ai.usage import RunUsage

from hondo.toolsets import OptionalToolset

FailingPhase = Literal["enter", "exit", "get_tools", "get_instructions"]
ExceptionFactory = Callable[[], BaseException]


async def broken_connector_tool() -> str:
    return "broken connector result"


async def healthy_connector_tool() -> str:
    return "healthy connector result"


async def agent_tool() -> str:
    return "agent tool result"


def _connection_error() -> BaseException:
    return ConnectionError("connector unavailable")


class FailingToolset(AbstractToolset[Any]):
    def __init__(
        self,
        phase: FailingPhase | tuple[FailingPhase, ...],
        *,
        exception_factory: ExceptionFactory = _connection_error,
        fail_count: int | None = None,
    ) -> None:
        self.phases = (phase,) if isinstance(phase, str) else phase
        self.exception_factory = exception_factory
        self.fail_count = fail_count
        self.get_tools_calls = 0
        self.enter_calls = 0
        self.exit_calls = 0
        self._failures = 0
        self._healthy: FunctionToolset[Any] = FunctionToolset(
            [broken_connector_tool], id="fake-connector"
        )

    @property
    def id(self) -> str | None:
        return "failing"

    def _raise_if_configured(self, phase: FailingPhase) -> None:
        if phase not in self.phases:
            return
        if self.fail_count is not None and self._failures >= self.fail_count:
            return
        self._failures += 1
        raise self.exception_factory()

    async def __aenter__(self) -> Self:
        self.enter_calls += 1
        self._raise_if_configured("enter")
        return self

    async def __aexit__(self, *args: Any) -> bool | None:
        self.exit_calls += 1
        self._raise_if_configured("exit")
        return None

    async def get_tools(self, ctx: RunContext[Any]) -> dict[str, ToolsetTool[Any]]:
        self.get_tools_calls += 1
        self._raise_if_configured("get_tools")
        return await self._healthy.get_tools(ctx)

    async def get_instructions(
        self, ctx: RunContext[Any]
    ) -> str | InstructionPart | Sequence[str | InstructionPart] | None:
        self._raise_if_configured("get_instructions")
        return await self._healthy.get_instructions(ctx)

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[Any],
        tool: ToolsetTool[Any],
    ) -> Any:
        return await self._healthy.call_tool(name, tool_args, ctx, tool)


def _request_instructions(messages: list[ModelMessage]) -> str:
    requests = [message for message in messages if isinstance(message, ModelRequest)]
    assert requests
    return requests[-1].instructions or ""


def _ctx() -> RunContext[Any]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


async def test_failing_connector_keeps_healthy_and_agent_tools() -> None:
    broken = FailingToolset("get_tools")
    healthy = FunctionToolset([healthy_connector_tool], id="healthy")
    seen_tools: list[set[str]] = []
    seen_instructions: list[str] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen_tools.append({tool.name for tool in info.function_tools})
        seen_instructions.append(_request_instructions(messages))
        return ModelResponse(parts=[TextPart("answer")])

    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn),
        tools=[agent_tool],
        toolsets=[OptionalToolset(broken, name="Linear"), healthy],
    )

    result = await agent.run("hello")

    assert result.output == "answer"
    assert seen_tools == [{"agent_tool", "healthy_connector_tool"}]
    assert "broken_connector_tool" not in seen_tools[0]
    assert "Linear" in seen_instructions[0]
    assert "unavailable" in seen_instructions[0]


async def test_persistent_failure_retries_once_then_short_circuits_later_steps() -> None:
    broken = FailingToolset("get_tools")
    healthy = FunctionToolset([healthy_connector_tool], id="healthy")
    seen_instructions: list[str] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen_instructions.append(_request_instructions(messages))
        if len(seen_instructions) == 1:
            return ModelResponse(parts=[ToolCallPart("healthy_connector_tool", {})])
        return ModelResponse(parts=[TextPart("done")])

    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn),
        toolsets=[OptionalToolset(broken, name="Linear"), healthy],
    )

    result = await agent.run("hello")

    assert result.output == "done"
    assert broken.get_tools_calls == 2
    assert len(seen_instructions) == 2
    assert all("Linear" in instructions for instructions in seen_instructions)
    assert all("unavailable" in instructions for instructions in seen_instructions)


async def test_one_discovery_failure_recovers_on_immediate_retry() -> None:
    flaky = FailingToolset("get_tools", fail_count=1)
    seen_tools: list[set[str]] = []
    seen_instructions: list[str] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen_tools.append({tool.name for tool in info.function_tools})
        seen_instructions.append(_request_instructions(messages))
        return ModelResponse(parts=[TextPart("recovered")])

    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn),
        toolsets=[OptionalToolset(flaky, name="Linear")],
    )

    result = await agent.run("hello")

    assert result.output == "recovered"
    assert flaky.get_tools_calls == 2
    assert seen_tools == [{"broken_connector_tool"}]
    assert all("unavailable" not in instructions for instructions in seen_instructions)


async def test_failure_state_resets_between_runs_on_same_agent() -> None:
    recovering = FailingToolset("get_tools", fail_count=2)
    seen_tools: list[set[str]] = []
    seen_instructions: list[str] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen_tools.append({tool.name for tool in info.function_tools})
        seen_instructions.append(_request_instructions(messages))
        return ModelResponse(parts=[TextPart("answer")])

    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn),
        toolsets=[OptionalToolset(recovering, name="Linear")],
    )

    await agent.run("first")
    await agent.run("second")

    assert seen_tools == [set(), {"broken_connector_tool"}]
    assert "unavailable" in seen_instructions[0]
    assert "unavailable" not in seen_instructions[1]
    assert recovering.get_tools_calls == 3


async def test_all_connectors_failing_still_returns_text() -> None:
    agent: Agent[None, str] = Agent(
        TestModel(call_tools=[]),
        toolsets=[
            OptionalToolset(FailingToolset("get_tools"), name="Linear"),
            OptionalToolset(FailingToolset("get_tools"), name="Bitbucket"),
        ],
    )

    result = await agent.run("hello")

    assert isinstance(result.output, str)
    assert result.output


async def test_cancellation_shapes_propagate_from_get_tools() -> None:
    cancelled = OptionalToolset(
        FailingToolset("get_tools", exception_factory=asyncio.CancelledError),
        name="Linear",
    )
    with pytest.raises(asyncio.CancelledError):
        await cancelled.get_tools(_ctx())

    cancelled_group = OptionalToolset(
        FailingToolset(
            "get_tools",
            exception_factory=lambda: BaseExceptionGroup("x", [asyncio.CancelledError()]),
        ),
        name="Linear",
    )
    with pytest.raises(BaseExceptionGroup) as exc_info:
        await cancelled_group.get_tools(_ctx())
    assert isinstance(exc_info.value.exceptions[0], asyncio.CancelledError)


async def test_exception_group_is_caught_and_marks_connector_unavailable() -> None:
    failing = FailingToolset(
        "get_tools",
        exception_factory=lambda: ExceptionGroup("x", [ConnectionError("down")]),
    )
    optional = OptionalToolset(failing, name="Linear")

    assert await optional.get_tools(_ctx()) == {}
    assert optional._unavailable is True
    assert failing.get_tools_calls == 2


async def test_enter_failure_recovers_at_discovery() -> None:
    failing = FailingToolset("enter")
    seen_tools: list[set[str]] = []
    seen_instructions: list[str] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen_tools.append({tool.name for tool in info.function_tools})
        seen_instructions.append(_request_instructions(messages))
        return ModelResponse(parts=[TextPart("answer")])

    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn),
        toolsets=[OptionalToolset(failing, name="Linear")],
    )

    result = await agent.run("hello")

    assert result.output == "answer"
    assert failing.enter_calls == 1
    assert failing.get_tools_calls == 1
    assert failing.exit_calls == 0
    assert seen_tools == [{"broken_connector_tool"}]
    assert "unavailable" not in seen_instructions[0]


async def test_enter_and_discovery_failure_fails_open_without_second_retry() -> None:
    failing = FailingToolset(("enter", "get_tools"))
    seen_instructions: list[str] = []

    async def model_fn(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen_instructions.append(_request_instructions(messages))
        return ModelResponse(parts=[TextPart("answer")])

    agent: Agent[None, str] = Agent(
        FunctionModel(model_fn),
        toolsets=[OptionalToolset(failing, name="Linear")],
    )

    result = await agent.run("hello")

    assert result.output == "answer"
    assert failing.enter_calls == 1
    assert failing.get_tools_calls == 1
    assert failing.exit_calls == 0
    assert "Linear" in seen_instructions[0]
    assert "unavailable" in seen_instructions[0]


async def test_teardown_failure_does_not_kill_a_finished_run() -> None:
    failing = FailingToolset("exit")
    agent: Agent[None, str] = Agent(
        TestModel(call_tools=[]),
        toolsets=[OptionalToolset(failing, name="Linear")],
    )

    result = await agent.run("hello")

    assert isinstance(result.output, str)
    assert failing.exit_calls == 1


async def test_discovery_failure_after_enter_still_exits_wrapped_toolset() -> None:
    failing = FailingToolset("get_tools")
    agent: Agent[None, str] = Agent(
        TestModel(call_tools=[]),
        toolsets=[OptionalToolset(failing, name="Linear")],
    )

    result = await agent.run("hello")

    assert isinstance(result.output, str)
    assert failing.enter_calls == 1
    assert failing.get_tools_calls == 2
    assert failing.exit_calls == 1


async def test_instruction_failure_drops_instructions_but_keeps_tools() -> None:
    # Tools for the step were already fetched successfully, so emitting the
    # unavailable notice would contradict the live, callable tools.
    failing = FailingToolset("get_instructions")
    optional = OptionalToolset(failing, name="Linear")
    ctx = _ctx()

    assert "broken_connector_tool" in await optional.get_tools(ctx)
    assert await optional.get_instructions(ctx) is None
    assert optional._unavailable is False
    assert "broken_connector_tool" in await optional.get_tools(ctx)


async def test_for_run_step_carries_failure_state_across_inner_replacement() -> None:
    class SteppingToolset(FailingToolset):
        async def for_run_step(self, ctx: RunContext[Any]) -> AbstractToolset[Any]:
            return SteppingToolset(self.phases)

    inner = SteppingToolset("get_tools")
    optional = OptionalToolset(inner, name="Linear")
    ctx = _ctx()

    assert await optional.get_tools(ctx) == {}
    assert optional._unavailable is True

    stepped = await optional.for_run_step(ctx)

    assert stepped is not optional
    assert isinstance(stepped, OptionalToolset)
    assert stepped._unavailable is True
    assert await stepped.get_tools(ctx) == {}


def test_visit_and_replace_reaches_mcp_leaf() -> None:
    class SentinelToolset(WrapperToolset[Any]):
        pass

    def visitor(toolset: AbstractToolset[Any]) -> AbstractToolset[Any]:
        if isinstance(toolset, MCPToolset):
            return SentinelToolset(toolset)
        return toolset

    optional = OptionalToolset(
        PrefixedToolset(
            MCPToolset("http://127.0.0.1:1/mcp", id="x"),
            "x",
        ),
        name="X",
    )

    result = optional.visit_and_replace(visitor)

    assert isinstance(result, OptionalToolset)
    assert isinstance(result.wrapped, PrefixedToolset)
    assert isinstance(result.wrapped.wrapped, SentinelToolset)


async def test_real_mcp_connection_failure_fails_open() -> None:
    agent: Agent[None, str] = Agent(
        TestModel(call_tools=[]),
        toolsets=[
            OptionalToolset(
                PrefixedToolset(
                    MCPToolset(
                        "http://127.0.0.1:1/mcp",
                        init_timeout=0.5,
                        id="dead",
                    ),
                    "dead",
                ),
                name="Dead",
            )
        ],
    )

    result = await agent.run("hello")
    request = result.all_messages()[0]

    assert isinstance(result.output, str)
    assert isinstance(request, ModelRequest)
    assert request.instructions is not None
    assert "Dead" in request.instructions
    assert "unavailable" in request.instructions
