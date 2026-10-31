"""AG-UI 1.0 HTTP/SSE envelope validation and message normalization.

The control API remains distinct: this module only handles the standard
RunAgentInput boundary and does not grant any MCP or filesystem permissions.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

AG_UI_VERSION = "1.0"
MAX_AG_UI_MESSAGES = 512
MAX_AG_UI_TEXT_BYTES = 512 * 1024


@dataclass(frozen=True)
class AgUiInput:
    thread_id: str
    run_id: str
    prompt: str
    messages: list[dict[str, Any]]
    resume: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    context: list[dict[str, Any]]
    state: Any


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                raise ValueError("Ungültiger Nachrichteninhalt.")
            # Unsupported image/audio/document/video input is skipped rather
            # than treated as executable text or used as system instructions.
            if part.get("type") == "text":
                if not isinstance(part.get("text"), str):
                    raise ValueError("Ungültiger Text-Part.")
                parts.append(part["text"])
        return "\n".join(parts)
    raise ValueError("Ungültiger Nachrichteninhalt.")


def parse_run_input(body: dict[str, Any]) -> AgUiInput:
    """Validate known 1.0 members and reject malformed values.

    Unknown top-level fields are ignored for forward-compatible negotiation.
    """
    thread_id, run_id = body.get("threadId"), body.get("runId")
    if not all(isinstance(v, str) and 0 < len(v) <= 128 for v in (thread_id, run_id)):
        raise ValueError("threadId und runId sind erforderlich (maximal 128 Zeichen).")
    version = body.get("protocolVersion")
    if version is not None and not isinstance(version, str):
        raise ValueError("protocolVersion muss Text sein.")
    if version is not None and not version.startswith("1."):
        raise ValueError("Nicht unterstützte AG-UI-Version.")
    messages = body.get("messages")
    if not isinstance(messages, list) or len(messages) > MAX_AG_UI_MESSAGES:
        raise ValueError("messages muss eine begrenzte Nachrichtenliste sein.")
    normalized = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {
            "user", "assistant", "system", "developer", "tool", "activity", "reasoning"
        }:
            raise ValueError("Ungültige AG-UI-Nachricht.")
        mid = message.get("id")
        if not isinstance(mid, str) or not mid:
            raise ValueError("Jede AG-UI-Nachricht benötigt eine ID.")
        role = message["role"]
        if role in {"system", "developer"} and not isinstance(message.get("content"), str):
            raise ValueError("System-/Developer-Nachrichten benötigen Text.")
        if role == "assistant" and "content" in message and not isinstance(message["content"], str):
            raise ValueError("Assistant-Text muss eine Zeichenkette sein.")
        if role == "tool" and (
            not isinstance(message.get("toolCallId"), str) or "content" not in message
        ):
            raise ValueError("Tool-Nachrichten benötigen toolCallId und content.")
        if role == "activity" and (
            not isinstance(message.get("activityType"), str)
            or not isinstance(message.get("content"), dict)
        ):
            raise ValueError("Ungültige Activity-Nachricht.")
        if role == "user":
            if "content" not in message:
                raise ValueError("Benutzernachricht ohne Inhalt.")
            text = _message_text(message)
            if len(text.encode("utf-8")) > MAX_AG_UI_TEXT_BYTES:
                raise ValueError("Nachricht ist zu groß.")
        normalized.append(message)
    resume = body.get("resume", [])
    if not isinstance(resume, list):
        raise ValueError("resume muss eine Liste sein.")
    for entry in resume:
        if (not isinstance(entry, dict) or not isinstance(entry.get("interruptId"), str)
                or entry.get("status") not in {"resolved", "cancelled"}):
            raise ValueError("Ungültige Interrupt-Fortsetzung.")
    tools = body.get("tools", [])
    context = body.get("context", [])
    if not isinstance(tools, list) or not isinstance(context, list):
        raise ValueError("tools und context müssen Listen sein.")
    for tool in tools:
        if (not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                or not isinstance(tool.get("description"), str)):
            raise ValueError("Ungültige Tool-Definition.")
    for item in context:
        if (not isinstance(item, dict) or not isinstance(item.get("description"), str)
                or not isinstance(item.get("value"), str)):
            raise ValueError("AG-UI-Kontext benötigt description und value als Text.")
    if "state" in body and body["state"] is None:
        raise ValueError("state darf nicht null sein.")
    prompt = ""
    for message in reversed(normalized):
        if message["role"] == "user":
            prompt = _message_text(message)
            break
    if len(prompt.encode("utf-8")) > MAX_AG_UI_TEXT_BYTES:
        raise ValueError("Prompt ist zu groß.")
    return AgUiInput(
        thread_id=thread_id, run_id=run_id, prompt=prompt,
        messages=normalized, resume=resume, tools=tools,
        context=context, state=body.get("state", {}),
    )


def encode_sse(event: dict[str, Any]) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n"
