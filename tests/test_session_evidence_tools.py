"""Tests for separated evidence tools, short refs and session-wide lifecycle."""
from __future__ import annotations

import copy

import pytest

from cli_agent.evidence_state import EvidenceState
from cli_agent.evidence_tools import EvidenceInputError


def _plan():
    return {"goal": "Power study", "steps": [
        {"id": "read", "description": "Read data", "status": "in_progress"},
        {"id": "compute", "description": "Compute", "status": "pending"},
    ]}


def test_short_source_refs_are_stable_and_verified():
    state = EvidenceState()
    assert state.register_result("very-long-tool-call-1", tool_name="energy__history",
                                 artifact_metadata={"workspace_path": "mcp-data-a/r.json",
                                                    "records_path": "/points",
                                                    "row_count": 20}) == "S1"
    assert state.register_result("very-long-tool-call-1", tool_name="wrong") == "S1"
    assert state.register_result("long-call-2", tool_name="data__resample_data",
                                 artifact_metadata={"workspace_path": "mcp-data-a/r2.json",
                                                    "records_path": "/rows"}) == "S2"
    state.update_plan(_plan())
    state.add_evidence({"id": "measure", "type": "calculation",
                        "statement": "Resampled power values.",
                        "source_refs": ["S1", "S2"], "plan_step_id": "compute"})
    refs = state.evidences["measure"]["tool_calls"]
    assert [x["source_ref"] for x in refs] == ["S1", "S2"]
    assert [x["call_id"] for x in refs] == ["very-long-tool-call-1", "long-call-2"]
    assert refs[0]["tool_name"] == "energy__history"
    assert refs[0]["records_path"] == "/points"
    assert refs[0]["row_count"] == 20
    assert state.compacted == {"very-long-tool-call-1", "long-call-2"}
    source_schema = state.tool_definitions()[1]["function"]["parameters"]["properties"]["source_refs"]["items"]
    assert source_schema["enum"] == ["S1", "S2"]
    snapshot = copy.deepcopy(state.snapshot())
    with pytest.raises(EvidenceInputError, match="Unbekannte"):
        state.add_evidence({"id": "bad", "type": "observation",
                            "statement": "Wrong source.", "source_refs": ["S999"],
                            "plan_step_id": "read"})
    assert state.snapshot() == snapshot


def test_new_task_preserves_evidence_but_not_plan_or_expired_paths():
    state = EvidenceState()
    state.register_result("first-run", tool_name="sensor",
                          artifact_metadata={"workspace_path": "mcp-data/r1.json",
                                             "records_path": "/rows"})
    state.update_plan(_plan())
    state.add_evidence({"id": "fact", "type": "observation",
                        "statement": "A result worth remembering.",
                        "source_refs": ["S1"], "plan_step_id": "read"})
    state.begin_task()
    assert state.plan is None
    assert state.evidences["fact"]["statement"] == "A result worth remembering."
    assert "plan_step_id" not in state.evidences["fact"]
    assert "workspace_path" not in state.evidences["fact"]["tool_calls"][0]
    assert state.evidences["fact"]["tool_calls"][0]["artifact_expired"] is True
    assert state.compacted == set()
    assert state.register_result("second-run", tool_name="sensor") == "S2"
    state.update_plan({"goal": "Next task", "steps": [
        {"id": "read", "status": "in_progress", "description": "Different task"},
    ]})
    state.add_evidence({"id": "new", "type": "observation",
                        "statement": "New session finding.", "source_refs": ["S2"],
                        "plan_step_id": "read"})
    assert set(state.evidences) == {"fact", "new"}
    assert state.evidences["fact"]["tool_calls"][0]["call_id"] == "first-run"
    assert state.evidences["new"]["tool_calls"][0]["call_id"] == "second-run"


def test_plan_and_evidence_updates_are_independent():
    state = EvidenceState()
    state.update_plan(_plan())
    state.update_plan({"steps": [{"id": "read", "status": "completed"}]})
    assert state.plan["steps"][0]["description"] == "Read data"
    assert state.plan["steps"][0]["status"] == "completed"
    state.register_result("call-a", tool_name="sensor")
    state.add_evidence({"id": "note", "type": "observation", "statement": "Initial",
                        "source_refs": ["S1"], "plan_step_id": "read"})
    state.update_evidence({"id": "note", "statement": "Revised"})
    assert state.evidences["note"]["statement"] == "Revised"
    assert state.evidences["note"]["tool_calls"][0]["source_ref"] == "S1"
    before = copy.deepcopy(state.snapshot())
    with pytest.raises(EvidenceInputError, match="existiert"):
        state.add_evidence({"id": "note", "type": "observation",
                            "statement": "Duplicate", "source_refs": ["S1"],
                            "plan_step_id": "read"})
    assert state.snapshot() == before
    with pytest.raises(EvidenceInputError, match="Planschritt"):
        state.update_plan({"remove_step_ids": ["read"]})
    assert state.snapshot() == before
    state.update_evidence({"id": "note", "remove": True})
    state.update_plan({"remove_step_ids": ["read"]})
    assert state.plan["steps"][0]["id"] == "compute"


def test_source_registry_is_session_bounded_and_ids_never_reused():
    state = EvidenceState()
    for i in range(1003):
        ref = state.register_result(f"call-{i}", tool_name="sensor")
        assert ref == f"S{i + 1}"
    assert len(state.tool_results) == 1000
    assert "call-0" not in state.tool_results
    assert "call-1002" in state.tool_results
    state.begin_task()
    assert state.register_result("later", tool_name="sensor") == "S1004"


def test_reset_history_explicitly_clears_session_evidence(tmp_path):
    from cli_agent.agent import CliAgent
    agent = CliAgent(tmp_path, object(), ())
    agent._evidence_state.register_result("call-x", tool_name="sensor")
    agent._evidence_state.add_evidence({"id": "note", "type": "observation",
                                        "statement": "Session note.",
                                        "source_refs": ["S1"]})
    agent.reset_history()
    assert agent._evidence_state.evidences == {}
    assert agent._evidence_state.tool_results == {}
