from __future__ import annotations

import copy
import pytest

from cli_agent.evidence_state import EvidenceError, EvidenceState, EVIDENCE_TOOL_NAME


def _entry(call_id="call-1", evidence_id="e0"):
    return {
        "id": evidence_id,
        "type": "observation",
        "statement": "Observed 100 W at 10:00Z.",
        "tool_calls": [{"call_id": call_id, "description": "Power measurement"}],
    }


def _history():
    return [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "sensor", "arguments": "{}"}},
            {"id": "call-2", "type": "function", "function": {"name": "sensor", "arguments": "{}"}},
            {"id": "evidence-call", "type": "function",
             "function": {"name": EVIDENCE_TOOL_NAME, "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call-1", "content": "data 1"},
        {"role": "tool", "tool_call_id": "call-2", "content": "data 2"},
    ]


def test_source_artifacts_are_attached_and_reference_is_auto_compacted():
    state = EvidenceState()
    state.register_result("call-1", tool_name="data__select_data",
                          artifact_metadata={
                              "workspace_path": "mcp-data-123/r-1.json",
                              "records_path": "/rows",
                          })
    state.register_result("call-2", tool_name="sensor")
    state.update({"evidences": [_entry()]})
    evidence = state.snapshot()["evidences"][0]
    assert evidence["tool_calls"][0]["workspace_path"] == "mcp-data-123/r-1.json"
    assert evidence["tool_calls"][0]["records_path"] == "/rows"
    assert evidence["tool_calls"][0]["tool_name"] == "data__select_data"
    original = _history()
    saved = copy.deepcopy(original)
    projection = state.project(original)
    assert original == saved
    calls = [m for m in projection if m["role"] == "assistant"][0]["tool_calls"]
    assert [c["id"] for c in calls] == ["call-2"]
    assert [m["tool_call_id"] for m in projection if m["role"] == "tool"] == ["call-2"]
    assert sum("evidence state" in str(m.get("content")) for m in projection) == 1


def test_discard_unreferenced_without_artifact_and_restore():
    state = EvidenceState()
    state.register_result("call-1", tool_name="sensor")
    state.register_result("call-2", tool_name="sensor")
    state.update({"discard_tool_call_ids": ["call-1", "call-2"]})
    assert all(m["role"] != "tool" for m in state.project(_history()))
    state.update({"restore_tool_call_ids": ["call-2"]})
    projection = state.project(_history())
    assert [m["tool_call_id"] for m in projection if m["role"] == "tool"] == ["call-2"]
    assert state.snapshot()["compacted_tool_call_ids"] == ["call-1"]


def test_unknown_source_rejected_atomically():
    state = EvidenceState()
    state.register_result("call-1", tool_name="sensor")
    state.update({"evidences": [_entry()]})
    before = state.snapshot()
    with pytest.raises(EvidenceError, match="Unbekannte"):
        state.update({"evidences": [_entry("missing", "e1")],
                      "discard_tool_call_ids": ["call-1"]})
    assert state.snapshot() == before


def test_invalid_sources_and_payload_cannot_sneak_paths_into_evidence():
    state = EvidenceState()
    state.register_result("call-1", tool_name="sensor",
                          artifact_metadata={"workspace_path": "owned.json"})
    bad = _entry()
    bad["tool_calls"][0]["workspace_path"] = "../other.json"
    with pytest.raises(EvidenceError, match="Quellen"):
        state.update({"evidences": [bad]})
    with pytest.raises(EvidenceError, match="Evidence"):
        state.update({"evidences": [{**_entry(), "extra": "x"}]})


def test_existing_evidence_is_replaced_and_can_be_removed():
    state = EvidenceState()
    state.register_result("call-1", tool_name="sensor")
    state.update({"evidences": [_entry()]})
    changed = _entry()
    changed["statement"] = "Corrected measurement."
    state.update({"evidences": [changed]})
    assert len(state.snapshot()["evidences"]) == 1
    assert state.snapshot()["evidences"][0]["statement"] == "Corrected measurement."
    state.update({"remove_evidence_ids": ["e0"]})
    assert state.snapshot()["evidences"] == []


def test_failed_update_is_not_permanently_injected():
    state = EvidenceState()
    state.pending_error = "Incorrect source"
    assert "Incorrect source" in state.project(_history())[-1]["content"]
    state.register_result("call-1", tool_name="sensor")
    state.update({"evidences": [_entry()]})
    assert state.pending_error is None


def test_no_evidence_has_no_added_context():
    state = EvidenceState()
    history = _history()
    visible = state.project(history)
    assert len(visible) == len(history) - 1
    assert visible[2]["tool_calls"][-1]["id"] == "call-2"
