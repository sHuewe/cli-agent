from __future__ import annotations

import json
import posixpath
from typing import Any

EXPECTED_KNOWLEDGE_TOOLS = {
    "knowledge_index",
    "knowledge_read",
}
KnowledgeCallKey = tuple[str, str]


def tool_result_text(result: Any) -> str:
    if getattr(result, "structuredContent", None) is not None:
        return json.dumps(result.structuredContent, ensure_ascii=False)

    parts: list[str] = []
    for item in result.content:
        text = getattr(item, "text", None)
        if text is not None:
            parts.append(text)
    if not parts:
        parts.append(str(result.content))
    prefix = "FEHLER: " if getattr(result, "isError", False) else ""
    return prefix + "\n".join(parts)


def _normalize_knowledge_path(path: Any, *, default: str = ".") -> str:
    value = str(path if path not in (None, "") else default)
    normalized = posixpath.normpath(value.replace("\\", "/"))
    return normalized or default


def _knowledge_call_key(
    tool_name: str,
    arguments: dict[str, Any],
) -> KnowledgeCallKey:
    normalized_arguments = dict(arguments)
    if tool_name == "knowledge_index":
        normalized_arguments["path"] = _normalize_knowledge_path(
            normalized_arguments.get("path"),
        )
    elif tool_name == "knowledge_read" and "path" in normalized_arguments:
        normalized_arguments["path"] = _normalize_knowledge_path(
            normalized_arguments["path"],
        )
    return (
        tool_name,
        json.dumps(
            normalized_arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _structured_tool_result(result: Any) -> dict[str, Any] | None:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        wrapped = structured.get("result")
        if isinstance(wrapped, dict) and "content" not in structured:
            return wrapped
        return structured

    try:
        parsed = json.loads(tool_result_text(result))
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    wrapped = parsed.get("result")
    if isinstance(wrapped, dict) and "content" not in parsed:
        return wrapped
    return parsed


def _knowledge_document(result: Any) -> dict[str, Any]:
    structured = _structured_tool_result(result)
    if structured is None:
        raise RuntimeError(
            "Das Ergebnis von knowledge_read enthält keine strukturierten Daten."
        )

    path = structured.get("path")
    kind = structured.get("kind")
    content = structured.get("content")
    if (
        not isinstance(path, str)
        or not isinstance(kind, str)
        or not isinstance(content, str)
    ):
        raise RuntimeError(
            "Das Ergebnis von knowledge_read enthält keinen gültigen Pfad, "
            "Dokumenttyp oder Dokumentinhalt."
        )

    return {
        "path": _normalize_knowledge_path(path),
        "kind": kind,
        "content": content,
        "warning": structured.get("warning"),
    }


def _knowledge_allowed_calls(result: Any) -> dict[str, set[str]]:
    structured = _structured_tool_result(result)
    if structured is None:
        return {}

    allowed_calls: dict[str, set[str]] = {}
    internal_links = structured.get("internal_links")
    if not isinstance(internal_links, list):
        return allowed_calls

    for link in internal_links:
        if not isinstance(link, dict) or link.get("exists") is False:
            continue
        path = link.get("path")
        next_tool = link.get("next_tool")
        if not isinstance(path, str) or next_tool not in EXPECTED_KNOWLEDGE_TOOLS:
            continue
        normalized_path = _normalize_knowledge_path(path)
        allowed_calls.setdefault(normalized_path, set()).add(str(next_tool))
    return allowed_calls


def _knowledge_result_without_repository_ids(result_text: str) -> str:
    try:
        result_value: Any = json.loads(result_text)
    except json.JSONDecodeError:
        return result_text

    def remove_concept_ids(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: remove_concept_ids(item)
                for key, item in value.items()
                if key != "concept_id"
            }
        if isinstance(value, list):
            return [remove_concept_ids(item) for item in value]
        return value

    return json.dumps(remove_concept_ids(result_value), ensure_ascii=False)


def _knowledge_result_with_agent_selection(
    result_text: str,
    *,
    selection_token: str,
    path: str,
) -> str:
    try:
        result_value: Any = json.loads(result_text)
    except json.JSONDecodeError:
        result_value = result_text
    return json.dumps(
        {
            "agent_selection": {
                "token": selection_token,
                "path": path,
                "instruction": (
                    "Use this exact token in selected_okf_tokens if this "
                    "Concept should be selected."
                ),
            },
            "okf_result": result_value,
        },
        ensure_ascii=False,
    )
