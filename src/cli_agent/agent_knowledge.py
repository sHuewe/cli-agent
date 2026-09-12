from __future__ import annotations

import json
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from mcp import ClientSession

from .agent_knowledge_support import (  # noqa: F401
    EXPECTED_KNOWLEDGE_TOOLS,
    KnowledgeCallKey,
    _knowledge_allowed_calls,
    _knowledge_call_key,
    _knowledge_document,
    _knowledge_result_with_agent_selection,
    _knowledge_result_without_repository_ids,
    _normalize_knowledge_path,
    tool_result_text,
)
from .config import McpServerConfig

DEFAULT_OKF_MAX_TOOL_CALLS = 200
DEFAULT_OKF_MAX_CONCEPT_READS = 180
MAX_PREMATURE_KNOWLEDGE_RETRIES = 2
MAX_KNOWLEDGE_SELECTION_RETRIES = 2


class OkfConfigLike(Protocol):
    """Structural type expected for the optional ``config.okf`` value."""

    repository: str | Path
    max_tool_calls: int
    max_concept_reads: int
    max_read_bytes: int
    max_index_entries: int
    compress_min_chars: int
    required: bool


@dataclass(frozen=True)
class _OkfOptions:
    repository: Path
    max_tool_calls: int = DEFAULT_OKF_MAX_TOOL_CALLS
    max_concept_reads: int = DEFAULT_OKF_MAX_CONCEPT_READS
    max_read_bytes: int = 256_000
    max_index_entries: int = 200
    compress_min_chars: int = 12_000
    required: bool = True


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
ApprovalCallback = Callable[[str, dict[str, Any]], Awaitable[bool]]

WRITE_TOOLS = frozenset(
    {
        "write_file",
        "delete_file",
        "make_directory",
        "copy_file",
    }
)
COMPOSE_MUTATING_TOOLS = frozenset(
    {
        "compose_up_all",
        "compose_up",
        "compose_down",
        "compose_restart",
    }
)
EXECUTING_TOOLS = frozenset({"validate_python_project"})


@dataclass
class _KnowledgeRunState:
    seen_calls: set[KnowledgeCallKey] = field(default_factory=set)
    allowed_calls: dict[str, set[str]] = field(default_factory=dict)
    concepts: dict[str, dict[str, Any]] = field(default_factory=dict)
    selection_tokens_by_path: dict[str, str] = field(default_factory=dict)
    successful_followup_calls: int = 0

    def add_allowed_calls(self, calls: dict[str, set[str]]) -> None:
        for path, tool_names in calls.items():
            self.allowed_calls.setdefault(path, set()).update(tool_names)

    def register_concept(self, document: dict[str, Any]) -> str | None:
        if document.get("kind") != "concept":
            return None

        path = str(document["path"])
        selection_token = self.selection_tokens_by_path.get(path)
        if selection_token is None:
            alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
            while True:
                suffix = "".join(secrets.choice(alphabet) for _ in range(6))
                selection_token = f"OKFSEL-{suffix}"
                if selection_token not in self.concepts:
                    break
            self.selection_tokens_by_path[path] = selection_token
        self.concepts[selection_token] = document
        return selection_token


def _validate_knowledge_selection(
    answer: str,
    state: _KnowledgeRunState,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        selection = json.loads(answer)
    except json.JSONDecodeError:
        return None, "Die finale Antwort ist kein gültiges JSON-Objekt."
    if not isinstance(selection, dict):
        return None, "Die finale Antwort ist kein JSON-Objekt."

    found_content = selection.get("found_content")
    if not isinstance(found_content, bool):
        return selection, "`found_content` muss ein boolescher Wert sein."

    selected_tokens = selection.get("selected_okf_tokens")
    if not isinstance(selected_tokens, list) or not all(
        isinstance(token, str) for token in selected_tokens
    ):
        return (
            selection,
            "`selected_okf_tokens` muss eine Liste von Zeichenketten sein.",
        )

    warnings = selection.get("warnings", [])
    if not isinstance(warnings, list) or not all(
        isinstance(warning, str) for warning in warnings
    ):
        return selection, "`warnings` muss eine Liste von Zeichenketten sein."

    if found_content:
        if not selected_tokens:
            return (
                selection,
                "Bei `found_content: true` fehlt eine `selected_okf_tokens`-Auswahl.",
            )
        unknown_tokens = sorted(
            {token for token in selected_tokens if token not in state.concepts}
        )
        if unknown_tokens:
            return (
                selection,
                "Diese Tokens gehören zu keinem erfolgreich gelesenen Concept: "
                + ", ".join(unknown_tokens),
            )
    elif selected_tokens:
        return (
            selection,
            "Bei `found_content: false` muss `selected_okf_tokens` leer sein.",
        )
    else:
        reason_code = selection.get("reason_code")
        if reason_code not in {"not_applicable", "not_found"}:
            return (
                selection,
                "Bei `found_content: false` muss `reason_code` entweder "
                "`not_applicable` oder `not_found` sein.",
            )
        if reason_code == "not_applicable" and state.successful_followup_calls > 0:
            return (
                selection,
                "`not_applicable` ist nur vor Beginn der Repository-Recherche "
                "zulässig.",
            )
        if reason_code == "not_found" and not state.concepts:
            return (
                selection,
                "`not_found` ist erst zulässig, nachdem mindestens ein "
                "Concept-Dokument erfolgreich gelesen und geprüft wurde.",
            )

    return selection, None


def _fallback_knowledge_selection(
    selection: dict[str, Any] | None,
    state: _KnowledgeRunState,
    *,
    reason: str,
) -> dict[str, Any]:
    selected_tokens: list[str] = []
    if selection is not None:
        raw_tokens = selection.get("selected_okf_tokens")
        if isinstance(raw_tokens, list):
            selected_tokens = list(
                dict.fromkeys(
                    token
                    for token in raw_tokens
                    if isinstance(token, str) and token in state.concepts
                )
            )

    strategy = "valid_tokens_from_invalid_response"
    if not selected_tokens:
        selected_tokens = list(state.concepts)
        strategy = "all_read_concepts" if selected_tokens else "no_read_concepts"

    warnings: list[str] = []
    if selection is not None:
        raw_warnings = selection.get("warnings")
        if isinstance(raw_warnings, list):
            warnings = list(
                dict.fromkeys(
                    warning for warning in raw_warnings if isinstance(warning, str)
                )
            )

    fallback = {
        "strategy": strategy,
        "reason": reason,
    }
    if selected_tokens:
        return {
            "found_content": True,
            "selected_okf_tokens": selected_tokens,
            "warnings": warnings,
            "agent_fallback": fallback,
        }

    return {
        "found_content": False,
        "selected_okf_tokens": [],
        "warnings": warnings,
        "reason_code": "retrieval_incomplete",
        "reason": ("Der Knowledge-Lauf konnte keine belegte Concept-Auswahl erzeugen."),
        "agent_fallback": fallback,
    }


def _assemble_knowledge_payload(
    selection: dict[str, Any],
    concepts: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    selected_tokens = selection.get("selected_okf_tokens")
    if not isinstance(selected_tokens, list) or not selected_tokens:
        raise RuntimeError(
            "Der OKF-Wissenslauf hat keine ausgewählten OKF-Tokens geliefert."
        )

    unique_tokens: list[str] = []
    seen_tokens: set[str] = set()
    for token in selected_tokens:
        if not isinstance(token, str):
            raise RuntimeError(
                "Der OKF-Wissenslauf hat einen ungültigen OKF-Token geliefert."
            )
        if token not in seen_tokens:
            unique_tokens.append(token)
            seen_tokens.add(token)

    unknown_tokens = [token for token in unique_tokens if token not in concepts]
    if unknown_tokens:
        raise RuntimeError(
            "Der OKF-Wissenslauf hat unbekannte OKF-Tokens ausgewählt: "
            + ", ".join(unknown_tokens)
        )

    raw_warnings = selection.get("warnings", [])
    if not isinstance(raw_warnings, list) or not all(
        isinstance(warning, str) for warning in raw_warnings
    ):
        raise RuntimeError("Der OKF-Wissenslauf hat ungültige Warnungen geliefert.")

    warnings = list(dict.fromkeys(raw_warnings))
    content: list[dict[str, str]] = []
    for token in unique_tokens:
        document = concepts[token]
        path = str(document["path"])
        content.append(
            {
                "concept": path,
                "content_type": "full",
                "content": document["content"],
            }
        )
        warning = document.get("warning")
        if isinstance(warning, str) and warning:
            formatted_warning = f"{path}: {warning}"
            if formatted_warning not in warnings:
                warnings.append(formatted_warning)

    return {
        "found_content": True,
        "content": content,
        "warnings": warnings,
    }
