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
