"""Sub-run integration tests for coordinator state isolation and evidence promotion."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from cli_agent.state import sub_runs
from cli_agent.state.evidence_state import EvidenceState


class Coordinator:
    def __init__(self):
        self._evidence_state = EvidenceState()
        self._sub_runs = []
        self._server_instructions = {}
        self._active_servers = set()
        self._tool_routes = {}
        self.model_client = SimpleNamespace(last_usage={"tokens": 2}, last_finish_reason="stop")

    def _model_tools(self):
        return [
            {"type": "function", "function": {"name": name}}
            for name in (
                "agent__start_sub_run", "agent__update_plan",
                "agent__publish_artifact", "server__read",
            )
        ]

    def _main_tools_need_workspace_context(self, names):
        return False

    def _build_reference_context_message(self, *, knowledge):
        return None

    def _format_current_time(self, timestamp):
        return "2026-10-31T12:00:00Z"


def _coordinator_with_evidence():
    agent = Coordinator()
    parent = agent._evidence_state
    parent.update_plan({
        "goal": "Investigate source data",
        "steps": [{"id": "read", "description": "Read source", "status": "in_progress"}],
    })
    parent.register_result(
        "parent-call", tool_name="server__read",
        artifact_metadata={"workspace_path": "data/parent.json", "records_path": "/rows"},
    )
    parent.add_evidence({
        "id": "inherited", "type": "observation", "statement": "Original",
        "source_refs": ["S1"], "plan_step_id": "read",
    })
    return agent


def _answer(status, evidence_ids):
    return json.dumps({
        "status": status, "summary": "Source reviewed",
        "evidence_ids": evidence_ids, "confidence": "high", "uncertainties": [],
    })


@pytest.mark.asyncio
async def test_sub_run_remaps_child_id_collision_and_restores_coordinator(monkeypatch):
    agent = _coordinator_with_evidence()
    parent = agent._evidence_state
    parent.register_result("other-parent-call", tool_name="server__read")
    parent.add_evidence({
        "id": "collision", "type": "observation", "statement": "Parent owns this ID",
        "source_refs": ["S2"], "plan_step_id": "read",
    })
    original_parent = copy.deepcopy(parent.evidences)
    original_model = agent.model_client
    child_models = []

    async def child_loop(child_agent, **kwargs):
        assert kwargs["phase"] == "sub"
        assert child_agent.model_client is not original_model
        child_models.append(child_agent.model_client)
        local = child_agent._evidence_state
        assert local is not parent
        assert "inherited" in local.evidences
        assert "plan_step_id" not in local.evidences["inherited"]
        assert local.plan is None
        names = [tool["function"]["name"] for tool in kwargs["tools"]]
        assert "server__read" in names
        assert "agent__start_sub_run" not in names
        assert "agent__update_plan" not in names
        assert "agent__publish_artifact" not in names
        local.register_result("child-call", tool_name="server__read")
        local.add_evidence({
            "id": "collision", "type": "calculation", "statement": "Child calculation",
            "source_refs": ["S3"],
        })
        return _answer("completed", ["collision"])

    monkeypatch.setattr("cli_agent.runtime.agent_loop.run_model_loop", child_loop)
    result = await sub_runs.execute_sub_run(
        agent, {
            "task": "Calculate a verifiable, narrowly scoped source result",
            "plan_step_id": "read", "evidence_ids": ["inherited"],
        },
        coordinator_messages=[{
            "role": "assistant",
            "tool_calls": [{"id": "main-call", "function": {"name": "server__read"}}],
        }],
    )
    assert len(child_models) == 1
    assert agent.model_client is original_model
    assert agent._evidence_state is parent
    assert not hasattr(agent, "_coordinator_evidence_state")
    assert parent.evidences["inherited"] == original_parent["inherited"]
    assert parent.evidences["collision"] == original_parent["collision"]
    promoted_id, = result["evidence_ids"]
    assert promoted_id != "collision"
    assert promoted_id.startswith("sub-")
    assert parent.evidences[promoted_id]["statement"] == "Child calculation"
    assert parent.evidences[promoted_id]["plan_step_id"] == "read"
    assert parent.evidences[promoted_id]["tool_calls"][0]["call_id"] == "child-call"
    assert "child-call" in parent.compacted
    assert parent.tool_results["child-call"]["source_ref"] == "S3"
    assert agent._sub_runs[0]["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["blocked", "failed"])
async def test_blocked_or_failed_child_never_promotes_local_findings(monkeypatch, status):
    agent = _coordinator_with_evidence()
    parent = agent._evidence_state
    before = copy.deepcopy(parent.snapshot())
    original_model = agent.model_client

    async def child_loop(child_agent, **kwargs):
        local = child_agent._evidence_state
        local.register_result("child-call", tool_name="server__read")
        local.add_evidence({
            "id": "child-only", "type": "observation", "statement": "Not promoted",
            "source_refs": ["S2"],
        })
        return _answer(status, ["child-only"])

    monkeypatch.setattr("cli_agent.runtime.agent_loop.run_model_loop", child_loop)
    result = await sub_runs.execute_sub_run(
        agent, {"task": "Inspect external source and report inability to verify"},
    )
    assert result["evidence_ids"] == []
    assert parent.snapshot() == before
    assert agent.model_client is original_model
    assert agent._evidence_state is parent
    assert agent._sub_runs[0]["status"] == status


@pytest.mark.asyncio
async def test_evidence_budget_rejection_is_atomic_and_restores_parent(monkeypatch):
    agent = _coordinator_with_evidence()
    parent = agent._evidence_state
    original_model = agent.model_client
    original = copy.deepcopy(parent.snapshot())

    async def child_loop(child_agent, **kwargs):
        local = child_agent._evidence_state
        local.register_result("child-call", tool_name="server__read")
        local.add_evidence({
            "id": "child-fact", "type": "observation", "statement": "Valid child finding",
            "source_refs": ["S2"],
        })
        return _answer("completed", ["child-fact"])

    def reject_budget(candidate):
        assert "child-fact" in candidate
        raise ValueError("Evidence budget exceeded")

    monkeypatch.setattr("cli_agent.runtime.agent_loop.run_model_loop", child_loop)
    monkeypatch.setattr(parent, "_check_budget", reject_budget)
    with pytest.raises(ValueError, match="Evidence budget exceeded"):
        await sub_runs.execute_sub_run(
            agent, {"task": "Analyze one candidate and save a verified observation"},
        )
    assert parent.snapshot() == original
    assert agent._sub_runs[0]["status"] == "failed"
    assert agent.model_client is original_model
    assert agent._evidence_state is parent
    assert not hasattr(agent, "_coordinator_evidence_state")


@pytest.mark.asyncio
async def test_model_exception_restores_parent_state_even_if_child_mutates_it(monkeypatch):
    agent = _coordinator_with_evidence()
    parent = agent._evidence_state
    previous_model = agent.model_client
    before = copy.deepcopy(parent.snapshot())

    async def child_loop(child_agent, **kwargs):
        child_agent._evidence_state.register_result("uncommitted", tool_name="server__read")
        raise RuntimeError("model disconnected")

    monkeypatch.setattr("cli_agent.runtime.agent_loop.run_model_loop", child_loop)
    with pytest.raises(RuntimeError, match="model disconnected"):
        await sub_runs.execute_sub_run(
            agent, {"task": "Inspect one evidence source and provide a bounded result"},
        )
    assert agent.model_client is previous_model
    assert agent._evidence_state is parent
    assert parent.snapshot() == before
    assert agent._sub_runs[0]["status"] == "failed"
