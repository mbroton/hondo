from __future__ import annotations

from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness import CodeMode

from hondo.agent.tool_boundary import ToolBoundary
from hondo.toolsets import prepare_sequential_tools


async def test_code_mode_calls_selected_tools_and_preserves_state_between_steps() -> None:
    calls: list[str] = []
    returns: list[str] = []
    step = 0

    def model(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nonlocal step
        assert {tool.name for tool in info.function_tools} == {"run_code", "reply"}
        for part in messages[-1].parts:
            if isinstance(part, ToolReturnPart) and part.tool_name == "run_code":
                assert isinstance(part.content, str)
                returns.append(part.content)
        step += 1
        if step == 1:
            return ModelResponse(
                parts=[ToolCallPart("run_code", {"code": "answer = linear_lookup()\nanswer"})]
            )
        if step == 2:
            return ModelResponse(parts=[ToolCallPart("run_code", {"code": "answer + ' ready'"})])
        return ModelResponse(parts=[TextPart("Done.")])

    tools: FunctionToolset[None] = FunctionToolset()
    agent = Agent(
        FunctionModel(model),
        deps_type=type(None),
        name="code_mode_compatibility",
        toolsets=[tools.prepared(prepare_sequential_tools)],
        capabilities=[
            ToolBoundary(),
            CodeMode(tools=lambda ctx, td: td.name.startswith("linear_")),
        ],
    )

    @tools.tool_plain
    async def linear_lookup() -> str:
        """Read a fixed ticket for this test."""
        calls.append("lookup")
        return "ENG-42"

    @agent.tool_plain
    def reply(text: str) -> str:
        """A native tool that stays outside the sandbox."""
        return text

    result = await agent.run("Read the ticket, then use it in the next step.")
    assert result.output == "Done."
    assert calls == ["lookup"]
    assert returns == ["ENG-42", "ENG-42 ready"]
