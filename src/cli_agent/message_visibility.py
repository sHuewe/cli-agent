from __future__ import annotations

import copy
from typing import Any


MCP_USER_MESSAGE_ROLE = "cli_agent_user_message"
MCP_USER_MESSAGE_TYPE = "mcp_message_to_user"
CLI_AGENT_MESSAGE_TO_USER_META_KEY = "io.github.shuewe.cli-agent/messageToUser"


def _strip_meta(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _strip_meta(item)
            for key, item in value.items()
            if key != "_meta"
        }
    if isinstance(value, list):
        return [_strip_meta(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_meta(item) for item in value]
    return value


def messages_for_model(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a detached model-safe view of working messages."""

    return [
        _strip_meta(copy.deepcopy(message))
        for message in messages
        if message.get("role") != MCP_USER_MESSAGE_ROLE
    ]
