from __future__ import annotations

import ast
import copy
import json
import re
from typing import Any, Self

from pydantic_ai import RunContext, ToolDefinition
from pydantic_ai._instructions import AgentInstructions
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ToolCallPart
from pydantic_ai.toolsets import AbstractToolset, ToolsetTool
from pydantic_core import SchemaValidator, core_schema

from hondo.context import current_scope
from hondo.crypto import TokenCipher
from hondo.mcp.content import model_result
from hondo.mcp.execution import MCPExecutor
from hondo.mcp.models import MCPSelection, RemoteTool
from hondo.mcp.oauth import digest
from hondo.mcp.sessions import MCPSessions
from hondo.mcp.store import MCPStore
from hondo.runtime.durable import step
from hondo.runtime.model_selection import RunModel


def tool_name(selection: MCPSelection, tool: RemoteTool) -> str:
    return f"mcp_{selection.connection_id.hex[:16]}_{digest(tool.name)[:12]}"


def selections(ctx: RunContext[RunModel]) -> list[MCPSelection]:
    scope = current_scope()
    if scope.is_dm:
        return []
    return [
        item
        for item in ctx.deps.mcp_connections
        if item.all_channels or scope.channel in item.channels
    ]


class MCPAccess(AbstractCapability[RunModel]):
    def __init__(self, store: MCPStore) -> None:
        self.store = store

    def get_instructions(self) -> AgentInstructions[RunModel]:
        return self._current_access

    def _current_access(self, ctx: RunContext[RunModel]) -> str:
        current = selections(ctx)
        count = sum(len(item.tools) for item in current)
        lines = [
            f"{count} connector tools are available in this channel for this run. "
            "Their functions are listed in the current run_code catalog."
            if count
            else "No connector tools are available in this channel for this run."
        ]
        lines += [
            f"{item.name} needs an admin to reconnect it, so its tools are not available."
            for item in current
            if item.sign_in_required
        ]
        status = "\n".join(lines)
        return f"<current_connector_access>\n{status}\n</current_connector_access>"

    async def wrap_tool_execute(
        self,
        ctx: RunContext[RunModel],
        *,
        call: ToolCallPart,
        tool_def: ToolDefinition,
        args: Any,
        handler: Any,
    ) -> Any:
        if tool_def.name == "run_code":
            used = _connector_functions(args["code"])
            # Check before execution: a refused call later in the script must
            # not leave earlier writes completed or turn into a caught Python
            # error. Recorded, because recovery runs the script again and its
            # calls replay from their records.
            if used and not await step("mcp_script_access", self._available, ctx, used):
                return {
                    "error": (
                        "A connector tool referenced by this script is unavailable "
                        "for this run. This script was not run. Do not retry the "
                        "unavailable tool. Use only the current run_code catalog. "
                        "If the task needs that tool, explain that you cannot get "
                        "a fresh result and an admin can check access in Connectors."
                    )
                }
        return await handler(args)

    async def _available(self, ctx: RunContext[RunModel], used: set[str]) -> bool:
        scope = current_scope()
        current = selections(ctx)
        allowed = await self.store.callable_tools(
            scope.key.team, scope.channel, current, listed_only=ctx.deps.shared_channel
        )
        return used <= {
            tool_name(selection, tool)
            for selection in current
            for tool in selection.tools
            if (selection.connection_id, tool.name) in allowed
        }


def _connector_functions(code: str) -> set[str]:
    try:
        script = ast.parse(code)
    except SyntaxError:
        return set()
    return {
        node.id
        for node in ast.walk(script)
        if isinstance(node, ast.Name)
        and isinstance(node.ctx, ast.Load)
        and re.fullmatch(r"mcp_[0-9a-f]{16}_[0-9a-f]{12}", node.id)
    }


class WorkspaceMCPTools(AbstractToolset[RunModel]):
    def __init__(self, store: MCPStore, cipher: TokenCipher, base_url: str) -> None:
        self.executor = MCPExecutor(store, cipher, base_url)
        self.sessions = MCPSessions(self.executor.connect)

    @property
    def id(self) -> str:
        return "workspace_mcp"

    async def for_run(self, ctx: RunContext[RunModel]) -> WorkspaceMCPTools:
        run = copy.copy(self)
        run.sessions = MCPSessions(self.executor.connect)
        return run

    async def __aenter__(self) -> Self:
        await self.sessions.__aenter__()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.sessions.__aexit__(*args)

    async def get_tools(self, ctx: RunContext[RunModel]) -> dict[str, ToolsetTool[RunModel]]:
        result = {}
        for selection in selections(ctx):
            for tool in selection.tools:
                name = tool_name(selection, tool)
                result[name] = ToolsetTool(
                    toolset=self,
                    max_retries=0,
                    args_validator=SchemaValidator(
                        core_schema.typed_dict_schema(
                            {
                                "arguments": core_schema.typed_dict_field(
                                    core_schema.dict_schema(keys_schema=core_schema.str_schema())
                                )
                            },
                            extra_behavior="forbid",
                        )
                    ),
                    tool_def=ToolDefinition(
                        name=name,
                        description=(
                            f"{selection.name}: {tool.name}\n{tool.description}\n"
                            "Pass the remote tool's JSON arguments as the arguments dictionary. "
                            "Media results are a list; return the list as the final expression to view it. "
                            f"Argument schema: {json.dumps(tool.input_schema)}"
                        ),
                        parameters_json_schema={
                            "type": "object",
                            "properties": {
                                "arguments": {"type": "object", "additionalProperties": True}
                            },
                            "required": ["arguments"],
                            "additionalProperties": False,
                        },
                        return_schema={},
                        sequential=True,
                        kind="unapproved" if tool.name in selection.ask_tools else "function",
                        metadata={"code_mode": True},
                    ),
                )
        return result

    async def call_tool(
        self,
        name: str,
        tool_args: dict[str, Any],
        ctx: RunContext[RunModel],
        tool: ToolsetTool[RunModel],
    ) -> Any:
        match = next(
            ((s, t) for s in selections(ctx) for t in s.tools if tool_name(s, t) == name), None
        )
        if match is None:
            return {"error": "This tool is unavailable."}
        selection, remote_tool = match
        result = await self.executor.execute(
            selection, name, remote_tool, tool_args["arguments"], ctx, self.sessions
        )
        return model_result(result)
