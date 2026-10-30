"""Optional, bounded delegated model runs within the owning agent session."""
from __future__ import annotations

import copy
import json
import uuid
from typing import Any

from .evidence_state import EvidenceState
from .evidence_tools import ADD_EVIDENCE_TOOL_NAME, UPDATE_EVIDENCE_TOOL_NAME

START_SUB_RUN = "agent__start_sub_run"
MAX_SUB_RUN_TOOLS = 30
MAX_SUB_RUNS_PER_TASK = 4
SUB_RUN_TOOL = {
    "type": "function",
    "function": {
        "name": START_SUB_RUN,
        "description": (
            "Delegate a substantial, clearly scoped investigation to a separate "
            "multi-step LLM context. Consider using this EARLY when even one "
            "research, search or analysis task will require many tool calls, "
            "multiple steps or extensive intermediate results; it need not be "
            "independent of every other task. The coordinator keeps oversight "
            "and final synthesis while the sub-run returns a compact result "
            "with verified evidence IDs. Prefer delegation when isolating work "
            "would reduce coordinator context. Do not delegate trivial queries, "
            "calculations, plan updates or short summaries. Sub-runs reuse "
            "session permissions and artifacts but incur additional model "
            "calls, tokens and latency."
        ),
        "parameters": {
            "type": "object", "additionalProperties": False,
            "required": ["task"],
            "properties": {
                "task": {"type": "string", "minLength": 20, "maxLength": 2500},
                "plan_step_id": {"type": "string", "maxLength": 80},
                "evidence_ids": {"type": "array", "maxItems": 16,
                                 "items": {"type": "string", "maxLength": 80}},
            },
        },
    },
}
SUB_RUN_RULES = """
You are a delegated step agent, not the coordinator. Solve only the assigned task.
Do not create or modify the coordinator plan or delegate further sub-runs.
Use tools only when necessary. Evidence tools are optional: record only meaningful
findings with verified source references. Do not invent evidence IDs or sources.
Return ONLY a JSON object with exactly: status (completed, partial, blocked, failed),
summary (string), evidence_ids (array of IDs you created), confidence (high, medium,
low), uncertainties (array of strings). Do not claim completion without evidence.
"""


def validate_sub_result(answer: str, available: set[str]) -> dict[str, Any]:
    from .agent_conversation import _normalize_json_response
    normalized, error = _normalize_json_response(answer)
    if error is not None:
        raise ValueError("Sub-Run lieferte kein gültiges JSON: " + error)
    result = json.loads(normalized)
    if not isinstance(result, dict) or set(result) != {
        "status", "summary", "evidence_ids", "confidence", "uncertainties"
    }:
        raise ValueError("Sub-Run JSON hat nicht die geforderte Struktur.")
    if result["status"] not in {"completed", "partial", "blocked", "failed"}:
        raise ValueError("Ungültiger Sub-Run-Status.")
    if result["confidence"] not in {"high", "medium", "low"}:
        raise ValueError("Ungültige Sub-Run-Konfidenz.")
    if not isinstance(result["summary"], str) or len(result["summary"]) > 4000:
        raise ValueError("Ungültige Sub-Run-Zusammenfassung.")
    ids = result["evidence_ids"]
    if (not isinstance(ids, list) or len(ids) > 16 or len(set(ids)) != len(ids)
            or any(not isinstance(i, str) or i not in available for i in ids)):
        raise ValueError("Sub-Run referenziert unbekannte Evidences.")
    if (not isinstance(result["uncertainties"], list)
            or len(result["uncertainties"]) > 16
            or any(not isinstance(i, str) or len(i) > 500 for i in result["uncertainties"])):
        raise ValueError("Ungültige Unsicherheiten.")
    return result


async def execute_sub_run(
    agent: Any, args: dict[str, Any], *,
    coordinator_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not isinstance(args, dict) or set(args) - {"task", "plan_step_id", "evidence_ids"}:
        raise ValueError("Ungültige Sub-Run-Argumente.")
    task = args.get("task")
    if not isinstance(task, str) or not 20 <= len(task) <= 2500:
        raise ValueError("Sub-Run benötigt einen konkreten Teilauftrag.")
    if len(agent._sub_runs) >= MAX_SUB_RUNS_PER_TASK:
        raise ValueError("Sub-Run-Limit dieser Aufgabe erreicht.")
    parent = agent._evidence_state
    step = args.get("plan_step_id")
    if step is not None and (parent.plan is None or step not in {
        item["id"] for item in parent.plan["steps"]
    }):
        raise ValueError("Unbekannter Planschritt.")
    ids = args.get("evidence_ids", [])
    if (not isinstance(ids, list) or len(ids) > 16
            or any(not isinstance(i, str) or i not in parent.evidences for i in ids)):
        raise ValueError("Unbekannte Evidence-ID.")
    run_id = "sub-" + uuid.uuid4().hex[:12]
    local = EvidenceState()
    local.tool_results = copy.deepcopy(parent.tool_results)
    local._next_source_number = parent._next_source_number
    local.evidences = {i: copy.deepcopy(parent.evidences[i]) for i in ids}
    # A sub-run may read selected evidence, but cannot edit it.
    inherited = set(local.evidences)
    local.refresh_model_tools()
    tools = [
        tool for tool in agent._model_tools()
        if tool["function"]["name"] not in {
            START_SUB_RUN, "agent__update_plan", "agent__publish_artifact"
        }
    ]
    tools = [
        tool for tool in tools if tool["function"]["name"] not in {
            ADD_EVIDENCE_TOOL_NAME, UPDATE_EVIDENCE_TOOL_NAME
        }
    ] + [tool for tool in local.tool_definitions() if tool["function"]["name"] in {
        ADD_EVIDENCE_TOOL_NAME, UPDATE_EVIDENCE_TOOL_NAME
    }]
    # Build a distinct prompt from the delegated tool set. Preserve the same
    # trust boundary as the coordinator: administrator-approved MCP instructions
    # belong in the system prompt; server-provided untrusted instructions are
    # supplied only as explicitly labelled reference data.
    from .agent import (
        BASE_SYSTEM_PROMPT, MCP_TOOL_SYSTEM_RULE, WORKSPACE_TOOL_SYSTEM_RULE,
        MCP_INSTRUCTION_SYSTEM_RULE, REFERENCE_CONTEXT_SYSTEM_RULE,
    )
    names = [tool["function"]["name"] for tool in tools]
    active_instructions = [
        (name, instructions)
        for name, instructions in agent._server_instructions.items()
        if name in agent._active_servers
    ]
    reference_context = agent._build_reference_context_message(knowledge=None)
    parts = [BASE_SYSTEM_PROMPT]
    if names:
        parts.append(MCP_TOOL_SYSTEM_RULE)
        if agent._main_tools_need_workspace_context(names):
            parts.append(WORKSPACE_TOOL_SYSTEM_RULE)
        parts.append("Available tools (only these may be called):\\n- " + "\\n- ".join(names))
    if active_instructions:
        parts.append(MCP_INSTRUCTION_SYSTEM_RULE)
    if reference_context is not None:
        parts.append(REFERENCE_CONTEXT_SYSTEM_RULE)
    parts.append(SUB_RUN_RULES)
    if active_instructions:
        parts.append(
            "Administrativ vertrauenswürdige Anweisungen der verbundenen "
            "MCP-Server:\\n\\n"
            + "\\n\\n".join(
                f"### MCP-Server {name}\\n{instructions}"
                for name, instructions in active_instructions
            )
        )
    system = "\\n\\n".join(parts)
    messages = [{"role": "system", "content": system}]
    if reference_context is not None:
        messages.append({"role": "user", "content": reference_context})
    messages.append({
        "role": "user",
        "content": json.dumps({
            "task": task, "plan_step_id": step,
            "existing_evidences": [
                parent.model_snapshot()["evidences"][list(parent.evidences).index(i)]
                for i in ids
            ],
        }, ensure_ascii=False),
    })
    # Retain the delegated evidence state for the UI's model-visible context
    # projection, even after the coordinator's state is restored.
    record = {"id": run_id, "task": task, "messages": messages, "tools": copy.deepcopy(tools),
              "evidence_state": local, "status": "running"}
    agent._sub_runs.append(record)
    from .agent_loop import ModelLoopRunState, run_model_loop
    previous = agent._evidence_state
    previous_coordinator = getattr(agent, "_coordinator_evidence_state", None)
    agent._coordinator_evidence_state = previous
    try:
        agent._evidence_state = local
        answer = await run_model_loop(
            agent, messages=messages, tools=tools, routes=agent._tool_routes,
            enabled_server_names=set(agent._active_servers),
            max_tool_calls=min(agent.max_tool_calls, MAX_SUB_RUN_TOOLS),
            phase="sub", run_state=ModelLoopRunState(),
            reserved_tool_call_ids={
                call["id"]
                for message in (coordinator_messages or [])
                if message.get("role") == "assistant"
                for call in message.get("tool_calls", [])
                if isinstance(call, dict) and isinstance(call.get("id"), str)
            } | {
                message["tool_call_id"]
                for message in (coordinator_messages or [])
                if message.get("role") == "tool"
                and isinstance(message.get("tool_call_id"), str)
            },
        )
        result = validate_sub_result(answer, set(local.evidences) - inherited)
        if result["status"] in {"completed", "partial"}:
            # Child IDs are local to the delegated context. Remap collisions
            # deterministically and return the promoted IDs to the coordinator.
            additions = {}
            promoted_ids = []
            for child_id in result["evidence_ids"]:
                entry = copy.deepcopy(local.evidences[child_id])
                promoted_id = child_id
                if promoted_id in parent.evidences or promoted_id in additions:
                    suffix = 1
                    prefix = run_id + "-" + child_id
                    promoted_id = prefix[:80]
                    while promoted_id in parent.evidences or promoted_id in additions:
                        suffix += 1
                        promoted_id = (prefix[:76] + "-" + str(suffix))[:80]
                entry["id"] = promoted_id
                if step is not None:
                    entry["plan_step_id"] = step
                additions[promoted_id] = entry
                promoted_ids.append(promoted_id)
            result["evidence_ids"] = promoted_ids
            candidate = {**parent.evidences, **additions}
            parent._check_budget(candidate)
            parent.evidences = candidate
            parent.compacted.update(source["call_id"] for entry in additions.values() for source in entry["tool_calls"])
            parent._refresh_live_tool_definition()
            for call_id, source in local.tool_results.items():
                if call_id not in parent.tool_results:
                    parent.tool_results[call_id] = copy.deepcopy(source)
            parent._next_source_number = max(parent._next_source_number, local._next_source_number)
            parent.refresh_model_tools()
        else:
            # No findings were imported for blocked/failed runs. Never return
            # local IDs that cannot be resolved in the coordinator's state.
            result["evidence_ids"] = []
        record["status"] = result["status"]
        return result
    except Exception:
        record["status"] = "failed"
        raise
    finally:
        agent._evidence_state = previous
        if previous_coordinator is None:
            agent.__dict__.pop("_coordinator_evidence_state", None)
        else:
            agent._coordinator_evidence_state = previous_coordinator
