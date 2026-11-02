"""Direct SDK MCP calls outside model conversations."""
from __future__ import annotations

import copy
import json
from dataclasses import dataclass
from typing import Any

from cli_agent.security.mcp_limits import (
    MCP_TOOL_CALL_TIMEOUT_SECONDS, await_mcp_operation,
    enforce_mcp_tool_result_limit,
)
from cli_agent.security.mcp_schema_guard import validate_mcp_tool_arguments


def _result_text(result: Any) -> str:
    """Keep MCP text content distinct from optional structured output."""
    content = getattr(result, "content", ())
    parts = [
        item.text for item in content
        if isinstance(getattr(item, "text", None), str)
    ]
    if parts:
        return "\n".join(parts)
    structured = getattr(result, "structuredContent", None)
    if structured is not None:
        return json.dumps(structured, ensure_ascii=False)
    return str(content)


@dataclass(frozen=True)
class DirectMcpResult:
    name: str
    text: str
    is_error: bool
    structured_content: Any = None


class McpInterface:
    """Instance-bound facade; executes calls on the owning worker task."""

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    async def call_tool(
        self, name: str, arguments: Any = None, /, **kwargs: Any,
    ) -> DirectMcpResult:
        if not isinstance(name, str) or not name:
            raise ValueError("Tool name must be a nonempty string")
        if arguments is not None and kwargs:
            raise TypeError("Pass arguments or keyword arguments, not both")
        payload = kwargs if kwargs else arguments
        return await self._agent._request("mcp_call_tool", (name, payload))


async def execute_direct_tool(runtime: Any, name: str, supplied: Any) -> DirectMcpResult:
    """No LLM run, history entry, evidence, or event callback mutation."""
    if not runtime._tool_is_enabled(name):
        raise ValueError(f"Tool {name!r} is disabled")
    route = runtime._tool_routes.get(name)
    if route is None:
        raise ValueError(f"Unknown or unavailable MCP tool: {name!r}")
    session, native_name, server = route
    if server.name not in runtime._active_servers:
        raise ValueError(f"MCP server {server.name!r} is disabled")
    schema = runtime._tool_input_schema(name, phase="main")
    if schema is None:
        raise RuntimeError(f"Missing input schema for {name!r}")
    properties = schema.get("properties", {})
    if supplied is None:
        args = {}
    elif isinstance(supplied, dict):
        args = dict(supplied)
    else:
        if not isinstance(properties, dict) or len(properties) != 1:
            raise TypeError("Positional value requires exactly one schema property")
        key = next(iter(properties))
        args = {key: supplied}

    validation_error = validate_mcp_tool_arguments(
        tool_name=name, schema=schema, arguments=args,
    )
    if validation_error is not None:
        raise ValueError(f"Invalid MCP tool arguments: {validation_error}")
    if runtime._requires_approval(server, native_name, exposed_name=name):
        approved = await runtime._approve_tool_call(name, args)
        if not approved:
            raise PermissionError(f"Tool invocation denied: {name}")
    result = await await_mcp_operation(
        session.call_tool(native_name, args),
        timeout_seconds=MCP_TOOL_CALL_TIMEOUT_SECONDS,
        operation=f"MCP-Tool {name!r}",
    )
    text = enforce_mcp_tool_result_limit(_result_text(result))
    return DirectMcpResult(
        name=name, text=text,
        is_error=bool(getattr(result, "isError", False)),
        structured_content=copy.deepcopy(getattr(result, "structuredContent", None)),
    )
