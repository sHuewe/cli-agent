from __future__ import annotations

import copy
from typing import Any


MCP_USER_MESSAGE_ROLE = "cli_agent_user_message"
MCP_USER_MESSAGE_TYPE = "mcp_message_to_user"
CLI_AGENT_MESSAGE_TO_USER_META_KEY = "io.github.shuewe.cli-agent/messageToUser"


def messages_for_model(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a detached model-safe view of working messages.

    Only protocol metadata attached to the message envelope is removed.
    Application payloads, including tool arguments that happen to use a
    legitimate key named `_meta`, are preserved unchanged.
    """

    result: list[dict[str, Any]] = []
    for message in messages:
        if message.get("role") == MCP_USER_MESSAGE_ROLE:
            continue
        model_message = copy.deepcopy(message)
        model_message.pop("_meta", None)
        result.append(model_message)
    return result
