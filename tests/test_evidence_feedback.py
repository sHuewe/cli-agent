"""Regression tests for plan feedback and compact working-memory projection."""
import asyncio
import copy
import json
from types import SimpleNamespace

from cli_agent.evidence_state import EvidenceState


def _plan():
    return {"goal": "Compare phases", "steps": [
        {"id": "s1", "description": "Load data", "status": "in_progress"},
        {"id": "s2", "description": "Compare signals", "status": "pending"},
    ]}


def test_plan_creation_update_and_noop_feedback():
    state = EvidenceState()
    created = state.update_plan(_plan())
    assert created["status"] == "created"
    assert created["plan"]["steps"][0] == {"id": "s1", "status": "in_progress"}
    updated = state.update_plan({"steps": [{"id": "s1", "status": "completed"}]})
    assert updated["status"] == "updated"
    assert updated["changes"] == [{
        "id": "s1", "action": "changed",
        "fields": {"status": {"before": "in_progress", "after": "completed"}},
    }]
    repeated = state.update_plan({"steps": [{"id": "s1", "status": "completed"}]})
    assert repeated["status"] == "unchanged"
    assert "Keinen identischen Aufruf" in repeated["message"]
    added = state.update_plan({"steps": [
        {"id": "s3", "description": "Conclude", "status": "pending"},
    ]})
    assert added["status"] == "updated"
    assert added["changes"][0] == {"id": "s3", "action": "added", "status": "pending"}


def test_compact_projection_retains_internal_provenance():
    state = EvidenceState()
    state.register_result("opaque-tool-id", tool_name="sensor", artifact_metadata={
        "workspace_path": "mcp-data/abc.json",
        "records_path": "/points", "row_count": 101,
        "columns": ["time", "value"],
    })
    state.add_evidence({"id": "fact", "type": "observation",
                        "statement": "Observed 17 W", "source_refs": ["S1"]})
    original = copy.deepcopy(state.snapshot())
    assert state.model_snapshot() == {"evidences": [{
        "id": "fact", "type": "observation",
        "statement": "Observed 17 W", "source_refs": ["S1"],
    }]}
    assert state.snapshot() == original
    state.begin_task()
    assert state.model_snapshot()["evidences"][0]["source_refs"] == ["S1"]
    assert state.evidences["fact"]["tool_calls"][0]["artifact_expired"] is True


def test_plan_feedback_is_model_visible():
    from cli_agent.agent_loop import run_model_loop
    from cli_agent.config import LoggingConfig

    class Model:
        def __init__(self):
            self.calls = 0

        async def chat(self, *, messages, tools):
            self.calls += 1
            if self.calls < 3:
                return {"role": "assistant", "content": "", "tool_calls": [{
                    "id": f"plan-{self.calls}", "type": "function",
                    "function": {"name": "agent__update_plan", "arguments":
                                 _plan() if self.calls == 1 else {
                                     "steps": [{"id": "s1", "status": "in_progress"}]
                                 }},
                }]}
            return {"role": "assistant", "content": "done"}

    state = EvidenceState()
    agent = SimpleNamespace(
        _evidence_state=state, model_client=Model(),
        _agui_frontend_tools={}, logging_config=LoggingConfig(),
        _dump_context=lambda *a, **kw: None,
        _dump_value=lambda *a, **kw: None,
    )
    messages = [{"role": "system", "content": "rules"}]
    assert asyncio.run(run_model_loop(
        agent, messages=messages, tools=state.tool_definitions(),
        routes={}, enabled_server_names=None, max_tool_calls=4, phase="main",
    )) == "done"
    responses = [json.loads(msg["content"]) for msg in messages
                 if msg.get("role") == "tool"]
    assert [result["status"] for result in responses] == ["created", "unchanged"]
    assert all("plan" in result and "evidences" not in result for result in responses)


def test_model_projection_appends_single_snapshot_after_completed_tool_results():
    state = EvidenceState()
    state.update_plan(_plan())
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "", "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "sensor", "arguments": "{}"},
        }]},
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]
    original = copy.deepcopy(messages)
    projected = state.project(messages)
    assert projected[:-1] == original
    assert projected[-1]["role"] == "user"
    assert "[cli-agent evidence state:" in projected[-1]["content"]
    assert json.loads(projected[-1]["content"].split("\\n", 1)[1]) == state.model_snapshot()
    assert messages == original
    assert sum("[cli-agent evidence state:" in str(msg.get("content", ""))
               for msg in projected) == 1
