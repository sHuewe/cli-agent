"""Minimal, non-sensitive snapshot of one SDK agent session."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .usage import UsageSummary, summarize_usage, usage_history


@dataclass(frozen=True)
class SessionState:
    history_length: int
    enabled_servers: tuple[str, ...]
    disabled_servers: tuple[str, ...]
    approved_tools: tuple[str, ...]
    disabled_tools: tuple[str, ...]
    okf_enabled: bool
    usage: UsageSummary


def session_state(runtime: Any, *, usage_start: int) -> SessionState:
    active = frozenset(getattr(runtime, "_active_servers", ()))
    configured = set(getattr(runtime, "_server_configs", {}))
    # Some disabled servers are disconnected, but still configured in the
    # instance's original MCP server definitions.
    configured.update(
        str(server.name)
        for server in getattr(runtime, "mcp_servers", ())
        if getattr(server, "name", None)
    )
    return SessionState(
        history_length=len(getattr(runtime, "history", ())),
        enabled_servers=tuple(sorted(active)),
        disabled_servers=tuple(sorted(configured - active)),
        approved_tools=tuple(sorted(getattr(runtime, "_session_approved_tools", ()))),
        disabled_tools=tuple(sorted(getattr(runtime, "_disabled_tools", ()))),
        okf_enabled=bool(getattr(runtime, "_okf_enabled", False)),
        usage=summarize_usage(usage_history(getattr(runtime, "model_client", None))[usage_start:]),
    )
