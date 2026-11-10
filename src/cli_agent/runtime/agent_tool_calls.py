from __future__ import annotations

import copy
import json
import logging
import uuid
from typing import Any

from cli_agent.context.agent_knowledge import (
    _register_chroma_results,
    KnowledgeCallKey,
    _KnowledgeRunState,
    _knowledge_allowed_calls,
    _knowledge_call_key,
    _knowledge_document,
    _knowledge_result_with_agent_selection,
    _knowledge_result_without_repository_ids,
    _normalize_knowledge_path,
    tool_result_text,
)
from cli_agent.runtime.knowledge_trace import trace_observe
from cli_agent.state.agent_types import ToolRoute
from cli_agent.state.evidence_tools import annotate_source_result
from cli_agent.security.mcp_limits import (
    MCP_TOOL_CALL_TIMEOUT_SECONDS,
    await_mcp_operation,
    enforce_mcp_tool_result_limit,
)
from cli_agent.security.mcp_schema_guard import validate_mcp_tool_arguments
from cli_agent.security.mcp_result_artifacts import ArtifactError, ONLY_METADATA_TO_LLM_KEY
from cli_agent.platform.os_operations import WorkspaceError
from cli_agent.state.message_visibility import (
    CLI_AGENT_MESSAGE_TO_USER_META_KEY,
    MCP_USER_MESSAGE_ROLE,
    MCP_USER_MESSAGE_TYPE,
)

logger = logging.getLogger("cli_agent.runtime.agent_tool_calls")


def _message_to_user(result: Any) -> str | None:
    meta = getattr(result, "meta", None)
    if not isinstance(meta, dict):
        return None
    extension = meta.get(CLI_AGENT_MESSAGE_TO_USER_META_KEY)
    if extension is None:
        return None
    if not isinstance(extension, dict):
        logger.warning("mcp_message_to_user_invalid reason=not_object")
        return None
    text = extension.get("text")
    if not isinstance(text, str) or not text.strip():
        logger.warning("mcp_message_to_user_invalid reason=missing_text")
        return None
    return enforce_mcp_tool_result_limit(text.strip())


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
                "The Knowledge run permits exactly one "
                "Knowledge tool call per model response. First evaluate the result "
                "of the first call, then determine whether "
                "another call is necessary.",
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
                    "The tool call has no valid JSON arguments.",
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
                    "Tool arguments must be a JSON object.",
                )
                transient_rejections.append(
                    (assistant_message, tool_call, tool_message)
                )
                continue

        if (
            phase != "knowledge"
            and isinstance(exposed_name, str)
            and hasattr(agent, "_tool_is_enabled")
            and not agent._tool_is_enabled(exposed_name)
        ):
            tool_message = agent._append_tool_error(
                messages,
                tool_call,
                exposed_name,
                f"MCP tool {exposed_name!r} is disabled for this session. "
                "Do not call it again. Use only an "
                "enabled tool from the current tool list.",
            )
            transient_rejections.append((assistant_message, tool_call, tool_message))
            continue

        route = routes.get(exposed_name)
        if route is None:
            tool_message = agent._append_tool_error(
                messages,
                tool_call,
                exposed_name,
                f"MCP tool {exposed_name!r} does not exist or is "
                "currently unavailable. Use only a "
                "tool from the current tool list.",
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
                f"MCP tool {exposed_name!r} is unavailable because "
                f"MCP server {server_config.name!r} is disabled. "
                "Do not call it again. Use only an "
                "tool from the current tool list.",
            )
            transient_rejections.append((assistant_message, tool_call, tool_message))
            continue

        input_schema = agent._tool_input_schema(exposed_name, phase=phase)
        if input_schema is None:
            logger.error(
                "tool_call_schema_missing phase=%s name=%s",
                phase,
                exposed_name,
            )
            raise RuntimeError(
                f"No registered input schema is available for MCP tool {exposed_name!r}."
            )
        validation_error = validate_mcp_tool_arguments(
            tool_name=str(exposed_name),
            schema=input_schema,
            arguments=arguments,
        )
        if validation_error is not None:
            tool_message = agent._append_tool_error(
                messages,
                tool_call,
                exposed_name,
                "Tool arguments do not match the input schema provided "
                f"by the MCP server: {validation_error}",
            )
            transient_rejections.append((assistant_message, tool_call, tool_message))
            logger.info(
                "tool_call_schema_rejected phase=%s name=%s",
                phase,
                exposed_name,
            )
            continue

        if agent._requires_approval(
            server_config,
            original_name,
            exposed_name=exposed_name,
        ):
            event_id = str(tool_call.get("id") or uuid.uuid4().hex)
            approval_context = getattr(agent, "_agui_approval_call_id", None)
            agent._agui_approval_call_id = event_id
            try:
                approved = await agent._approve_tool_call(
                    exposed_name,
                    arguments,
                )
            finally:
                agent._agui_approval_call_id = approval_context
            if not approved:
                # An AG-UI interrupt advertised this model-provided call ID
                # before the decision. Even when denied, its tool lifecycle
                # must have a correlated outcome in the resumed run.
                if (getattr(agent, "_agui_frontend_tools", None) is not None
                        or getattr(agent, "_sdk_event_reporting", False)):
                    callback = getattr(agent, "event_callback", None)
                    if callback is not None:
                        if getattr(agent, "_sdk_event_reporting", False):
                            await callback("tool_started", {
                                "call_id": event_id, "tool": exposed_name,
                            })
                        await callback("tool_finished", {
                            "call_id": event_id, "tool": exposed_name,
                            "status": "denied",
                        })
                tool_message = agent._append_tool_error(
                    messages,
                    tool_call,
                    exposed_name,
                    "This mutating tool call was not authorized by an explicit "
                    "user approval. Do not retry it unless the user explicitly "
                    "approves the action.",
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
                    "This identical OKF call has already completed successfully. "
                    "Use its existing result rather than calling it again.",
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
            if original_name != "knowledge_search" and original_name not in allowed_tools:
                tool_message = agent._append_tool_error(
                    messages,
                    tool_call,
                    exposed_name,
                    "The OKF repository did not offer this path for "
                    f"{original_name!r}: {requested_path!r}. "
                    "Use only exact paths and corresponding `next_tool` entries "
                    "from `root_index` or the `internal_links` of a received "
                    "tool result.",
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

        event_callback = getattr(agent, "event_callback", None)
        event_id = str(tool_call.get("id") or uuid.uuid4().hex)
        if event_callback is not None:
            await event_callback(
                "tool_started",
                {"call_id": event_id, "tool": exposed_name},
            )
        if agent.logging_config.log_tool_calls:
            logger.info(
                "tool_call phase=%s name=%s arguments=%s",
                phase,
                exposed_name,
                json.dumps(arguments, ensure_ascii=False),
            )
        try:
            result = await await_mcp_operation(
                session.call_tool(original_name, arguments),
                timeout_seconds=MCP_TOOL_CALL_TIMEOUT_SECONDS,
                operation=f"MCP-Tool {exposed_name!r}",
            )
        except BaseException as exc:
            # Always balance TOOL_CALL_START, including timeout and cancellation.
            if event_callback is not None:
                await event_callback(
                    "tool_finished",
                    {"call_id": event_id, "tool": exposed_name, "status": "error"},
                )
            if isinstance(exc, Exception):
                logger.error(
                    "tool_call_failed phase=%s name=%s error_type=%s",
                    phase, exposed_name, type(exc).__name__,
                )
            raise

        if event_callback is not None:
            finished_payload = {
                "call_id": event_id, "tool": exposed_name,
                "status": "error" if getattr(result, "isError", False) else "success",
            }
            if (
                phase == "knowledge"
                and exposed_name == "vector__knowledge_search"
                and getattr(getattr(agent, "_vector_config", None), "mode", None) == "chroma"
                and not getattr(result, "isError", False)
            ):
                from cli_agent.context.chroma_knowledge import chroma_search_event_summary
                raw_search = getattr(result, "structuredContent", None)
                if isinstance(raw_search, dict):
                    finished_payload["search_summary"] = chroma_search_event_summary(
                        raw_search,
                        collection=agent._vector_config.collection,
                        initial=False,
                    )
            await event_callback("tool_finished", finished_payload)
        user_message = _message_to_user(result)
        is_error = bool(getattr(result, "isError", False))
        if (
            not is_error
            and knowledge_state is not None
            and knowledge_call_key is not None
        ):
            knowledge_state.add_allowed_calls(_knowledge_allowed_calls(result))
            if (
                original_name == "knowledge_search"
                and getattr(getattr(agent, "_vector_config", None), "mode", None) == "chroma"
            ):
                raw_chroma = getattr(result, "structuredContent", None)
                if not isinstance(raw_chroma, dict):
                    raise RuntimeError("Chroma-Suche lieferte kein strukturiertes Ergebnis.")
                # The host, not the LLM, assigns tokens to newly discovered
                # chunks. Return the tokens together with the original text.
                result.structuredContent = _register_chroma_results(
                    knowledge_state, raw_chroma,
                )
                trace_observe(
                    agent, "search", backend="chroma", stage="followup",
                    result=result.structuredContent,
                )
                if (
                    knowledge_state.chroma_budget_exhausted
                    or (
                        max_concept_reads is not None
                        and len(knowledge_state.concepts) >= max_concept_reads
                    )
                ):
                    # Stop further retrieval once either cumulative budget is
                    # reached. The next LLM turn can select known tokens only.
                    knowledge_selection_only_mode = True
                    knowledge_concept_limit_notice_pending = True
            if (
                original_name == "knowledge_search"
                and getattr(getattr(agent, "_vector_config", None), "mode", None) != "chroma"
            ):
                raw_vector = getattr(result, "structuredContent", None)
                if isinstance(raw_vector, dict):
                    trace_observe(
                        agent, "search", backend="okf", stage="followup",
                        result=raw_vector,
                    )
            if original_name == "knowledge_read":
                document = _knowledge_document(result)
                trace_observe(agent, "read", document=document)
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
        artifactized = False
        store = getattr(agent, "_result_artifacts", None)
        meta = getattr(result, "meta", None)
        artifact_requested = (
            store is not None
            and getattr(agent, "workspace_access", "none") in {"read", "write"}
            and isinstance(meta, dict)
            and meta.get(ONLY_METADATA_TO_LLM_KEY) is True
        )
        # Metadata-only results are stored with their own bounded artifact
        # budget. The lower generic MCP context limit still applies whenever
        # an unabridged result could reach the model.
        if not artifact_requested:
            enforce_mcp_tool_result_limit(raw_result_text)
        if artifact_requested:
            try:
                metadata_result = store.metadata_result(result, raw_result_text)
                if metadata_result is not None:
                    model_result_text = metadata_result
                    artifactized = True
                else:
                    # Fail safe if a future artifact store declines an opt-in:
                    # its unabridged payload must still satisfy the normal cap.
                    enforce_mcp_tool_result_limit(raw_result_text)
            except (ArtifactError, WorkspaceError, OSError) as exc:
                logger.warning("mcp_result_artifact_failed name=%s error_type=%s", exposed_name, type(exc).__name__)
                # Never silently send the large original payload when the MCP
                # server explicitly requested metadata-only delivery.
                model_result_text = json.dumps(
                    {
                        "error": "mcp_result_artifact_failed",
                        "reason": str(exc) if isinstance(exc, ArtifactError) else
                        "MCP-Daten konnten nicht sicher im Workspace gespeichert werden.",
                    },
                    ensure_ascii=False,
                )
                artifactized = True
        compressed = False

        if knowledge_selection_token is not None:
            model_result_text = _knowledge_result_without_repository_ids(
                model_result_text
            )
        result_text = model_result_text

        should_compress = (
            not artifactized
            and bool(getattr(server_config, "compress_result", False))
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
        evidence = getattr(agent, "_evidence_state", None) if phase in {"main", "sub"} else None
        if evidence is not None and not is_error:
            metadata = None
            if artifactized:
                try:
                    metadata = json.loads(model_result_text)
                except (TypeError, ValueError):
                    metadata = None
            source_ref = evidence.register_result(
                tool_call_id, tool_name=str(exposed_name), artifact_metadata=metadata,
            )
            if source_ref is not None:
                tool_message["content"] = annotate_source_result(
                    tool_message["content"], source_ref=source_ref,
                    tool_name=str(exposed_name),
                )
        if user_message is not None:
            notice = {
                "role": MCP_USER_MESSAGE_ROLE,
                "type": MCP_USER_MESSAGE_TYPE,
                "content": user_message,
                "source": str(exposed_name or original_name),
            }
            messages.append(notice)
            callback = getattr(agent, "user_message_callback", None)
            if callback is not None:
                try:
                    await callback(user_message, notice["source"])
                except Exception as exc:
                    logger.error(
                        "mcp_message_to_user_delivery_failed "
                        "source=%s error_type=%s",
                        notice["source"],
                        type(exc).__name__,
                    )
        agent._dump_context(messages, phase=phase)

    return calls, knowledge_concept_limit_notice_pending
