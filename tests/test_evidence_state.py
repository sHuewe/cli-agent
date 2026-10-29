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
    assert len(visible) == len(history)
    assert [c["id"] for c in visible[2]["tool_calls"]] == ["call-1", "call-2"]


def test_internal_evidence_tool_call_is_transient_in_main_loop():
    import asyncio
    from types import SimpleNamespace
    from cli_agent.agent_loop import run_model_loop
    from cli_agent.config import LoggingConfig
    from cli_agent.evidence_state import EVIDENCE_TOOL

    class Model:
        def __init__(self):
            self.responses = [
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "internal-1", "type": "function",
                     "function": {"name": EVIDENCE_TOOL_NAME, "arguments": {
                         "evidences": [{
                             "id": "fact-1", "type": "observation",
                             "statement": "Sensor value was 100 W.",
                             "tool_calls": [{"call_id": "old-1", "description": "Measurement"}]
                         }]
                     }}}
                ]},
                {"role": "assistant", "content": "Final answer"}
            ]
            self.seen = []

        async def chat(self, *, messages, tools):
            self.seen.append(copy.deepcopy(messages))
            return self.responses.pop(0)

    state = EvidenceState()
    state.register_result("old-1", tool_name="sensor")
    model = Model()
    agent = SimpleNamespace(
        _evidence_state=state,
        model_client=model,
        logging_config=LoggingConfig(),
        _agui_frontend_tools={},
        _dump_context=lambda *a, **k: None,
        _dump_value=lambda *a, **k: None,
    )
    messages = [
        {"role": "system", "content": "Rules"},
        {"role": "user", "content": "Question"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "old-1", "type": "function",
             "function": {"name": "sensor", "arguments": "{}"}}
        ]},
        {"role": "tool", "tool_call_id": "old-1", "content": "100 W"},
    ]
    answer = asyncio.run(run_model_loop(
        agent, messages=messages, tools=[EVIDENCE_TOOL],
        routes={}, enabled_server_names=None, max_tool_calls=5,
        phase="main",
    ))
    assert answer == "Final answer"
    assert len(model.seen) == 2
    assert all(m.get("role") != "tool" for m in model.seen[1])
    assert "fact-1" in str(model.seen[1])
    assert not any(
        c.get("function", {}).get("name") == EVIDENCE_TOOL_NAME
        for m in messages for c in m.get("tool_calls", [])
    )
    assert state.snapshot()["compacted_tool_call_ids"] == ["old-1"]



def test_evidence_is_available_by_default_with_tools(tmp_path):
    from cli_agent.agent import CliAgent

    agent = CliAgent(tmp_path, object(), ())
    assert isinstance(agent._evidence_state, EvidenceState)
    assert agent._model_tools() == []  # No tool results to summarize.
    agent._server_tools = {
        "sensor": [
            {"type": "function", "function": {
                "name": "sensor__get_value",
                "parameters": {"type": "object", "properties": {}},
            }}
        ]
    }
    agent._active_servers = {"sensor"}
    names = {tool["function"]["name"] for tool in agent._model_tools()}
    assert names == {"sensor__get_value", EVIDENCE_TOOL_NAME}
