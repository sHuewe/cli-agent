from __future__ import annotations

import copy
import pytest

from cli_agent.evidence_state import EvidenceError, EvidenceState, EVIDENCE_TOOL, EVIDENCE_TOOL_NAME


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
    assert [c["id"] for c in calls] == ["call-2", "evidence-call"]
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
    assert [c["id"] for c in visible[2]["tool_calls"]] == ["call-1", "call-2", "evidence-call"]


def test_internal_evidence_tool_call_is_transient_in_main_loop():
    import asyncio
    from types import SimpleNamespace
    from cli_agent.agent_loop import run_model_loop
    from cli_agent.config import LoggingConfig
    from cli_agent.evidence_tools import ADD_EVIDENCE_TOOL_NAME

    class Model:
        def __init__(self):
            self.responses = [
                {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "internal-1", "type": "function",
                     "function": {"name": ADD_EVIDENCE_TOOL_NAME, "arguments": {
                         "id": "fact-1", "type": "observation",
                         "statement": "Sensor value was 100 W.",
                         "source_refs": ["S1"],
                     }}}
                ]},
                {"role": "assistant", "content": "Final answer"}
            ]
            self.seen = []

        async def chat(self, *, messages, tools):
            self.seen.append(copy.deepcopy(messages))
            return self.responses.pop(0)

    state = EvidenceState()
    assert state.register_result("old-1", tool_name="sensor") == "S1"
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
        agent, messages=messages, tools=state.tool_definitions(),
        routes={}, enabled_server_names=None, max_tool_calls=5,
        phase="main",
    ))
    assert answer == "Final answer"
    assert len(model.seen) == 2
    assert [m.get("tool_call_id") for m in model.seen[1] if m.get("role") == "tool"] == ["internal-1"]
    assert "fact-1" in str(model.seen[1])
    assert "S1" in str(model.seen[1])
    internal = [
        c for m in messages for c in m.get("tool_calls", [])
        if c.get("function", {}).get("name") == ADD_EVIDENCE_TOOL_NAME
    ]
    assert len(internal) == 1
    assert internal[0]["function"]["arguments"] == {}
    assert any(
        m.get("tool_call_id") == internal[0]["id"]
        and "erfolgreich aktualisiert" in m.get("content", "")
        for m in messages
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
    assert names == {"sensor__get_value", "agent__update_plan", "agent__add_evidence", "agent__update_evidence", "agent__publish_artifact"}



def test_idless_ollama_tool_calls_support_evidence_compaction():
    import asyncio
    from types import SimpleNamespace
    from cli_agent.agent_loop import run_model_loop
    from cli_agent.config import LoggingConfig
    from cli_agent.evidence_tools import ADD_EVIDENCE_TOOL_NAME

    class Model:
        def __init__(self):
            self.step = 0
            self.saw_ids = []

        async def chat(self, *, messages, tools):
            self.step += 1
            if self.step == 1:
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": "sensor__read", "arguments": {}}}
                ]}
            if self.step == 2:
                first = next(m for m in messages if m.get("role") == "assistant"
                             and m.get("tool_calls"))
                call_id = first["tool_calls"][0]["id"]
                self.saw_ids.append(call_id)
                assert next(m for m in messages if m.get("role") == "tool")[
                    "tool_call_id"] == call_id
                assert state.tool_results[call_id]["source_ref"] == "S1"
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"function": {"name": ADD_EVIDENCE_TOOL_NAME, "arguments": {
                        "id": "reading", "type": "observation",
                        "statement": "Sensor measured 100W.",
                        "source_refs": ["S1"],
                    }}}
                ]}
            assert [m.get("tool_call_id") for m in messages if m.get("role") == "tool"] == [
                next(c["id"] for m in messages if m.get("role") == "assistant"
                     for c in m.get("tool_calls", [])
                     if c["function"]["name"] == ADD_EVIDENCE_TOOL_NAME)
            ]
            assert "reading" in str(messages)
            return {"role": "assistant", "content": "100 W"}

    class Sensor:
        async def call_tool(self, name, args):
            assert name == "read"
            return SimpleNamespace(
                content=[SimpleNamespace(text="100 W")],
                meta=None, isError=False,
            )

    model = Model()
    state = EvidenceState()
    agent = SimpleNamespace(
        _evidence_state=state,
        model_client=model,
        logging_config=LoggingConfig(),
        _agui_frontend_tools={},
        _result_artifacts=None,
        workspace_access="none",
        _dump_context=lambda *a, **k: None,
        _dump_value=lambda *a, **k: None,
        _tool_is_enabled=lambda name: True,
        _tool_input_schema=lambda name, phase: {"type": "object"},
        _requires_approval=lambda *a, **kw: False,
    )
    server = SimpleNamespace(name="sensor", compress_result=False)
    messages = [{"role": "system", "content": "rules"}]
    result = asyncio.run(run_model_loop(
        agent, messages=messages,
        tools=[{"type": "function", "function": {
            "name": "sensor__read", "parameters": {"type": "object"},
        }}, *state.tool_definitions()],
        routes={"sensor__read": (Sensor(), "read", server)},
        enabled_server_names=None,
        max_tool_calls=5, phase="main",
    ))
    assert result == "100 W"
    assert len(state.tool_results) == 1
    assert state.snapshot()["compacted_tool_call_ids"] == model.saw_ids
    assert messages[1]["tool_calls"][0]["id"] == model.saw_ids[0]


def test_evidence_updates_use_regular_tool_activity_events():
    import asyncio
    from types import SimpleNamespace
    from cli_agent.agent_loop import run_model_loop
    from cli_agent.config import LoggingConfig
    from cli_agent.evidence_tools import ADD_EVIDENCE_TOOL_NAME, UPDATE_EVIDENCE_TOOL_NAME

    events = []

    async def event_callback(kind, payload):
        events.append((kind, dict(payload)))

    class Model:
        def __init__(self):
            self.step = 0

        async def chat(self, *, messages, tools):
            self.step += 1
            if self.step == 1:
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "evidence-success", "type": "function",
                     "function": {"name": ADD_EVIDENCE_TOOL_NAME, "arguments": {
                         "id": "a", "type": "observation",
                         "statement": "Observed value.",
                         "source_refs": ["S1"],
                     }}}
                ]}
            if self.step == 2:
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "evidence-error", "type": "function",
                     "function": {"name": UPDATE_EVIDENCE_TOOL_NAME,
                                  "arguments": {"id": "missing", "remove": True}}}
                ]}
            return {"role": "assistant", "content": "done"}

    evidence = EvidenceState()
    evidence.register_result("source-1", tool_name="sensor")
    agent = SimpleNamespace(
        model_client=Model(),
        _evidence_state=evidence,
        _agui_frontend_tools={},
        logging_config=LoggingConfig(),
        event_callback=event_callback,
        _dump_context=lambda *a, **k: None,
        _dump_value=lambda *a, **k: None,
    )
    messages = [{"role": "system", "content": "rules"}]
    result = asyncio.run(run_model_loop(
        agent, messages=messages, tools=evidence.tool_definitions(), routes={},
        enabled_server_names=None, max_tool_calls=5, phase="main",
    ))
    assert result == "done"
    assert events == [
        ("tool_started", {"call_id": "evidence-success", "tool": ADD_EVIDENCE_TOOL_NAME}),
        ("tool_finished", {"call_id": "evidence-success", "tool": ADD_EVIDENCE_TOOL_NAME,
                           "status": "success"}),
        ("tool_started", {"call_id": "evidence-error", "tool": UPDATE_EVIDENCE_TOOL_NAME}),
        ("tool_finished", {"call_id": "evidence-error", "tool": UPDATE_EVIDENCE_TOOL_NAME,
                           "status": "error"}),
    ]
    internal = [
        call for msg in messages for call in msg.get("tool_calls", [])
        if call.get("function", {}).get("name") in {
            ADD_EVIDENCE_TOOL_NAME, UPDATE_EVIDENCE_TOOL_NAME,
        }
    ]
    assert len(internal) == 2
    assert all(call["function"]["arguments"] == {} for call in internal)
    assert evidence.evidences["a"]["statement"] == "Observed value."
    assert any(
        m.get("tool_call_id") == "evidence-error"
        and "NICHT übernommen" in m.get("content", "")
        for m in messages
    )


def test_evidence_requires_at_least_one_verified_source():
    state = EvidenceState()
    state.register_result("known-1", tool_name="measure")
    entry = _entry("known-1")
    entry["tool_calls"] = []
    with pytest.raises(EvidenceError, match="mindestens eine"):
        state.update({"evidences": [entry]})
    assert not state.evidences
    assert EVIDENCE_TOOL_NAME == "agent__update_evidence"

    schema = __import__(
        "cli_agent.evidence_state", fromlist=["EVIDENCE_TOOL"]
    ).EVIDENCE_TOOL["function"]["parameters"]
    assert schema["properties"]["evidences"]["items"]["properties"][
        "tool_calls"]["minItems"] == 1

    bad = _entry("unknown-1")
    with pytest.raises(EvidenceError, match="known-1"):
        state.update({"evidences": [bad]})
    assert not state.evidences

    state.update({"evidences": [_entry("known-1")]})
    assert state.snapshot()["compacted_tool_call_ids"] == ["known-1"]
    # Removing evidence or explicitly discarding calls requires no new evidence.
    state.update({"remove_evidence_ids": ["e0"]})
    assert state.snapshot()["evidences"] == []



def test_evidence_projection_keeps_sanitized_tool_completion():
    state = EvidenceState()
    state.register_result("source", tool_name="sensor")
    state.update({"evidences": [_entry("source")]})
    history = [
        {"role": "system", "content": "rules"},
        {"role": "assistant", "content": "Saving findings", "tool_calls": [
            {"id": "ev-1", "type": "function", "function": {
                "name": EVIDENCE_TOOL_NAME, "arguments": {}
            }}
        ]},
        {"role": "tool", "tool_call_id": "ev-1",
         "content": "Evidence-State erfolgreich aktualisiert."},
    ]
    visible = state.project(history)
    assert visible[-3]["tool_calls"][0]["function"]["arguments"] == {}
    assert visible[-2]["tool_call_id"] == "ev-1"
    assert "erfolgreich" in visible[-2]["content"]
    assert visible[-1]["role"] == "user"
    assert "[cli-agent evidence state:" in visible[-1]["content"]



def _plan():
    return {
        "goal": "Determine the device and electrical phase",
        "steps": [
            {"id": "identify", "description": "Identify active day",
             "status": "in_progress"},
            {"id": "measure", "description": "Compare phase signals",
             "status": "pending"},
        ],
    }


def test_plan_can_be_created_without_tools_and_is_projected():
    state = EvidenceState()
    state.update({"plan": _plan()})
    assert state.snapshot()["plan"] == _plan()
    assert state.snapshot()["evidences"] == []
    assert "Identify active day" in str(state.project(_history()))
    assert state.compacted == set()
    # Plan only, no completed tool sources required.
    assert state.tool_results == {}


def test_evidence_links_to_plan_and_status_can_progress():
    state = EvidenceState()
    state.update({"plan": _plan()})
    state.register_result("call-1", tool_name="sensor__read")
    linked = {**_entry(), "plan_step_id": "identify"}
    state.update({"evidences": [linked]})
    assert state.snapshot()["evidences"][0]["plan_step_id"] == "identify"
    revised = _plan()
    revised["steps"][0]["status"] = "completed"
    revised["steps"][1]["status"] = "in_progress"
    state.update({"plan": revised})
    assert state.snapshot()["plan"] == revised
    assert len(state.snapshot()["evidences"]) == 1
    assert state.snapshot()["compacted_tool_call_ids"] == ["call-1"]


def test_invalid_plan_or_unlinked_evidence_rejected_atomically():
    state = EvidenceState()
    with pytest.raises(EvidenceError, match="Doppelte Planschritt-ID"):
        invalid = _plan()
        invalid["steps"][1]["id"] = "identify"
        state.update({"plan": invalid})
    assert "plan" not in state.snapshot()
    with pytest.raises(EvidenceError, match="Status"):
        invalid = _plan()
        invalid["steps"][0]["status"] = "unknown"
        state.update({"plan": invalid})
    state.register_result("call-1", tool_name="sensor")
    state.update({"plan": _plan()})
    before = state.snapshot()
    with pytest.raises(EvidenceError, match="plan_step_id"):
        state.update({"evidences": [_entry()]})
    with pytest.raises(EvidenceError, match="plan_step_id"):
        state.update({"evidences": [{**_entry(), "plan_step_id": "unknown"}]})
    assert state.snapshot() == before


def test_plan_cannot_orphan_existing_evidence():
    state = EvidenceState()
    state.register_result("call-1", tool_name="sensor")
    state.update({"plan": _plan(), "evidences": [
        {**_entry(), "plan_step_id": "identify"}
    ]})
    before = state.snapshot()
    new_plan = {
        "goal": "Focus on other analysis",
        "steps": [{"id": "measure", "description": "Measure phase",
                   "status": "in_progress"}],
    }
    with pytest.raises(EvidenceError, match="keinem gültigen Planschritt"):
        state.update({"plan": new_plan})
    assert state.snapshot() == before
    state.update({"plan": new_plan, "remove_evidence_ids": ["e0"]})
    assert state.snapshot()["plan"] == new_plan
    assert state.snapshot()["evidences"] == []


def test_plan_is_bounded_and_does_not_force_trivial_tasks_to_plan():
    state = EvidenceState()
    state.register_result("call-1", tool_name="sensor")
    state.update({"evidences": [_entry()]})
    assert "plan" not in state.snapshot()
    with pytest.raises(EvidenceError, match="zwischen 1 und 16"):
        state.update({"plan": {"goal": "many", "steps": [
            {"id": f"s{i}", "description": "Analyse", "status": "pending"}
            for i in range(17)
        ]}})
    assert "plan" not in state.snapshot()
    with pytest.raises(EvidenceError, match="ohne Plan"):
        state.update({"evidences": [{**_entry(), "plan_step_id": "identify"}]})


def test_partial_plan_status_update_and_atomic_failures():
    state = EvidenceState()
    state.update({"plan": _plan()})
    state.update({"plan_step_updates": [{"id": "identify", "status": "completed"}]})
    assert state.plan["steps"][0]["status"] == "completed"
    assert state.plan["steps"][1]["status"] == "pending"
    before = state.snapshot()
    with pytest.raises(EvidenceError, match="Unbekannte Planschritt-ID"):
        state.update({"plan_step_updates": [{"id": "missing", "status": "skipped"}]})
    assert state.snapshot() == before
    with pytest.raises(EvidenceError, match="nicht gleichzeitig"):
        state.update({"plan": _plan(), "plan_step_updates": [
            {"id": "identify", "status": "completed"}
        ]})
    assert state.snapshot() == before


def test_tool_definition_enumerates_only_completed_sources():
    state = EvidenceState()
    assert "enum" not in state.tool_definition()["function"]["parameters"][
        "properties"]["evidences"]["items"]["properties"]["tool_calls"][
        "items"]["properties"]["call_id"]
    for i in range(4):
        state.register_result(f"call-{i}", tool_name="sensor")
    schema = state.tool_definition()["function"]["parameters"]
    source = schema["properties"]["evidences"]["items"]["properties"][
        "tool_calls"]["items"]["properties"]["call_id"]
    assert source["enum"] == [f"call-{i}" for i in range(4)]
    assert "enum" not in EVIDENCE_TOOL["function"]["parameters"][
        "properties"]["evidences"]["items"]["properties"]["tool_calls"][
        "items"]["properties"]["call_id"]
    state.update({"evidences": [_entry("call-0")]})
    for i in range(4, 110):
        state.register_result(f"call-{i}", tool_name="sensor")
    offered = state.tool_definition()["function"]["parameters"]["properties"][
        "evidences"]["items"]["properties"]["tool_calls"]["items"][
        "properties"]["call_id"]["enum"]
    assert "call-0" in offered
    assert "call-109" in offered
    assert "call-1" not in offered
    assert len(offered) <= 97


def test_bounded_diagnostic_arguments_are_not_part_of_state_projection():
    state = EvidenceState()
    state.record_debug_update(
        call_id="ev-1", arguments={"plan": _plan()},
        status="error", error="Invalid update",
    )
    assert state.debug_updates[0]["call_id"] == "ev-1"
    assert "identify" in state.debug_updates[0]["arguments"]
    assert "debug_updates" not in state.snapshot()
    assert "Invalid update" not in str(state.project(_history()))


def test_evidence_update_reports_unknown_fields_and_empty_arguments():
    state = EvidenceState()
    with pytest.raises(EvidenceError, match="Unbekannte Evidence-Felder") as error:
        state.update({"plan_steps": [], "evidences": []})
    assert "plan_steps" in str(error.value)
    assert "plan_step_updates" in str(error.value)

    with pytest.raises(EvidenceError, match="leeres JSON-Objekt") as error:
        state.update({})
    assert '"plan"' in str(error.value)
    assert '"goal"' in str(error.value)
    assert state.snapshot()["evidences"] == []


def test_compaction_ids_are_excluded_from_model_projection_but_in_debug_snapshot():
    import json

    state = EvidenceState()
    state.register_result("call-1", tool_name="sensor")
    state.register_result("call-2", tool_name="sensor")
    state.update({"evidences": [_entry()], "discard_tool_call_ids": ["call-2"]})

    model_state = state.model_snapshot()
    assert set(model_state) == {"evidences"}
    assert "compacted_tool_call_ids" not in model_state
    assert state.snapshot()["compacted_tool_call_ids"] == ["call-1", "call-2"]

    projection = state.project(_history())
    note = next(m["content"] for m in projection
                if "[cli-agent evidence state:" in str(m.get("content")))
    payload = json.loads(note.split("\n", 1)[1])
    assert payload == model_state
    assert "compacted_tool_call_ids" not in note
    assert all(m["role"] != "tool" for m in projection)
    assert state.snapshot()["compacted_tool_call_ids"] == ["call-1", "call-2"]

    state.update({"restore_tool_call_ids": ["call-2"]})
    assert [m["tool_call_id"] for m in state.project(_history())
            if m["role"] == "tool"] == ["call-2"]


def test_discard_only_keeps_no_management_note_in_model_context():
    state = EvidenceState()
    state.register_result("call-1", tool_name="sensor")
    state.update({"discard_tool_call_ids": ["call-1"]})
    assert state.model_snapshot() == {"evidences": []}
    assert state.snapshot()["compacted_tool_call_ids"] == ["call-1"]
    projected = state.project(_history())
    assert not any("[cli-agent evidence state:" in str(m.get("content"))
                   for m in projected)
    assert all(m.get("tool_call_id") != "call-1" for m in projected)
