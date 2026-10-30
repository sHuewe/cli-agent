from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from .agent_knowledge import (
    MAX_KNOWLEDGE_SELECTION_RETRIES,
    MAX_PREMATURE_KNOWLEDGE_RETRIES,
    _KnowledgeRunState,
    _fallback_knowledge_selection,
    _validate_knowledge_selection,
)
from .agent_tool_calls import process_tool_calls
from .agent_types import ToolRoute
from .message_visibility import messages_for_model
from .evidence_state import EVIDENCE_TOOL_NAME, EvidenceError
from .evidence_tools import PLAN_TOOL_NAME, ADD_EVIDENCE_TOOL_NAME, UPDATE_EVIDENCE_TOOL_NAME, EvidenceInputError
from .artifact_publication import PUBLISH_TOOL_NAME, register_artifact
from .model import log_model_call_diagnostics

logger = logging.getLogger("cli_agent.agent_loop")


def _assign_tool_call_ids(
    message: dict[str, Any],
    previous_messages: list[dict[str, Any]],
) -> None:
    """Give id-less provider calls stable per-run IDs before execution.

    Source IDs must identify a single assistant call, even when a provider
    omits IDs or accidentally reuses one from a previous response.
    """
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return
    used_ids = {
        call["id"]
        for prior in previous_messages
        if prior.get("role") == "assistant"
        for call in prior.get("tool_calls", [])
        if isinstance(call, dict)
        and isinstance(call.get("id"), str)
        and call["id"]
    }
    used_ids.update(
        prior["tool_call_id"]
        for prior in previous_messages
        if prior.get("role") == "tool"
        and isinstance(prior.get("tool_call_id"), str)
    )
    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id.strip() or call_id in used_ids:
            call_id = f"call_{uuid.uuid4().hex}"
            while call_id in used_ids:
                call_id = f"call_{uuid.uuid4().hex}"
            call["id"] = call_id
        used_ids.add(call_id)
        # The internal model history uses the OpenAI function-call envelope,
        # even if the provider's native format omits the type.
        call.setdefault("type", "function")


@dataclass
class ModelLoopRunState:
    tool_calls: int = 0


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
    run_state: ModelLoopRunState | None = None,
) -> str:
    state = run_state or ModelLoopRunState()
    calls = state.tool_calls
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
                messages=(
                    agent._evidence_state.project(messages)
                    if phase == "main" and getattr(agent, "_evidence_state", None) is not None
                    else messages_for_model(messages)
                ),
                tools=(
                    []
                    if phase == "knowledge" and knowledge_selection_only_mode
                    else tools
                ),
            )
            log_model_call_diagnostics(agent.model_client, phase=phase)
            _assign_tool_call_ids(message, messages)
            messages.append(message)
            if phase == "main" and getattr(agent, "_evidence_state", None) is not None:
                agent._evidence_state.pending_error = None
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
                                    "angebotenes Knowledge-Tool auf. Andernfalls "
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
                                "Nutze jetzt die Knowledge-Tools, bevor du erneut "
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
        frontend_calls = []
        if phase == "main":
            frontend = getattr(agent, "_agui_frontend_tools", {})
            if frontend:
                from .ag_ui_frontend_tools import FrontendToolCall, FrontendToolHandoff
                import uuid
                for call in tool_calls:
                    function = call.get("function") or {}
                    if function.get("name") in frontend:
                        arguments = function.get("arguments") or {}
                        arguments = (arguments if isinstance(arguments, str)
                                     else json.dumps(arguments, ensure_ascii=False))
                        frontend_calls.append(
                            FrontendToolCall(
                                id=str(call.get("id") or uuid.uuid4().hex),
                                name=function["name"],
                                arguments=arguments,
                            )
                        )
        backend_calls = (
            [call for call in tool_calls
             if call.get("function", {}).get("name")
             not in getattr(agent, "_agui_frontend_tools", {})]
            if frontend_calls else tool_calls
        )
        evidence = getattr(agent, "_evidence_state", None) if phase == "main" else None
        evidence_calls = [
            call for call in backend_calls
            if (call.get("function") or {}).get("name") in {PLAN_TOOL_NAME, ADD_EVIDENCE_TOOL_NAME, UPDATE_EVIDENCE_TOOL_NAME, EVIDENCE_TOOL_NAME}
        ] if evidence is not None else []
        backend_calls = [call for call in backend_calls if call not in evidence_calls]
        publish_calls = [
            call for call in backend_calls
            if (call.get("function") or {}).get("name") == PUBLISH_TOOL_NAME
        ] if phase == "main" else []
        backend_calls = [call for call in backend_calls if call not in publish_calls]
        # Reserve the full batch BEFORE executing any backend side effects.
        # Frontend calls are just as much part of max_tool_calls as MCP calls.
        if calls + len(frontend_calls) + len(backend_calls) + len(evidence_calls) + len(publish_calls) > max_tool_calls:
            raise RuntimeError(
                f"Abbruch in Phase {phase!r} nach {max_tool_calls} Tool-Aufrufen."
            )
        calls += len(frontend_calls)
        state.tool_calls = calls
        calls, knowledge_concept_limit_notice_pending = await process_tool_calls(
            agent,
            assistant_message=message,
            messages=messages,
            tool_calls=backend_calls,
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
        state.tool_calls = calls
        for internal_call in evidence_calls:
            calls += 1
            state.tool_calls = calls
            event_callback = getattr(agent, "event_callback", None)
            event_id = internal_call["id"]
            tool_name = internal_call["function"]["name"]
            if event_callback is not None:
                await event_callback(
                    "tool_started",
                    {"call_id": event_id, "tool": tool_name},
                )
            status = "success"
            previous_count = len(evidence.evidences)
            previous_compacted = len(evidence.compacted)
            try:
                raw_args = (internal_call.get("function") or {}).get("arguments") or {}
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                if tool_name == EVIDENCE_TOOL_NAME:
                    evidence.update(args, call_id=event_id)
                elif tool_name == PLAN_TOOL_NAME:
                    evidence.update_plan(args)
                elif tool_name == ADD_EVIDENCE_TOOL_NAME:
                    evidence.add_evidence(args)
                else:
                    evidence.update_evidence(args)
                result_text = (
                    "Evidence-State erfolgreich aktualisiert. "
                    f"Einträge: {len(evidence.evidences)} "
                    f"(vorher {previous_count}); "
                    f"ausgeblendete Toolaufrufe: {len(evidence.compacted)} "
                    f"(vorher {previous_compacted}). "
                    "Nicht erneut speichern, sofern keine Korrektur nötig ist."
                )
                logger.info("evidence_state_updated entries=%d hidden=%d",
                            len(evidence.evidences), len(evidence.compacted))
            except (EvidenceError, EvidenceInputError, TypeError, ValueError, json.JSONDecodeError) as exc:
                status = "error"
                result_text = (
                    "Evidence-Update NICHT übernommen: " + str(exc)[:1800]
                    + " Korrigiere die konkret genannten Argumente und versuche es erneut."
                )
                logger.warning("evidence_update_rejected type=%s", type(exc).__name__)
            finally:
                # Keep a protocol-complete, short confirmation in the model
                # history, but never repeat the potentially huge evidence
                # payload in the assistant tool call's arguments.
                internal_call["function"]["arguments"] = {}
                messages.append({
                    "role": "tool",
                    "tool_call_id": event_id,
                    "content": result_text,
                })
                if event_callback is not None:
                    await event_callback(
                        "tool_finished",
                        {"call_id": event_id, "tool": tool_name,
                         "status": status},
                    )
        for publish_call in publish_calls:
            calls += 1
            state.tool_calls = calls
            event_id = publish_call["id"]
            event_callback = getattr(agent, "event_callback", None)
            if event_callback is not None:
                await event_callback("tool_started", {"call_id": event_id, "tool": PUBLISH_TOOL_NAME})
            status = "success"
            try:
                raw_args = (publish_call.get("function") or {}).get("arguments") or {}
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                if not isinstance(args, dict) or set(args) != {"path", "filename"}:
                    raise ValueError("path und filename sind erforderlich.")
                artifact_id, published = register_artifact(
                    agent._result_artifacts, args["path"], args["filename"],
                )
                # The API session owns the public ID mapping, not the LLM.
                if event_callback is not None:
                    await event_callback("artifact_published", {
                        "call_id": event_id, "id": artifact_id, "path": published.path,
                        "filename": published.filename, "mime_type": published.mime_type,
                    })
                result_text = "Download bereitgestellt: " + published.filename
            except (ValueError, RuntimeError, OSError) as exc:
                status = "error"
                result_text = "Download konnte nicht veröffentlicht werden: " + str(exc)[:500]
            messages.append({"role": "tool", "tool_call_id": event_id, "content": result_text})
            if event_callback is not None:
                await event_callback("tool_finished", {
                    "call_id": event_id, "tool": PUBLISH_TOOL_NAME, "status": status,
                })
        if frontend_calls:
            from .ag_ui_frontend_tools import FrontendToolHandoff
            # All backend calls from the same model response have been handled
            # locally first. Frontend calls must never enter MCP routing.
            raise FrontendToolHandoff(frontend_calls, message)

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