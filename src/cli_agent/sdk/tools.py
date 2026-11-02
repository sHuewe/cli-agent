"""Immutable public metadata for tools currently registered in the SDK agent."""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolInfo:
    name: str
    server: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    approval_required: bool = False


def available_tools(runtime: Any) -> tuple[ToolInfo, ...]:
    """Return currently offered tools, not privileged/inactive capabilities.

    An inactive external server may have no live tool schema; the SDK does not
    reconnect it just to display metadata. Enabling it refreshes its tools.
    """
    details = []
    routes = getattr(runtime, "_tool_routes", {})
    for tool in runtime._model_tools():
        function = tool.get("function", {})
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        route = routes.get(name)
        if route is None:
            server = "agent"
            requires_approval = False
        else:
            _session, native_name, server_config = route
            server = server_config.name
            requires_approval = runtime._requires_approval(
                server_config, native_name, exposed_name=name,
            )
        details.append(ToolInfo(
            name=name,
            server=server,
            description=str(function.get("description") or ""),
            input_schema=copy.deepcopy(function.get("parameters") or {}),
            approval_required=requires_approval,
        ))
    return tuple(details)
