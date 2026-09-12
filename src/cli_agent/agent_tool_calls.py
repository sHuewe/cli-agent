from __future__ import annotations

import copy
import json
import logging
from typing import Any

from .agent_knowledge import (
    KnowledgeCallKey,
    ToolRoute,
    _KnowledgeRunState,
    _knowledge_allowed_calls,
    _knowledge_call_key,
    _knowledge_document,
    _knowledge_result_with_agent_selection,
    _knowledge_result_without_repository_ids,
    _normalize_knowledge_path,
    tool_result_text,
)

logger = logging.getLogger("cli_agent.agent_tool_calls")


async def process_tool_calls(
    agent: Any,
    *,
    assistant_message: dict[str, Any],
    messages: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    routes: dict[str, ToolRoute],
    enabled_server_names: set[str] | None,
    max_tool_calls: int,
    phase: str,
    knowledge_state: _KnowledgeRunState | None,
    max_concept_reads: int | None,
    calls: int,
    knowledge_selection_only_mode: bool,
    transient_rejections: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]],
) -> tuple[int, bool]:
    knowledge_concept_limit_notice_pending = False

    primary_knowledge_call = tool_calls[0] if phase == "knowledge" else None
    for tool_call in tool_calls:
        if (
            primary_knowledge_call is not None
            and tool_call is not primary_knowledge_call
        ):
            function = tool_call.get("function", {})
            exposed_name = function.get("name")
            tool_message = agent._append_tool_error(
                messages,
                tool_call,
                exposed_name,
                "Im Knowledge-Lauf ist pro Modellantwort genau ein "
                "OKF-Tool-Aufruf zulässig. Werte zunächst das Ergebnis "
                "des ersten Aufrufs aus und entscheide danach, ob ein "
                "weiterer Aufruf erforderlich ist.",
            )
            transient_rejections.append((assistant_message, tool_call, tool_message))
            logger.info(
                "knowledge_parallel_tool_call_rejected name=%s",
                exposed_name,
            )
            continue

        calls += 1
        if calls > max_tool_calls:
            raise RuntimeError(
                f"Abbruch in Phase {phase!r} nach {max_tool_calls} Tool-Aufrufen."
            )

        function = tool_call.get("function", {})
        exposed_name = function.get("name")
        arguments = function.get("arguments") or {}
        if not isinstance(arguments, dict):
            try:
                arguments = json.loads(arguments)
            except (TypeError, json.JSONDecodeError):
                tool_message = agent._append_tool_error(
                    messages,
                    tool_call,
                    exposed_name,
                    "Der Tool-Aufruf enthält keine gültigen JSON-Argumente.",
                )
                transient_rejections.append(
                    (assistant_message, tool_call, tool_message)
                )
                continue
            if not isinstance(arguments, dict):
                tool_message = agent._append_tool_error(
                    messages,
                    tool_call,
                    exposed_name,
                    "Die Tool-Argumente müssen ein JSON-Objekt sein.",
                )
                transient_rejections.append(
                    (assistant_message, tool_call, tool_message)
                )
                continue

        route = routes.get(exposed_name)
        if route is None:
            tool_message = agent._append_tool_error(
                messages,
                tool_call,
                exposed_name,
                f"Das MCP-Tool {exposed_name!r} existiert nicht oder ist "
                "aktuell nicht verfügbar. Verwende ausschließlich ein "
                "Tool aus der aktuellen Toolliste.",
            )
            transient_rejections.append((assistant_message, tool_call, tool_message))
            continue
        session, original_name, server_config = route
        if (
            enabled_server_names is not None
            and server_config.name not in enabled_server_names
        ):
            tool_message = agent._append_tool_error(
                messages,
                tool_call,
                exposed_name,
                f"Das MCP-Tool {exposed_name!r} ist nicht verfügbar, weil "
                f"der MCP-Server {server_config.name!r} deaktiviert ist. "
                "Rufe es nicht erneut auf und verwende ausschließlich ein "
                "Tool aus der aktuellen Toolliste.",
            )
            transient_rejections.append((assistant_message, tool_call, tool_message))
            continue

        if agent._requires_approval(
            server_config,
            original_name,
            exposed_name=exposed_name,
        ):
            approved = await agent._approve_tool_call(
                exposed_name,
                arguments,
            )
            if not approved:
                tool_message = agent._append_tool_error(
                    messages,
                    tool_call,
                    exposed_name,
                    "Dieser mutierende Tool-Aufruf wurde nicht durch "
                    "eine explizite Benutzerfreigabe autorisiert. "
                    "Führe ihn nicht erneut aus, bevor der Benutzer "
                    "die Aktion freigegeben hat.",
                )
                transient_rejections.append(
                    (assistant_message, tool_call, tool_message)
                )
                continue

        knowledge_call_key: KnowledgeCallKey | None = None
        knowledge_selection_token: str | None = None
        knowledge_document_path: str | None = None
        if phase == "knowledge" and knowledge_state is not None:
            knowledge_call_key = _knowledge_call_key(
                original_name,
                arguments,
            )
            if knowledge_call_key in knowledge_state.seen_calls:
                tool_message = agent._append_tool_error(
                    messages,
                    tool_call,
                    exposed_name,
                    "Dieser identische OKF-Aufruf wurde bereits "
                    "erfolgreich ausgeführt. Verwende das vorhandene "
                    "Ergebnis und rufe ihn nicht erneut auf.",
                )
                transient_rejections.append(
                    (assistant_message, tool_call, tool_message)
                )
                if agent.logging_config.log_tool_calls:
                    logger.info(
                        "knowledge_tool_call_duplicate name=%s arguments=%s",
                        exposed_name,
                        json.dumps(arguments, ensure_ascii=False),
                    )
                continue

            requested_path = _normalize_knowledge_path(
                arguments.get("path"),
            )
            allowed_tools = knowledge_state.allowed_calls.get(
                requested_path,
                set(),
            )
            if original_name not in allowed_tools:
                tool_message = agent._append_tool_error(
                    messages,
                    tool_call,
                    exposed_name,
                    "Dieser Pfad wurde vom OKF-Repository nicht für "
                    f"{original_name!r} angeboten: {requested_path!r}. "
                    "Verwende ausschließlich einen exakten Pfad und "
                    "das zugehörige `next_tool` aus `root_index` oder "
                    "`internal_links` eines erhaltenen "
                    "Tool-Ergebnisses.",
                )
                transient_rejections.append(
                    (assistant_message, tool_call, tool_message)
                )
                logger.info(
                    "knowledge_tool_call_undiscovered name=%s path=%s",
                    exposed_name,
                    requested_path,
                )
                continue

        if agent.logging_config.log_tool_calls:
            logger.info(
                "tool_call phase=%s name=%s arguments=%s",
                phase,
                exposed_name,
                json.dumps(arguments, ensure_ascii=False),
            )
        try:
            result = await session.call_tool(original_name, arguments)
        except Exception as exc:
            # MCP error strings can contain returned data; do not persist them
            # in the default local log.
            logger.error(
                "tool_call_failed phase=%s name=%s error_type=%s",
                phase,
                exposed_name,
                type(exc).__name__,
            )
            raise

        is_error = bool(getattr(result, "isError", False))
        if (
            not is_error
            and knowledge_state is not None
            and knowledge_call_key is not None
        ):
            knowledge_state.add_allowed_calls(_knowledge_allowed_calls(result))
            if original_name == "knowledge_read":
                document = _knowledge_document(result)
                knowledge_selection_token = knowledge_state.register_concept(document)
                if knowledge_selection_token is not None:
                    knowledge_document_path = str(document["path"])
                    logger.info(
                        "knowledge_concept_registered selection_token=%s path=%s",
                        knowledge_selection_token,
                        knowledge_document_path,
                    )
                    if (
                        max_concept_reads is not None
                        and len(knowledge_state.concepts) >= max_concept_reads
                        and not knowledge_selection_only_mode
                    ):
                        knowledge_selection_only_mode = True
                        knowledge_concept_limit_notice_pending = True
            knowledge_state.seen_calls.add(knowledge_call_key)
            knowledge_state.successful_followup_calls += 1

        raw_result_text = tool_result_text(result)
        model_result_text = raw_result_text
        compressed = False

        if knowledge_selection_token is not None:
            model_result_text = _knowledge_result_without_repository_ids(
                model_result_text
            )
        result_text = model_result_text

        should_compress = (
            bool(getattr(server_config, "compress_result", False))
            and len(result_text)
            >= int(getattr(server_config, "compress_min_chars", 12_000))
            and not is_error
        )

        if should_compress:
            current_turn_messages = copy.deepcopy(messages)
            try:
                result_text = await agent._compress_tool_result(
                    current_turn_messages=current_turn_messages,
                    tool_name=exposed_name,
                    arguments=arguments,
                    result_text=result_text,
                )
                compressed = True
            except Exception as exc:
                logger.error(
                    "tool_result_compression_failed phase=%s name=%s error_type=%s",
                    phase,
                    exposed_name,
                    type(exc).__name__,
                )
                result_text = model_result_text
        if (
            knowledge_selection_token is not None
            and knowledge_document_path is not None
        ):
            result_text = _knowledge_result_with_agent_selection(
                result_text,
                selection_token=knowledge_selection_token,
                path=knowledge_document_path,
            )
        logger.info(
            (
                "tool_result phase=%s name=%s is_error=%s "
                "raw_length=%d final_length=%d compressed=%s"
            ),
            phase,
            exposed_name,
            is_error,
            len(raw_result_text),
            len(result_text),
            compressed,
        )
        if agent.logging_config.log_tool_results:
            logger.info(
                "tool_result_content phase=%s name=%s content=%s",
                phase,
                exposed_name,
                result_text,
            )
        tool_message: dict[str, Any] = {
            "role": "tool",
            "content": result_text,
        }

        tool_call_id = tool_call.get("id")

        if tool_call_id:
            tool_message["tool_call_id"] = tool_call_id
        else:
            tool_message["tool_name"] = exposed_name

        messages.append(tool_message)
        agent._dump_context(messages, phase=phase)

    return calls, knowledge_concept_limit_notice_pending
