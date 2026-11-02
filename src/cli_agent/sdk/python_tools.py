"""In-process Python tool adapter for SDK agents."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
from dataclasses import dataclass
from typing import Any, Callable

from mcp.types import CallToolResult, TextContent
from pydantic import create_model

from cli_agent.state.agent_types import _RuntimeMcpServerConfig

_PY_NAME = re.compile("[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True)
class PythonTool:
    name: str
    function: Callable[..., Any]
    description: str
    model: type[Any]

    @property
    def schema(self) -> dict[str, Any]:
        return self.model.model_json_schema()


def make_python_tool(
    function: Callable[..., Any], *, name: str | None = None,
    description: str | None = None,
) -> PythonTool:
    if not callable(function):
        raise TypeError("Python tool must be callable")
    actual_name = name or getattr(function, "__name__", None)
    if not isinstance(actual_name, str) or not _PY_NAME.fullmatch(actual_name):
        raise ValueError("Invalid Python tool name")
    signature = inspect.signature(function)
    from typing import get_type_hints
    hints = get_type_hints(function)
    fields: dict[str, Any] = {}
    for parameter in signature.parameters.values():
        if parameter.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            raise TypeError("Python tools require named arguments")
        if parameter.name not in hints:
            raise TypeError("Python tools require annotated arguments")
        default = ... if parameter.default is inspect.Parameter.empty else parameter.default
        fields[parameter.name] = (hints[parameter.name], default)
    model = create_model(f"PythonTool_{actual_name}", **fields)
    model.model_json_schema()
    doc = description if description is not None else inspect.getdoc(function)
    return PythonTool(actual_name, function, doc or actual_name, model)


class PythonToolSession:
    def __init__(self, tools: tuple[PythonTool, ...]) -> None:
        self.tools = {tool.name: tool for tool in tools}

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        tool = self.tools.get(name)
        if tool is None:
            return CallToolResult(content=[TextContent(type="text", text="Unknown Python tool")], isError=True)
        try:
            kwargs = tool.model.model_validate(arguments).model_dump()
            if inspect.iscoroutinefunction(tool.function):
                value = await tool.function(**kwargs)
            else:
                value = await asyncio.to_thread(tool.function, **kwargs)
            output = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            if len(output.encode("utf-8")) > 5000:
                raise ValueError("Python tool result is too large")
            return CallToolResult(content=[TextContent(type="text", text=output)])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return CallToolResult(
                content=[TextContent(type="text", text=f"Python tool error: {type(exc).__name__}")],
                isError=True,
            )


def register_python_tools(runtime: Any, tools: tuple[PythonTool, ...]) -> None:
    if not tools:
        return
    if "python" in runtime._server_configs or "python" in runtime._active_servers:
        raise ValueError("MCP server name 'python' is reserved for SDK tools")
    session = PythonToolSession(tools)
    server = _RuntimeMcpServerConfig(name="python", transport="python", built_in=False)
    definitions = []
    routes = {}
    identities = {}
    for tool in tools:
        exposed_name = f"python__{tool.name}"
        if exposed_name in runtime._tool_routes:
            raise ValueError(f"Duplicate tool: {exposed_name}")
        definitions.append({"type": "function", "function": {
            "name": exposed_name, "description": tool.description,
            "parameters": tool.schema,
        }})
        routes[exposed_name] = (session, tool.name, server)
        identities[exposed_name] = ("python", tool.name)
    runtime._server_configs["python"] = server
    runtime._server_tools["python"] = definitions
    runtime._tool_routes.update(routes)
    runtime._tool_identities.update(identities)
    runtime._active_servers.add("python")
