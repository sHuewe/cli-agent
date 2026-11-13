from __future__ import annotations

import copy
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from cli_agent.context.agent_knowledge import (
    MAX_KNOWLEDGE_SELECTION_RETRIES,
    MAX_PREMATURE_KNOWLEDGE_RETRIES,
    _KnowledgeRunState,
    _fallback_knowledge_selection,
    _validate_knowledge_selection,
)
from cli_agent.runtime.agent_tool_calls import process_tool_calls
from cli_agent.state.sub_runs import START_SUB_RUN, SUB_RUN_SOFT_TOOL_LIMIT, execute_sub_run
from cli_agent.state.agent_types import ToolRoute
from cli_agent.state.message_visibility import messages_for_model
from cli_agent.state.evidence_state import EvidenceError
from cli_agent.state.evidence_tools import PLAN_TOOL_NAME, ADD_EVIDENCE_TOOL_NAME, UPDATE_EVIDENCE_TOOL_NAME, REMOVE_EVIDENCE_TOOL_NAME, EvidenceInputError
from cli_agent.state.evidence_cleanup import maybe_cleanup_evidences, note_plan_update, note_tool_calls
from cli_agent.platform.artifact_publication import PUBLISH_TOOL_NAME, register_artifact
from cli_agent.models.model import log_model_call_diagnostics
from cli_agent.telemetry import span, set_attribute, traced_model_loop

logger = logging.getLogger("cli_agent.runtime.agent_loop")


def _assign_tool_call_ids(
    message: dict[str, Any],
    previous_messages: list[dict[str, Any]],
    *,
    reserved_ids: set[str] | None = None,
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
    used_ids.update(reserved_ids or ())
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


@traced_model_loop
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
    reserved_tool_call_ids: set[str] | None = None,
) -> str:
    state = run_state or ModelLoopRunState()
    calls = state.tool_calls
    empty_responses = 0
    premature_knowledge_responses = 0
    invalid_knowledge_responses = 0
    require_found_content_after_correction = False
    knowledge_selection_only_mode = False
    selection_only_tool_rejections = 0
    sub_finalization_rounds = 0

    def partial_sub_result(reason: str) -> str:
        evidence_state = getattr(agent, "_evidence_state", None)
        existing = set(getattr(evidence_state, "_sub_run_inherited_ids", set()))
        new_ids = [
            item for item in getattr(evidence_state, "evidences", {})
            if item not in existing
        ][:16]
        return json.dumps({
            "status": "partial",
            "summary": "The sub-run ended after reaching the soft tool-call budget.",
            "evidence_ids": new_ids,
            "confidence": "low",
            "uncertainties": [reason],
        }, ensure_ascii=False)

    transient_rejections: list[
        tuple[dict[str, Any], dict[str, Any], dict[str, Any]]
    ] = []

    while True:
        if phase == "main":
            await maybe_cleanup_evidences(agent)
        if phase == "sub" and calls >= SUB_RUN_SOFT_TOOL_LIMIT:
            if sub_finalization_rounds >= 3:
                return partial_sub_result(
                    "Finalization after repeated attempts at the soft tool budget."
                )
            sub_finalization_rounds += 1
            if sub_finalization_rounds == 1:
                messages.append({
                    "role": "user",
                    "content": (
                        "The sub-run soft budget has been reached. Finish the investigation; "
                        "do not start more data queries. Save already verified "
                        "significant results with agent__add_evidence if needed, "
                        "then immediately return the required JSON with "
                        "status=partial or completed and new evidence IDs. "
                        "Describe unfinished work in uncertainties."
                    ),
                })
        agent._dump_context(messages, phase=phase)
        # Model lifecycle telemetry is an SDK opt-in. Existing CLI/API event
        # consumers use a tool-only protocol and must remain unchanged.
        model_event = (
            getattr(agent, "event_callback", None)
            if getattr(agent, "_sdk_event_reporting", False)
            else None
        )
        model_call_id = f"model_{uuid.uuid4().hex}"
        if model_event is not None:
            await model_event("model_started", {"call_id": model_call_id, "phase": phase})
        model_status = "success"
        try:
            with span("llm.request", phase=phase,
                      model=getattr(agent.model_client, "model", "unknown")) as llm_span:
                message = await agent.model_client.chat(
                    messages=(
                        agent._evidence_state.project(messages)
                        if phase in {"main", "sub"} and getattr(agent, "_evidence_state", None) is not None
                        else messages_for_model(messages)
                    ),
                    tools=(
                        []
                        if phase == "knowledge" and knowledge_selection_only_mode
                        else (
                            [tool for tool in tools
                             if tool["function"]["name"] == ADD_EVIDENCE_TOOL_NAME]
                            if phase == "sub" and calls >= SUB_RUN_SOFT_TOOL_LIMIT
                            else tools
                        )
                    ),
                )
                usage = getattr(agent.model_client, "last_usage", None)
                for field in ("input_tokens", "output_tokens", "total_tokens"):
                    set_attribute(llm_span, "llm." + field,
                                  getattr(usage, field, None))
            log_model_call_diagnostics(agent.model_client, phase=phase)
            reserved_ids = set(reserved_tool_call_ids or ())
            if phase in {"main", "sub"}:
                reserved_ids.update(
                    getattr(getattr(agent, "_evidence_state", None), "tool_results", {})
                )
            _assign_tool_call_ids(message, messages, reserved_ids=reserved_ids)
            messages.append(message)
            if phase in {"main", "sub"} and getattr(agent, "_evidence_state", None) is not None:
                agent._evidence_state.pending_error = None
        except BaseException:
            model_status = "error"
            raise
        finally:
            if model_event is not None:
                await model_event("model_finished", {
                    "call_id": model_call_id, "phase": phase, "status": model_status,
                })
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
        if phase in {"main", "sub"}:
            note_tool_calls(agent, tool_calls)
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
                                    "The model returned a positive selection before the first "
                                    "concept read "
                                    "was completed."
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
                                    "You returned `found_content: true` before reading any concept, "
                                    "so no valid selection tokens exist. The root_index "
                                    "is for navigation only. If this request is applicable, "
                                    "call exactly one offered Knowledge tool now. Otherwise "
                                    "return `found_content: false` with "
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
                            "After an invalid selection with `found_content: true`, "
                            "a format-only correction must not switch to "
                            "`found_content: false`."
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
                                    "Repeatedly invalid final selection: "
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
                                "Correct only the final selection using concepts already read. "
                                "Do not call additional tools merely to fix formatting."
                            )
                        else:
                            correction_action = (
                                "No valid token exists yet. Use Knowledge tools before "
                                "returning another positive selection."
                            )
                        messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "Your final selection is invalid: "
                                    f"{selection_error}\n"
                                    "`found_content: true` requires "
                                    "`selected_okf_tokens` issued as "
                                    "`agent_selection.token` after successful "
                                    "`knowledge_read`. Do not use repository paths "
                                    "or `concept_id` values. Currently valid tokens: "
                                    + json.dumps(
                                        valid_tokens,
                                        ensure_ascii=False,
                                    )
                                    + ". "
                                    + correction_action
                                    + " Return only the required final JSON object."
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
                answer = "(The model did not produce an answer.)"
                agent._dump_context(messages, phase=phase)
                return answer

            empty_responses += 1
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "You produced neither a final answer nor a tool call. "
                        "Continue this phase with available tools or return "
                        "its required final text result. Do not claim success "
                        "without supporting tool results."
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
                        "The model repeatedly made "
                        "disallowed tool calls in selection-only mode."
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
                    "The Knowledge run is in selection-only mode. Select "
                    "one or more previously listed valid agent tokens; "
                    "do not call further tools.",
                )
                transient_rejections.append((message, rejected_call, tool_message))
            logger.info(
                "knowledge_tool_calls_rejected_selection_only count=%d",
                len(tool_calls),
            )
            continue

        if tool_calls:
            empty_responses = 0
        if phase == "sub":
            allowed_names = {ADD_EVIDENCE_TOOL_NAME}
            if (calls >= SUB_RUN_SOFT_TOOL_LIMIT
                    and any((call.get("function") or {}).get("name")
                            not in allowed_names for call in tool_calls)):
                return partial_sub_result(
                    "After the soft budget the model tried more "
                    "investigation tools instead of returning results."
                )
            if (calls < SUB_RUN_SOFT_TOOL_LIMIT
                    and calls + len(tool_calls) > SUB_RUN_SOFT_TOOL_LIMIT):
                # Never execute a batch that would overrun the soft budget;
                # keep already persisted evidences instead of hard-failing.
                return partial_sub_result(
                    "A tool batch would exceed the soft sub-run budget; "
                    "further work must be split off."
                )
        frontend_calls = []
        if phase == "main":
            frontend = getattr(agent, "_agui_frontend_tools", {})
            if frontend:
                from cli_agent.interfaces.ag_ui_frontend_tools import FrontendToolCall, FrontendToolHandoff
                for call in tool_calls:
                    function = call.get("function") or {}
                    if function.get("name") in frontend and function.get("name") not in {
                        PLAN_TOOL_NAME, ADD_EVIDENCE_TOOL_NAME,
                        UPDATE_EVIDENCE_TOOL_NAME, REMOVE_EVIDENCE_TOOL_NAME, PUBLISH_TOOL_NAME, START_SUB_RUN,
                    }:
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
             not in (set(getattr(agent, "_agui_frontend_tools", {}))
                     - {PLAN_TOOL_NAME, ADD_EVIDENCE_TOOL_NAME,
                        UPDATE_EVIDENCE_TOOL_NAME, REMOVE_EVIDENCE_TOOL_NAME, PUBLISH_TOOL_NAME, START_SUB_RUN})]
            if frontend_calls else tool_calls
        )
        evidence = getattr(agent, "_evidence_state", None) if phase in {"main", "sub"} else None
        evidence_calls = [
            call for call in backend_calls
            if (call.get("function") or {}).get("name") in {PLAN_TOOL_NAME, ADD_EVIDENCE_TOOL_NAME, UPDATE_EVIDENCE_TOOL_NAME, REMOVE_EVIDENCE_TOOL_NAME}
        ] if evidence is not None else []
        backend_calls = [call for call in backend_calls if call not in evidence_calls]
        sub_calls = [call for call in backend_calls if phase == "main" and
                     (call.get("function") or {}).get("name") == START_SUB_RUN]
        backend_calls = [call for call in backend_calls if call not in sub_calls]
        publish_calls = [
            call for call in backend_calls
            if (call.get("function") or {}).get("name") == PUBLISH_TOOL_NAME
        ] if phase == "main" else []
        backend_calls = [call for call in backend_calls if call not in publish_calls]
        # Reserve the full batch BEFORE executing any backend side effects.
        # Frontend calls are just as much part of max_tool_calls as MCP calls.
        if calls + len(frontend_calls) + len(backend_calls) + len(evidence_calls) + len(publish_calls) + len(sub_calls) > max_tool_calls:
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
            debug_args = (internal_call.get("function") or {}).get("arguments")
            debug_error = None
            previous_count = len(evidence.evidences)
            previous_compacted = len(evidence.compacted)
            try:
                raw_args = (internal_call.get("function") or {}).get("arguments") or {}
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                if tool_name == PLAN_TOOL_NAME:
                    before_plan = copy.deepcopy(evidence.plan)
                    plan_result = evidence.update_plan(args)
                    if phase == "main":
                        note_plan_update(agent, before_plan, evidence.plan)
                    result_text = json.dumps(plan_result, ensure_ascii=False)
                elif tool_name == ADD_EVIDENCE_TOOL_NAME:
                    evidence.add_evidence(args)
                elif tool_name == REMOVE_EVIDENCE_TOOL_NAME:
                    evidence.remove_evidence(args)
                else:
                    evidence.update_evidence(args)
                if tool_name != PLAN_TOOL_NAME:
                    result_text = (
                        "Evidence state successfully updated. "
                        f"Entries: {len(evidence.evidences)} "
                        f"(previously {previous_count}); "
                        f"hidden tool calls: {len(evidence.compacted)} "
                        f"(previously {previous_compacted}). "
                        "Do not save again unless a correction is needed."
                    )
                logger.info("evidence_state_updated entries=%d hidden=%d",
                            len(evidence.evidences), len(evidence.compacted))
            except (EvidenceError, EvidenceInputError, TypeError, ValueError, json.JSONDecodeError) as exc:
                status = "error"
                debug_error = f"{type(exc).__name__}: {exc}"
                if tool_name == PLAN_TOOL_NAME:
                    result_text = json.dumps({
                        "status": "error", "error": str(exc)[:1800],
                        "plan": evidence.compact_plan(evidence.plan),
                    }, ensure_ascii=False)
                else:
                    result_text = (
                        "Evidence update NOT applied: " + str(exc)[:1800]
                        + " Correct the specified arguments and retry."
                    )
                logger.warning("evidence_update_rejected type=%s", type(exc).__name__)
            finally:
                if evidence.debug_enabled:
                    evidence.record_debug_update(
                        call_id=event_id, arguments=debug_args,
                        status=status, error=debug_error,
                    )
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
        for sub_call in sub_calls:
            calls += 1
            state.tool_calls = calls
            event_id = sub_call["id"]
            callback = getattr(agent, "event_callback", None)
            if callback is not None:
                await callback("tool_started", {"call_id": event_id, "tool": START_SUB_RUN})
            status = "success"
            try:
                args = sub_call["function"].get("arguments") or {}
                if isinstance(args, str):
                    args = json.loads(args)
                result_text = json.dumps(await execute_sub_run(agent, args, coordinator_messages=messages), ensure_ascii=False)
            except (ValueError, RuntimeError, TypeError) as exc:
                status = "error"
                result_text = json.dumps({"status": "failed", "error": str(exc)[:1200]}, ensure_ascii=False)
            messages.append({"role": "tool", "tool_call_id": event_id, "content": result_text})
            if callback is not None:
                await callback("tool_finished", {"call_id": event_id, "tool": START_SUB_RUN, "status": status})
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
                result_text = "Download published: " + published.filename
            except (ValueError, RuntimeError, OSError) as exc:
                status = "error"
                result_text = "Could not publish download: " + str(exc)[:500]
            messages.append({"role": "tool", "tool_call_id": event_id, "content": result_text})
            if event_callback is not None:
                await event_callback("tool_finished", {
                    "call_id": event_id, "tool": PUBLISH_TOOL_NAME, "status": status,
                })
        if frontend_calls:
            from cli_agent.interfaces.ag_ui_frontend_tools import FrontendToolHandoff
            # All backend calls from the same model response have been handled
            # locally first. Frontend calls must never enter MCP routing.
            raise FrontendToolHandoff(frontend_calls, message)

        if knowledge_concept_limit_notice_pending and knowledge_state is not None:
            knowledge_selection_only_mode = True
            valid_tokens = {
                token: document["path"]
                for token, document in knowledge_state.concepts.items()
            }
            if getattr(knowledge_state, "chroma_budget_exhausted", False):
                notice = (
                    "The cumulative Chroma context budget has been reached. "
                    "Do not call more tools. Select only relevant, already "
                    "registered chunk tokens: "
                )
            else:
                notice = (
                    "The configured limit of "
                    f"{max_concept_reads} read concepts has been reached. "
                    "Do not call more tools. Select only materially useful "
                    "agent tokens: "
                )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        notice
                        + json.dumps(valid_tokens, ensure_ascii=False)
                        + ". Return only the required final JSON object."
                    ),
                }
            )
            logger.info(
                "knowledge_concept_limit_reached limit=%d",
                max_concept_reads,
            )
            agent._dump_context(messages, phase=phase)