from __future__ import annotations

import json
import logging
from typing import Any

from .agent_tool_calls import process_tool_calls

from .agent_knowledge import (
    MAX_KNOWLEDGE_SELECTION_RETRIES,
    MAX_PREMATURE_KNOWLEDGE_RETRIES,
    ToolRoute,
    _KnowledgeRunState,
    _fallback_knowledge_selection,
    _validate_knowledge_selection,
)

logger = logging.getLogger("cli_agent.agent_loop")


async def run_model_loop(
    agent,
    *,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    routes: dict[str, ToolRoute],
    enabled_server_names: set[str] | None,
    max_tool_calls: int,
    phase: str,
    knowledge_state: _KnowledgeRunState | None = None,
    max_concept_reads: int | None = None,
) -> str:
    calls = 0
    empty_responses = 0
    premature_knowledge_responses = 0
    invalid_knowledge_responses = 0
    require_found_content_after_correction = False
    knowledge_selection_only_mode = False
    selection_only_tool_rejections = 0
    transient_rejections: list[
        tuple[dict[str, Any], dict[str, Any], dict[str, Any]]
    ] = []

    while True:
        agent._dump_context(messages, phase=phase)
        try:
            message = await agent.model_client.chat(
                messages=messages,
                tools=(
                    []
                    if phase == "knowledge" and knowledge_selection_only_mode
                    else tools
                ),
            )
            messages.append(message)
        finally:
            for assistant_message, tool_call, tool_message in transient_rejections:
                agent._discard_rejected_tool_call(
                    messages,
                    assistant_message,
                    tool_call,
                    tool_message,
                )
            transient_rejections.clear()
        agent._dump_context(messages, phase=phase)
        if phase == "knowledge":
            agent._dump_value("knowledge_last_model_message.json", message)
        if agent.logging_config.log_model_messages:
            logger.info(
                "model_message phase=%s message=%s",
                phase,
                json.dumps(message, ensure_ascii=False),
            )

        tool_calls = message.get("tool_calls") or []
        if not tool_calls:
            answer = str(message.get("content") or "").strip()
            logger.info(
                "assistant_answer phase=%s length=%d",
                phase,
                len(answer),
            )
            if answer:
                empty_responses = 0
                if phase == "knowledge" and knowledge_state is not None:
                    selection, selection_error = _validate_knowledge_selection(
                        answer,
                        knowledge_state,
                    )
                    premature_positive = (
                        selection is not None
                        and selection.get("found_content") is True
                        and not knowledge_state.concepts
                    )
                    if premature_positive:
                        if (
                            premature_knowledge_responses
                            >= MAX_PREMATURE_KNOWLEDGE_RETRIES
                        ):
                            fallback = _fallback_knowledge_selection(
                                selection,
                                knowledge_state,
                                reason=(
                                    "Das Modell hat wiederholt vor dem ersten "
                                    "Concept-Read eine positive Auswahl "
                                    "ausgegeben."
                                ),
                            )
                            agent._dump_value(
                                "knowledge_selection_fallback.json",
                                fallback,
                            )
                            logger.warning("knowledge_premature_selection_fallback")
                            return json.dumps(fallback, ensure_ascii=False)

                        premature_knowledge_responses += 1
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Du hast `found_content: true` "
                                    "ausgegeben, obwohl noch kein Concept "
                                    "erfolgreich gelesen wurde und daher "
                                    "keine gültigen Auswahl-Tokens "
                                    "existieren. `root_index` ist nur eine "
                                    "Navigationshilfe. Wenn die Anfrage "
                                    "anwendbar ist, rufe jetzt genau ein "
                                    "angebotenes OKF-Tool auf. Andernfalls "
                                    "liefere `found_content: false` mit "
                                    "`reason_code: not_applicable`."
                                ),
                            }
                        )
                        logger.info(
                            "knowledge_premature_selection_retry count=%d",
                            premature_knowledge_responses,
                        )
                        continue

                    if (
                        selection_error is None
                        and require_found_content_after_correction
                        and selection is not None
                        and selection.get("found_content") is not True
                    ):
                        selection_error = (
                            "Nach einer ungültigen Auswahl mit "
                            "`found_content: true` darf die reine "
                            "Formatkorrektur nicht zu "
                            "`found_content: false` wechseln."
                        )
                    if selection_error is not None:
                        if (
                            invalid_knowledge_responses
                            >= MAX_KNOWLEDGE_SELECTION_RETRIES
                        ):
                            fallback = _fallback_knowledge_selection(
                                selection,
                                knowledge_state,
                                reason=(
                                    "Wiederholt ungültige finale Auswahl: "
                                    + selection_error
                                ),
                            )
                            agent._dump_value(
                                "knowledge_selection_fallback.json",
                                fallback,
                            )
                            logger.warning(
                                "knowledge_invalid_selection_fallback reason=%s",
                                selection_error,
                            )
                            return json.dumps(fallback, ensure_ascii=False)
                        invalid_knowledge_responses += 1
                        if (
                            selection is not None
                            and selection.get("found_content") is True
                        ):
                            require_found_content_after_correction = True
                        valid_tokens = {
                            token: document["path"]
                            for token, document in (knowledge_state.concepts.items())
                        }
                        if valid_tokens:
                            knowledge_selection_only_mode = True
                            correction_action = (
                                "Korrigiere ausschließlich die finale "
                                "Auswahl anhand der bereits gelesenen "
                                "Concepts. Rufe keine weiteren Tools nur "
                                "zur Korrektur des Ausgabeformats auf."
                            )
                        else:
                            correction_action = (
                                "Es existiert noch kein gültiger Token. "
                                "Nutze jetzt die OKF-Tools, bevor du erneut "
                                "eine positive Auswahl ausgibst."
                            )
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Deine finale Auswahl ist ungültig: "
                                    f"{selection_error}\n"
                                    "`found_content: true` ist nur mit "
                                    "`selected_okf_tokens` zulässig, die "
                                    "der Agent nach einem erfolgreichen "
                                    "`knowledge_read` unter "
                                    "`agent_selection.token` vergeben hat. "
                                    "Verwende weder Repository-Pfade noch "
                                    "`concept_id`-Werte. Aktuell gültige "
                                    "Tokens: "
                                    + json.dumps(
                                        valid_tokens,
                                        ensure_ascii=False,
                                    )
                                    + ". "
                                    + correction_action
                                    + " Liefere danach erneut "
                                    "ausschließlich das verlangte "
                                    "JSON-Objekt."
                                ),
                            }
                        )
                        logger.info(
                            "knowledge_invalid_selection_retry reason=%s",
                            selection_error,
                        )
                        continue
                agent._dump_context(messages, phase=phase)
                return answer

            if empty_responses >= 1:
                if phase == "knowledge" and knowledge_state is not None:
                    fallback = _fallback_knowledge_selection(
                        None,
                        knowledge_state,
                        reason=(
                            "Das Modell hat wiederholt weder eine finale "
                            "Antwort noch einen Tool-Aufruf erzeugt."
                        ),
                    )
                    agent._dump_value(
                        "knowledge_selection_fallback.json",
                        fallback,
                    )
                    logger.warning("knowledge_empty_response_fallback")
                    return json.dumps(fallback, ensure_ascii=False)
                answer = "(Das Modell hat keine Antwort erzeugt.)"
                agent._dump_context(messages, phase=phase)
                return answer

            empty_responses += 1
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Du hast weder eine finale Antwort noch einen "
                        "Tool-Aufruf erzeugt. Setze diese Phase jetzt mit "
                        "den verfügbaren Tools fort oder liefere das für "
                        "diese Phase verlangte textliche Endergebnis. "
                        "Behaupte keinen Erfolg ohne passende "
                        "Tool-Ergebnisse."
                    ),
                }
            )
            logger.info(
                "empty_model_response_retry phase=%s",
                phase,
            )
            continue

        if phase == "knowledge" and knowledge_selection_only_mode:
            if selection_only_tool_rejections >= 1:
                if knowledge_state is None:
                    raise RuntimeError("Knowledge-Auswahlmodus ohne Laufzustand.")
                fallback = _fallback_knowledge_selection(
                    None,
                    knowledge_state,
                    reason=(
                        "Das Modell hat im reinen Auswahlmodus wiederholt "
                        "unzulässige Tool-Aufrufe erzeugt."
                    ),
                )
                agent._dump_value(
                    "knowledge_selection_fallback.json",
                    fallback,
                )
                logger.warning("knowledge_selection_only_tool_fallback")
                return json.dumps(fallback, ensure_ascii=False)
            selection_only_tool_rejections += 1
            for rejected_call in tool_calls:
                function = rejected_call.get("function", {})
                exposed_name = function.get("name")
                tool_message = agent._append_tool_error(
                    messages,
                    rejected_call,
                    exposed_name,
                    "Der Knowledge-Lauf befindet sich im reinen "
                    "Auswahlmodus. Verwende jetzt ausschließlich "
                    "einen oder mehrere der bereits genannten gültigen "
                    "Agent-Tokens und rufe keine weiteren Tools auf.",
                )
                transient_rejections.append((message, rejected_call, tool_message))
            logger.info(
                "knowledge_tool_calls_rejected_selection_only count=%d",
                len(tool_calls),
            )
            continue

        if tool_calls:
            empty_responses = 0
        calls, knowledge_concept_limit_notice_pending = await process_tool_calls(
            agent,
            assistant_message=message,
            messages=messages,
            tool_calls=tool_calls,
            routes=routes,
            enabled_server_names=enabled_server_names,
            max_tool_calls=max_tool_calls,
            phase=phase,
            knowledge_state=knowledge_state,
            max_concept_reads=max_concept_reads,
            calls=calls,
            knowledge_selection_only_mode=knowledge_selection_only_mode,
            transient_rejections=transient_rejections,
        )

        if knowledge_concept_limit_notice_pending and knowledge_state is not None:
            knowledge_selection_only_mode = True
            valid_tokens = {
                token: document["path"]
                for token, document in knowledge_state.concepts.items()
            }
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "Das konfigurierte Limit von "
                        f"{max_concept_reads} gelesenen Concepts ist "
                        "erreicht. Rufe keine weiteren Tools auf. Wähle "
                        "jetzt nur die materiell hilfreichen Concepts "
                        "aus diesen Agent-Tokens aus: "
                        + json.dumps(valid_tokens, ensure_ascii=False)
                        + ". Liefere ausschließlich das verlangte finale "
                        "JSON-Objekt."
                    ),
                }
            )
            logger.info(
                "knowledge_concept_limit_reached limit=%d",
                max_concept_reads,
            )
            agent._dump_context(messages, phase=phase)