"""Frontend tools defined by a remote AG-UI application.

Tools advertised by a client are *not* MCP tools. They are returned to the
application as AG-UI tool-call events, and the application sends their results
back as ToolMessage objects on the following RunAgentInput.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_FRONTEND_TOOLS = 32
MAX_TOOL_DEFINITION_BYTES = 64 * 1024
MAX_FRONTEND_RESULTS_BYTES = 256 * 1024
FRONTEND_FAILURE_META_KEY = "io.github.shuewe.cli-agent/frontend_tool_failed"


@dataclass(frozen=True)
class FrontendToolCall:
    id: str
    name: str
    arguments: str


class FrontendToolHandoff(Exception):
    """The agent yielded tool calls for the client, without executing them."""

    def __init__(self, calls: list[FrontendToolCall], assistant: dict[str, Any]):
        super().__init__("AG-UI frontend tools must be executed by the client.")
        self.calls = tuple(calls)
        self.assistant = assistant


def normalize_frontend_tools(
    offered: list[dict[str, Any]], *, server_names: set[str]
) -> dict[str, dict[str, Any]]:
    """Validate and format the application's tools for the model.

    Name clashes are rejected even for disabled MCP tools, to ensure that
    an application cannot redirect or shadow backend tool execution.
    """
    if len(offered) > MAX_FRONTEND_TOOLS:
        raise ValueError("Too many AG-UI frontend tools.")
    normalized: dict[str, dict[str, Any]] = {}
    for item in offered:
        name = item["name"]
        if (
            not name or len(name) > 128 or not name.isascii()
            or not all(c.isalnum() or c in "_-" for c in name)
        ):
            raise ValueError("Invalid frontend tool name.")
        if name in normalized or name in server_names:
            raise ValueError("Duplicate or MCP-colliding tool name.")
        description = item["description"]
        if len(description) > 4096:
            raise ValueError("Frontend tool description is too long.")
        schema = item.get("parameters", {"type": "object", "properties": {}})
        if not isinstance(schema, dict):
            raise ValueError("Frontend tool parameters must be a JSON schema object.")
        from jsonschema import Draft202012Validator
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:
            raise ValueError("Invalid frontend tool parameter schema.") from exc
        try:
            encoded = json.dumps(item, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("Invalid frontend tool definition.") from exc
        if len(encoded.encode("utf-8")) > MAX_TOOL_DEFINITION_BYTES:
            raise ValueError("Frontend tool definition is too large.")
        normalized[name] = {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": schema,
            },
        }
    return normalized


def checked_frontend_results(
    messages: list[dict[str, Any]],
    expected: tuple[FrontendToolCall, ...],
) -> list[dict[str, Any]]:
    """Validate that every awaited frontend call has one matching tool result.

    Ignore old tool messages belonging to previous runs. The server never
    accepts a new answer for an id not issued in the pending tool batch.
    """
    required = {call.id for call in expected}
    originals = {call.id: call for call in expected}
    if len(required) != len(expected):
        raise ValueError("Duplicate frontend tool call ID.")
    if not required:
        raise ValueError("No pending frontend tools.")
    results: dict[str, dict[str, Any]] = {}
    assistant_found = False
    for message in messages:
        if message.get("role") == "assistant":
            listed = {
                call.get("id"): call for call in message.get("toolCalls", [])
                if isinstance(call, dict)
            }
            if required.issubset(listed):
                for call_id, issued in originals.items():
                    info = listed[call_id].get("function", {})
                    if (not isinstance(info, dict)
                            or info.get("name") != issued.name
                            or info.get("arguments") != issued.arguments):
                        raise ValueError("Frontend tool definition changed during resume.")
                assistant_found = True
        elif message.get("role") == "tool" and message.get("toolCallId") in required:
            call_id = message["toolCallId"]
            if call_id in results:
                raise ValueError("Frontend tool result was submitted more than once.")
            content = message.get("content")
            if isinstance(content, list):
                content = "\n".join(
                    part["text"] for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                )
            if not isinstance(content, str):
                raise ValueError("Frontend tool result must be text or content parts.")
            if len(content.encode("utf-8")) > MAX_FRONTEND_RESULTS_BYTES:
                raise ValueError("Frontend tool result is too large.")
            error = message.get("error")
            if error is not None:
                if not isinstance(error, str):
                    raise ValueError("Frontend tool error must be text.")
                content = "ERROR: " + error[:4096] + "\n" + content
            results[call_id] = {
                "role": "tool", "tool_call_id": call_id, "content": content,
                **({"_meta": {FRONTEND_FAILURE_META_KEY: True}} if error is not None else {}),
            }
    if not assistant_found:
        raise ValueError("Pending assistant message containing the frontend tool calls is missing.")
    if set(results) != required:
        raise ValueError("Not all frontend tool results were submitted.")
    return [results[call.id] for call in expected]


def history_for_model(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lossless text/function-call history for the existing LLM adapter.

    Untrusted system/developer messages are NOT promoted into cli-agent's
    system prompt. Rich content parts unsupported by the model are ignored.
    """
    history: list[dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        if role == "user":
            content = message.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    part["text"] for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                )
            history.append({"role": "user", "content": content})
        elif role == "assistant":
            value: dict[str, Any] = {"role": "assistant", "content": message.get("content") or ""}
            calls = message.get("toolCalls")
            if isinstance(calls, list) and calls:
                value["tool_calls"] = [
                    {"id": call["id"], "type": "function", "function": {
                        "name": call["function"]["name"],
                        "arguments": call["function"]["arguments"],
                    }}
                    for call in calls
                    if isinstance(call, dict) and isinstance(call.get("function"), dict)
                    and isinstance(call.get("id"), str)
                    and isinstance(call["function"].get("name"), str)
                    and isinstance(call["function"].get("arguments"), str)
                ]
            history.append(value)
        elif role == "tool":
            content = message.get("content", "")
            if isinstance(content, list):
                content = "\n".join(
                    part["text"] for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                    and isinstance(part.get("text"), str)
                )
            if isinstance(message.get("error"), str):
                content = "ERROR: " + message["error"][:4096] + "\n" + content
            history.append({"role": "tool", "tool_call_id": message["toolCallId"], "content": content})
    return history
