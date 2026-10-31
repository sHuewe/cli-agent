"""Agent-loop internal tool ordering, failure and callback regressions."""
from __future__ import annotations

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from cli_agent.runtime import agent_loop
from cli_agent.settings.config import LoggingConfig
from cli_agent.state.evidence_state import EvidenceState
from cli_agent.state.evidence_tools import ADD_EVIDENCE_TOOL_NAME, PLAN_TOOL_NAME
from cli_agent.state.sub_runs import START_SUB_RUN
from cli_agent.platform.artifact_publication import PUBLISH_TOOL_NAME


class Model:
    def __init__(self, tool_calls):
        self.replies = [
            {"role": "assistant", "content": "", "tool_calls": tool_calls},
            {"role": "assistant", "content": "Finished"},
        ]
        self.seen = []

    async def chat(self, *, messages, tools):
        self.seen.append(copy.deepcopy(messages))
        return self.replies.pop(0)


def call(name, arguments, call_id):
    return {
        "id": call_id, "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


async def run_internal(calls, *, monkeypatch=None):
    events = []
    evidence = EvidenceState()
    model = Model(calls)

    async def callback(kind, data):
        events.append((kind, dict(data)))

    agent = SimpleNamespace(
        _evidence_state=evidence,
        model_client=model,
        logging_config=LoggingConfig(),
        _agui_frontend_tools={},
        event_callback=callback,
        _dump_context=lambda *a, **k: None,
        _dump_value=lambda *a, **k: None,
    )
    messages = [{"role": "system", "content": "Rules"}]
    output = await agent_loop.run_model_loop(
        agent, messages=messages, tools=evidence.tool_definitions(),
        routes={}, enabled_server_names=None, max_tool_calls=10, phase="main",
    )
    return output, agent, messages, events, model


@pytest.mark.asyncio
async def test_bad_plan_reports_error_without_fake_success_and_keeps_next_answer():
    output, agent, messages, events, model = await run_internal([
        call(PLAN_TOOL_NAME, {"steps": [{"id": "missing"}]}, "plan-1"),
    ])
    assert output == "Finished"
    assert agent._evidence_state.plan is None
    result = next(m for m in messages if m.get("tool_call_id") == "plan-1")
    response = json.loads(result["content"])
    assert response["status"] == "error"
    assert events == [
        ("tool_started", {"call_id": "plan-1", "tool": PLAN_TOOL_NAME}),
        ("tool_finished", {"call_id": "plan-1", "tool": PLAN_TOOL_NAME, "status": "error"}),
    ]
    assert len(model.seen) == 2


@pytest.mark.asyncio
async def test_evidence_update_failure_is_protocol_complete_and_redacted():
    output, agent, messages, events, model = await run_internal([
        call(ADD_EVIDENCE_TOOL_NAME, {
            "id": "fact", "type": "observation",
            "statement": "No trusted source", "source_refs": ["S999"],
        }, "evidence-1"),
    ])
    assert output == "Finished"
    assert agent._evidence_state.evidences == {}
    tool = next(m for m in messages if m.get("tool_call_id") == "evidence-1")
    assert "NICHT übernommen" in tool["content"]
    internal = messages[1]["tool_calls"][0]
    assert internal["function"]["arguments"] == {}
    assert events[-1][1]["status"] == "error"


@pytest.mark.asyncio
async def test_bad_subrun_call_reports_failure_without_running_mcp(monkeypatch):
    invoked = []
    async def fake_subrun(*args, **kwargs):
        invoked.append((args, kwargs))
        raise ValueError("No valid evidence IDs")
    monkeypatch.setattr(agent_loop, "execute_sub_run", fake_subrun)
    output, agent, messages, events, model = await run_internal([
        call(START_SUB_RUN, {"task": "Find evidence"}, "subrun-1"),
    ])
    assert output == "Finished"
    assert len(invoked) == 1
    result = json.loads(next(m for m in messages if m.get("tool_call_id") == "subrun-1")["content"])
    assert result == {"status": "failed", "error": "No valid evidence IDs"}
    assert [kind for kind, _ in events] == ["tool_started", "tool_finished"]
    assert events[-1][1]["status"] == "error"


@pytest.mark.asyncio
async def test_publish_validation_rejects_partial_arguments_without_side_effect(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("register_artifact called with incomplete arguments")
    monkeypatch.setattr(agent_loop, "register_artifact", unexpected)
    output, agent, messages, events, model = await run_internal([
        call(PUBLISH_TOOL_NAME, {"path": "data/result.json"}, "publish-1"),
    ])
    assert output == "Finished"
    result = next(m for m in messages if m.get("tool_call_id") == "publish-1")
    assert "konnte nicht veröffentlicht werden" in result["content"]
    assert events[-1][1]["status"] == "error"


@pytest.mark.asyncio
async def test_mixed_internal_batch_reports_events_for_each_call(monkeypatch):
    async def fake_subrun(*args, **kwargs):
        return {"status": "partial", "evidence_ids": [], "summary": "No evidence"}
    monkeypatch.setattr(agent_loop, "execute_sub_run", fake_subrun)
    output, agent, messages, events, model = await run_internal([
        call(PLAN_TOOL_NAME, {
            "goal": "Study readings",
            "steps": [{"id": "read", "description": "Read source", "status": "in_progress"}],
        }, "plan-1"),
        call(START_SUB_RUN, {"task": "Investigate one defined source"}, "sub-1"),
        call(PUBLISH_TOOL_NAME, {"path": "a"}, "pub-1"),
    ])
    assert output == "Finished"
    assert agent._evidence_state.plan["steps"][0]["id"] == "read"
    names = [item["tool_call_id"] for item in messages if item.get("role") == "tool"]
    assert names == ["plan-1", "sub-1", "pub-1"]
    assert [event[0] for event in events] == [
        "tool_started", "tool_finished",
        "tool_started", "tool_finished",
        "tool_started", "tool_finished",
    ]
    assert events[-1][1]["status"] == "error"
