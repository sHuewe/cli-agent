from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mcp import ClientSession

from .config import McpServerConfig


@dataclass(frozen=True)
class _RuntimeMcpServerConfig:
    """Minimal MCP config used only for the internal OKF stdio process."""

    name: str
    transport: str = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    compress_result: bool = False
    compress_min_chars: int = 12_000
    built_in: bool = True


ServerConfig = McpServerConfig | _RuntimeMcpServerConfig
ToolRoute = tuple[ClientSession, str, ServerConfig]
ApprovalCallback = Callable[[str, dict[str, Any]], Awaitable[bool | str]]
