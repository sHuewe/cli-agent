"""Regression tests for tool-free, atomic session evidence cleanup."""
import asyncio
import copy
import json
from types import SimpleNamespace

import pytest

from cli_agent.state.evidence_state import EvidenceState
from cli_agent.state.evidence_cleanup import (
    apply_cleanup_delta, maybe_cleanup_evidences, note_plan_update,
    note_tool_calls, reset_cleanup_tracking,
)


def evidence_state(count=4):
    state = EvidenceState()
    state.tool_results = {
        f"call-{i}": {"source_ref": f"S{i+1}", "tool_name": "sensor__read"}
        for i in range(count)
    }
    state.evidences = {
        f"e{i}": {
            "id": f"e{i}",
            "type": "observation",
            "statement": f"Measured temperature {i}; keep scope and uncertainty",
            "tool_calls": [{"call_id": f"call-{i}", "source_ref": f"S{i+1}",
                            "tool_name": "sensor__read", "description": "measurement"}],
        }
        for i in range(count)
    }
    return state


def test_batch_removal_is_atomic_and_refreshes_state():
    s = evidence_state()
    with pytest.raises(ValueError):
        s.remove_evidence({"evidence_ids": ["e0", "missing"]})
    assert len(s.evidences) == 4
    assert s.remove_evidence({"evidence_ids": ["e0", "e1"]}) == {
        "removed": ["e0", "e1"], "remaining": 2
    }
    assert set(s.evidences) == {"e2", "e3"}


def test_delta_merge_unions_trusted_sources():
    s = evidence_state()
    delta = {
        "remove": [{"id": "e3", "reason": "duplicate"}],
        "update": [{"id": "e2", "statement": "Concise equivalent conclusion"}],
        "merge": [{"target_id": "e0", "source_ids": ["e1"],
                   "statement": "Combined measured temperatures 0 and 1"}],
    }
    assert apply_cleanup_delta(s, delta)
    assert set(s.evidences) == {"e0", "e2"}
    assert s.evidences["e0"]["statement"] == "Combined measured temperatures 0 and 1"
    assert [x["source_ref"] for x in s.evidences["e0"]["tool_calls"]] == ["S1", "S2"]


@pytest.mark.parametrize("delta", [
    {"remove": [], "update": [], "merge": [] , "unexpected": []},
    {"remove": [{"id": "e0", "reason": "duplicate"}],
     "update": [{"id": "e0", "statement": "new"}], "merge": []},
    {"remove": [], "update": [], "merge": [
        {"target_id": "e0", "source_ids": ["e1", "e1"], "statement": "same"}]},
    {"remove": [], "update": [], "merge": [
        {"target_id": "e0", "source_ids": ["not-found"], "statement": "same"}]},
    {"remove": [], "update": [{"id": "e0", "statement": ""}], "merge": []},
])
def test_invalid_delta_never_mutates_state(delta):
    s = evidence_state()
    before = copy.deepcopy(s.evidences)
    with pytest.raises((ValueError, KeyError)):
        apply_cleanup_delta(s, delta)
    assert s.evidences == before


def test_plan_trigger_ignores_creation_and_progress():
    agent = SimpleNamespace(_cleanup_plan_dirty=False)
    initial = {"goal": "compare", "steps": [
        {"id": "a", "status": "pending", "description": "first"}]}
    note_plan_update(agent, None, initial)
    assert not agent._cleanup_plan_dirty
    progress = copy.deepcopy(initial)
    progress["steps"][0]["status"] = "in_progress"
    note_plan_update(agent, initial, progress)
    assert not agent._cleanup_plan_dirty
    completed = copy.deepcopy(progress)
    completed["steps"][0]["status"] = "completed"
    note_plan_update(agent, progress, completed)
    assert agent._cleanup_plan_dirty


def test_tool_call_counter_excludes_internal_and_counts_sub_work():
    agent = SimpleNamespace()
    note_tool_calls(agent, [{"function": {"name": "sensor__read"}},
                            {"function": {"name": "agent__add_evidence"}},
                            {"function": {"name": "agent__start_sub_run"}},
                            {"function": {"name": "sensor__read"}}])
    assert agent._cleanup_tool_call_count == 2


def test_single_shot_without_tools_or_model_visible_result(monkeypatch):
    import cli_agent.state.evidence_cleanup as cleanup
    monkeypatch.setattr(cleanup, "MIN_EVIDENCE_BYTES", 1)
    recorded = []

    class Model:
        async def chat(self, messages, tools):
            recorded.append((copy.deepcopy(messages), tools))
            return {"content": json.dumps({
                "remove": [{"id": "e3", "reason": "duplicate"}],
                "update": [], "merge": []
            })}

    agent = SimpleNamespace(_evidence_state=evidence_state(), model_client=Model())
    reset_cleanup_tracking(agent, "How warm was it?")
    agent._evidence_state.evidences["e0"]["statement"] += " with new evidence"
    note_tool_calls(agent, [{"function": {"name": "sensor__read"}}] * 20)
    asyncio.run(maybe_cleanup_evidences(agent))
    assert len(recorded) == 1
    assert recorded[0][1] == []
    assert len(recorded[0][0]) == 2
    assert "e3" not in agent._evidence_state.evidences
    asyncio.run(maybe_cleanup_evidences(agent))
    assert len(recorded) == 1


def test_invalid_llm_output_is_ignored_and_not_retried(monkeypatch):
    import cli_agent.state.evidence_cleanup as cleanup
    monkeypatch.setattr(cleanup, "MIN_EVIDENCE_BYTES", 1)
    calls = []

    class Model:
        async def chat(self, messages, tools):
            calls.append(1)
            return {"content": "not JSON"}

    agent = SimpleNamespace(_evidence_state=evidence_state(), model_client=Model())
    reset_cleanup_tracking(agent, "Analyse")
    agent._cleanup_plan_dirty = True
    agent._evidence_state.evidences["e0"]["statement"] = "New finding"
    before = copy.deepcopy(agent._evidence_state.evidences)
    asyncio.run(maybe_cleanup_evidences(agent))
    assert agent._evidence_state.evidences == before
    asyncio.run(maybe_cleanup_evidences(agent))
    assert len(calls) == 1

def test_cleanup_diagnostics_preserve_exact_model_messages_and_processing(monkeypatch):
    import cli_agent.state.evidence_cleanup as cleanup
    monkeypatch.setattr(cleanup, "MIN_EVIDENCE_BYTES", 1)
    actual = []

    class Model:
        async def chat(self, messages, tools):
            actual.append(copy.deepcopy(messages))
            assert tools == []
            return {"role": "assistant", "content": json.dumps({
                "remove": [{"id": "e3", "reason": "duplicate"}],
                "update": [{"id": "e0", "statement": "New concise finding"}],
                "merge": [],
            })}

    agent = SimpleNamespace(_evidence_state=evidence_state(), model_client=Model())
    reset_cleanup_tracking(agent, "How warm?")
    agent._evidence_state.evidences["e0"]["statement"] += " revised"
    agent._cleanup_plan_dirty = True
    asyncio.run(maybe_cleanup_evidences(agent))
    assert len(agent._cleanup_runs) == 1
    run = agent._cleanup_runs[0]
    assert run["id"] == "cleanup-1"
    assert run["trigger"] == "plan_changed"
    assert run["messages"][:2] == actual[0]
    assert run["messages"][2]["role"] == "assistant"
    assert run["processing_result"]["status"] == "applied"
    assert run["processing_result"]["removed"] == 1
    assert run["processing_result"]["updated"] == 1
    assert run["processing_result"]["evidences_before"] == 4
    assert run["processing_result"]["evidences_after"] == 3


def test_cleanup_diagnostics_invalid_output_and_reset(monkeypatch):
    import cli_agent.state.evidence_cleanup as cleanup
    monkeypatch.setattr(cleanup, "MIN_EVIDENCE_BYTES", 1)

    class Model:
        async def chat(self, messages, tools):
            return {"role": "assistant", "content": "not JSON"}

    agent = SimpleNamespace(_evidence_state=evidence_state(), model_client=Model())
    reset_cleanup_tracking(agent, "Analyse")
    agent._evidence_state.evidences["e0"]["statement"] = "Revised"
    agent._cleanup_plan_dirty = True
    before = copy.deepcopy(agent._evidence_state.evidences)
    asyncio.run(maybe_cleanup_evidences(agent))
    run = agent._cleanup_runs[0]
    assert run["processing_result"]["status"] == "invalid"
    assert run["messages"][-1] == {"role": "assistant", "content": "not JSON"}
    assert agent._evidence_state.evidences == before
    reset_cleanup_tracking(agent, "Next")
    assert agent._cleanup_runs == []
    assert agent._cleanup_run_sequence == 0


def test_cancelled_cleanup_records_terminal_status_and_propagates(monkeypatch):
    import cli_agent.state.evidence_cleanup as cleanup
    monkeypatch.setattr(cleanup, "MIN_EVIDENCE_BYTES", 1)

    class Model:
        async def chat(self, messages, tools):
            raise asyncio.CancelledError()

    agent = SimpleNamespace(_evidence_state=evidence_state(), model_client=Model())
    reset_cleanup_tracking(agent, "Analyse")
    agent._evidence_state.evidences["e0"]["statement"] = "Revised"
    agent._cleanup_plan_dirty = True
    before = copy.deepcopy(agent._evidence_state.evidences)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(maybe_cleanup_evidences(agent))
    assert agent._cleanup_runs[0]["processing_result"]["status"] == "cancelled"
    assert agent._cleanup_runs[0]["processing_result"]["evidences_before"] == 4
    assert agent._cleanup_runs[0]["processing_result"]["evidences_after"] == 4
    assert agent._evidence_state.evidences == before
    assert len(agent._cleanup_runs[0]["messages"]) == 2

def test_cleanup_cancellation_records_terminal_status_and_propagates(monkeypatch):
    import cli_agent.state.evidence_cleanup as cleanup
    monkeypatch.setattr(cleanup, "MIN_EVIDENCE_BYTES", 1)

    class Model:
        async def chat(self, messages, tools):
            raise asyncio.CancelledError()

    agent = SimpleNamespace(_evidence_state=evidence_state(), model_client=Model())
    reset_cleanup_tracking(agent, "Analyse")
    agent._cleanup_plan_dirty = True
    agent._evidence_state.evidences["e0"]["statement"] += " revised"
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(maybe_cleanup_evidences(agent))
    result = agent._cleanup_runs[0]["processing_result"]
    assert result["status"] == "cancelled"
    assert result["evidences_before"] == result["evidences_after"]

def test_budget_uses_model_visible_state_not_source_metadata():
    state = evidence_state()
    for item in state.evidences.values():
        item["tool_calls"][0]["workspace_path"] = "x" * 15000
        item["tool_calls"][0]["record_sets"] = [{"large": "y" * 12000}]
    # Provenance stays intact internally but is excluded from the 32-kB LLM
    # budget. This was blocking otherwise valid sub-run evidence promotion.
    state._check_budget(state.evidences)
    from cli_agent.state.evidence_tools import visible_evidence_state
    assert state.model_snapshot() == visible_evidence_state(state.evidences, state.plan)
    assert len(json.dumps(state.model_snapshot())) < 32000


def test_cleanup_prompt_requests_updates_for_verbose_unique_evidence():
    from cli_agent.state.evidence_cleanup import CLEANUP_SYSTEM_PROMPT
    assert "even if no exact duplicates" in CLEANUP_SYSTEM_PROMPT
    assert '"update"' in CLEANUP_SYSTEM_PROMPT
    assert "material" in CLEANUP_SYSTEM_PROMPT.lower()


def test_conversation_history_for_cleanup_keeps_only_dialogue():
    from cli_agent.state.evidence_cleanup import conversation_history_for_cleanup
    history = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Earlier topic"},
        {"role": "assistant", "content": "Old result"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t"}]},
        {"role": "tool", "content": "raw", "tool_call_id": "t"},
        {"role": "user", "content": "[cli-agent evidence state: injected]"},
        {"role": "user", "content": "Externer Referenzkontext für die nachfolgende Benutzeranfrage."},
    ]
    assert conversation_history_for_cleanup(history, "New topic") == [
        {"role": "user", "content": "Earlier topic"},
        {"role": "assistant", "content": "Old result"},
        {"role": "user", "content": "New topic"},
    ]


def test_cleanup_receives_history_and_runs_at_ten_calls(monkeypatch):
    from cli_agent.state import evidence_cleanup as cleanup
    monkeypatch.setattr(cleanup, "MIN_EVIDENCE_BYTES", 1)
    calls = []

    class Model:
        async def chat(self, messages, tools):
            calls.append(json.loads(messages[1]["content"]))
            return {"content": '{"remove":[],"update":[],"merge":[]}'}

    agent = SimpleNamespace(_evidence_state=evidence_state(), model_client=Model(),
                            history=[{"role": "user", "content": "Previously studied X"},
                                     {"role": "assistant", "content": "Finding about X"}])
    reset_cleanup_tracking(agent, "Now study Y")
    agent._evidence_state.evidences["e0"]["statement"] += " revised"
    note_tool_calls(agent, [{"function": {"name": "sensor__read"}}] * 10)
    asyncio.run(maybe_cleanup_evidences(agent))
    assert len(calls) == 1
    assert [x["content"] for x in calls[0]["conversation_history"]] == [
        "Previously studied X", "Finding about X", "Now study Y"]
    assert agent._cleanup_runs[0]["processing_result"]["status"] == "unchanged"


def test_cleanup_size_pressure_trigger(monkeypatch):
    from cli_agent.state import evidence_cleanup as cleanup
    monkeypatch.setattr(cleanup, "MIN_EVIDENCE_BYTES", 1)
    monkeypatch.setattr(cleanup, "CLEANUP_SIZE_PRESSURE_BYTES", 1)
    calls = []

    class Model:
        async def chat(self, messages, tools):
            calls.append(1)
            return {"content": '{"remove":[],"update":[],"merge":[]}'}

    agent = SimpleNamespace(_evidence_state=evidence_state(), model_client=Model())
    reset_cleanup_tracking(agent, "Investigation")
    agent._evidence_state.evidences["e0"]["statement"] += " revised"
    asyncio.run(maybe_cleanup_evidences(agent))
    assert calls == [1]
    assert agent._cleanup_runs[0]["trigger"] == "size_pressure"
    asyncio.run(maybe_cleanup_evidences(agent))
    assert calls == [1]
