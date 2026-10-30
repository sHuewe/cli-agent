"""Optional, bounded delegated model runs within the owning agent session."""
from __future__ import annotations

import copy
import json
import uuid
from typing import Any

from .agent_conversation import _normalize_json_response

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
            "EXPENSIVE: starts a separate multi-step LLM context and incurs additional "
            "model calls, tokens and latency. Delegate only substantial, independent "
            "investigations where context isolation clearly helps. Never delegate "
            "single tool queries, simple calculations, plan updates or summaries. "
            "Sub-runs share the current session's permissions and artifacts, and "
            "return a compact JSON result and selected verified evidence IDs."
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


async def execute_sub_run(agent: Any, args: dict[str, Any]) -> dict[str, Any]:
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
    system = agent._build_system_prompt()
    system = system.split("Nutze agent__update_plan")[0] + SUB_RUN_RULES
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps({
            "task": task, "plan_step_id": step,
            "existing_evidences": [parent.model_snapshot()["evidences"][list(parent.evidences).index(i)] for i in ids],
        }, ensure_ascii=False)},
    ]
    record = {"id": run_id, "task": task, "messages": messages, "tools": copy.deepcopy(tools),
              "status": "running"}
    agent._sub_runs.append(record)
    from .agent_loop import ModelLoopRunState, run_model_loop
    previous = agent._evidence_state
    try:
        agent._evidence_state = local
        answer = await run_model_loop(
            agent, messages=messages, tools=tools, routes=agent._tool_routes,
            enabled_server_names=set(agent._active_servers),
            max_tool_calls=min(agent.max_tool_calls, MAX_SUB_RUN_TOOLS),
            phase="sub", run_state=ModelLoopRunState(),
        )
        result = validate_sub_result(answer, set(local.evidences) - inherited)
        if result["status"] in {"completed", "partial"}:
            additions = {i: copy.deepcopy(local.evidences[i]) for i in result["evidence_ids"]}
            if set(additions) & set(parent.evidences):
                raise ValueError("Sub-Run Evidence-ID bereits im Hauptlauf vergeben.")
            candidate = {**parent.evidences, **additions}
            parent._check_budget(candidate)
            parent.evidences = candidate
            for call_id, source in local.tool_results.items():
                if call_id not in parent.tool_results:
                    parent.tool_results[call_id] = copy.deepcopy(source)
            parent._next_source_number = max(parent._next_source_number, local._next_source_number)
            parent.refresh_model_tools()
        record["status"] = result["status"]
        return result
    except Exception:
        record["status"] = "failed"
        raise
    finally:
        agent._evidence_state = previous
