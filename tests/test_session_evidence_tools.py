"""Tests for separated evidence tools, short refs and session-wide lifecycle."""
from __future__ import annotations

import copy

import pytest

from cli_agent.state.evidence_state import EvidenceState
from cli_agent.state.evidence_tools import EvidenceInputError


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
    from cli_agent.runtime.agent import CliAgent
    agent = CliAgent(tmp_path, object(), ())
    agent._evidence_state.register_result("call-x", tool_name="sensor")
    agent._evidence_state.add_evidence({"id": "note", "type": "observation",
                                        "statement": "Session note.",
                                        "source_refs": ["S1"]})
    agent.reset_history()
    assert agent._evidence_state.evidences == {}
    assert agent._evidence_state.tool_results == {}


def test_main_loop_routes_new_evidence_revisions_and_removals():
    import asyncio
    from types import SimpleNamespace
    from cli_agent.runtime.agent_loop import run_model_loop
    from cli_agent.settings.config import LoggingConfig

    class Model:
        def __init__(self):
            self.step = 0

        async def chat(self, *, messages, tools):
            self.step += 1
            if self.step == 1:
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "revise-1", "type": "function", "function": {
                        "name": "agent__update_evidence",
                        "arguments": {"id": "old", "statement": "Corrected"},
                    }},
                ]}
            if self.step == 2:
                assert state.evidences["old"]["statement"] == "Corrected"
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "remove-1", "type": "function", "function": {
                        "name": "agent__update_evidence",
                        "arguments": {"id": "old", "remove": True},
                    }},
                ]}
            assert "old" not in state.evidences
            return {"role": "assistant", "content": "Done"}

    state = EvidenceState()
    state.register_result("sensor-call", tool_name="sensor")
    state.add_evidence({"id": "old", "type": "observation",
                        "statement": "Original", "source_refs": ["S1"]})
    agent = SimpleNamespace(
        _evidence_state=state, model_client=Model(),
        _agui_frontend_tools={}, logging_config=LoggingConfig(),
        _dump_context=lambda *a, **k: None,
        _dump_value=lambda *a, **k: None,
    )
    messages = [{"role": "system", "content": "rules"}]
    answer = asyncio.run(run_model_loop(
        agent, messages=messages, tools=state.tool_definitions(),
        routes={}, enabled_server_names=None, max_tool_calls=5, phase="main",
    ))
    assert answer == "Done"
    assert not state.evidences
    completions = [m for m in messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in completions] == ["revise-1", "remove-1"]
    assert all("erfolgreich" in m["content"] for m in completions)


def test_server_owned_tool_takes_precedence_even_if_frontend_dict_is_poisoned():
    import asyncio
    from types import SimpleNamespace
    from cli_agent.runtime.agent_loop import run_model_loop
    from cli_agent.settings.config import LoggingConfig

    class Model:
        def __init__(self):
            self.step = 0

        async def chat(self, *, messages, tools):
            self.step += 1
            if self.step == 1:
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "plan-1", "type": "function", "function": {
                        "name": "agent__update_plan",
                        "arguments": {"goal": "X", "steps": [
                            {"id": "s1", "description": "X", "status": "in_progress"}
                        ]},
                    }},
                ]}
            assert state.plan is not None
            return {"role": "assistant", "content": "Done"}

    state = EvidenceState()
    agent = SimpleNamespace(
        _evidence_state=state, model_client=Model(),
        _agui_frontend_tools={"agent__update_plan": {"function": {}}},
        logging_config=LoggingConfig(),
        _dump_context=lambda *a, **k: None,
        _dump_value=lambda *a, **k: None,
    )
    messages = [{"role": "system", "content": "rules"}]
    answer = asyncio.run(run_model_loop(
        agent, messages=messages, tools=state.tool_definitions(),
        routes={}, enabled_server_names=None, max_tool_calls=5, phase="main",
    ))
    assert answer == "Done"
    assert state.plan["goal"] == "X"


def test_provider_id_reused_across_questions_receives_new_registered_id():
    import asyncio
    from types import SimpleNamespace
    from cli_agent.runtime.agent_loop import run_model_loop
    from cli_agent.settings.config import LoggingConfig

    class Model:
        def __init__(self):
            self.calls = 0

        async def chat(self, *, messages, tools):
            self.calls += 1
            if self.calls == 1:
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "reused-provider-id", "type": "function",
                     "function": {"name": "sensor__read", "arguments": {}}}
                ]}
            return {"role": "assistant", "content": "Done"}

    class Sensor:
        async def call_tool(self, name, args):
            return SimpleNamespace(
                content=[SimpleNamespace(text="new value")],
                meta=None, isError=False,
            )

    state = EvidenceState()
    assert state.register_result("reused-provider-id", tool_name="old_sensor") == "S1"
    state.add_evidence({"id": "old", "type": "observation",
                        "statement": "Previous reading", "source_refs": ["S1"]})
    state.begin_task()
    model = Model()
    agent = SimpleNamespace(
        _evidence_state=state, model_client=model, logging_config=LoggingConfig(),
        _agui_frontend_tools={}, _result_artifacts=None, workspace_access="none",
        _dump_context=lambda *a, **k: None, _dump_value=lambda *a, **k: None,
        _tool_is_enabled=lambda name: True,
        _tool_input_schema=lambda name, phase: {"type": "object"},
        _requires_approval=lambda *a, **kw: False,
    )
    messages = [{"role": "system", "content": "rules"}]
    assert asyncio.run(run_model_loop(
        agent, messages=messages,
        tools=[{"type": "function", "function": {
            "name": "sensor__read", "parameters": {"type": "object"},
        }}],
        routes={"sensor__read": (
            Sensor(), "read", SimpleNamespace(name="sensor", compress_result=False)
        )},
        enabled_server_names=None, max_tool_calls=3, phase="main",
    )) == "Done"
    new_id = messages[1]["tool_calls"][0]["id"]
    assert new_id != "reused-provider-id"
    assert state.tool_results["reused-provider-id"]["source_ref"] == "S1"
    assert state.tool_results[new_id]["source_ref"] == "S2"
    assert "[Quelle S2:" in messages[2]["content"]
    assert state.evidences["old"]["tool_calls"][0]["call_id"] == "reused-provider-id"


def test_cited_refs_remain_in_both_model_tool_enums_after_96_new_results():
    state = EvidenceState()
    state.register_result("first", tool_name="sensor")
    state.add_evidence({"id": "fact", "type": "observation",
                        "statement": "Old relevant source", "source_refs": ["S1"]})
    for n in range(2, 111):
        state.register_result(f"call-{n}", tool_name="sensor")
    for tool in state.tool_definitions()[1:]:
        offered = tool["function"]["parameters"]["properties"]["source_refs"]["items"]["enum"]
        assert "S1" in offered
        assert "S110" in offered
        assert "S2" not in offered
        assert len(offered) == 97
    state.update_evidence({"id": "fact", "source_refs": ["S1", "S110"]})
    assert [s["source_ref"] for s in state.evidences["fact"]["tool_calls"]] == ["S1", "S110"]


def test_split_tools_record_bounded_diagnostics_for_success_and_error():
    import asyncio
    from types import SimpleNamespace
    from cli_agent.runtime.agent_loop import run_model_loop
    from cli_agent.settings.config import LoggingConfig

    class Model:
        def __init__(self):
            self.i = 0

        async def chat(self, *, messages, tools):
            self.i += 1
            if self.i == 1:
                return {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "valid-plan", "type": "function", "function": {
                        "name": "agent__update_plan",
                        "arguments": {"goal": "test", "steps": [
                            {"id": "read", "description": "Fetch", "status": "in_progress"}
                        ]},
                    },
                }]}
            if self.i == 2:
                return {"role": "assistant", "content": "", "tool_calls": [{
                    "id": "invalid-evidence", "type": "function", "function": {
                        "name": "agent__add_evidence",
                        "arguments": {"id": "note", "type": "observation",
                                      "statement": "test", "source_refs": ["S900"],
                                      "plan_step_id": "read"},
                    },
                }]}
            return {"role": "assistant", "content": "done"}

    state = EvidenceState()
    state.debug_enabled = True
    agent = SimpleNamespace(
        _evidence_state=state, model_client=Model(),
        _agui_frontend_tools={}, logging_config=LoggingConfig(),
        _dump_context=lambda *a, **k: None,
        _dump_value=lambda *a, **k: None,
    )
    messages = [{"role": "system", "content": "rules"}]
    assert asyncio.run(run_model_loop(
        agent, messages=messages, tools=state.tool_definitions(),
        routes={}, enabled_server_names=None, max_tool_calls=4, phase="main",
    )) == "done"
    assert [e["status"] for e in state.debug_updates] == ["success", "error"]
    assert [e["call_id"] for e in state.debug_updates] == ["valid-plan", "invalid-evidence"]
    assert "read" in state.debug_updates[0]["arguments"]
    assert "S900" in state.debug_updates[1]["arguments"]
    assert "Unbekannte source_ref" in state.debug_updates[1]["error"]
    assert "debug_updates" not in state.snapshot()
    assert all(c["function"]["arguments"] == {}
               for m in messages for c in m.get("tool_calls", []))
