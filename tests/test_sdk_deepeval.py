"""Optional DeepEval SDK target integration; tests do not require DeepEval."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import sys
from types import ModuleType, SimpleNamespace

import pytest

from cli_agent.sdk import AgentBuilder
from cli_agent.sdk.deepeval_adapter import DeepEvalTarget


@dataclass(frozen=True)
class Hit:
    content: str | None


@dataclass(frozen=True)
class Trace:
    selected: tuple[Hit, ...]
    truncated: bool = False
    content_included: bool = True


class FakeAgent:
    def __init__(self, calls):
        self.calls = calls
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def ask(self, prompt):
        self.calls.append(prompt)
        return SimpleNamespace(
            answer="Gateway rejects missing tokens",
            knowledge_trace=Trace(selected=(Hit(content="Observed passage"),)),
        )


@pytest.fixture()
def deepeval_trace(monkeypatch):
    records = []
    observed = []
    fake = ModuleType("deepeval")
    tracing = ModuleType("deepeval.tracing")

    def observe(**kwargs):
        observed.append(kwargs)
        def decorator(function):
            async def wrapper(*args, **kw):
                return await function(*args, **kw)
            return wrapper
        return decorator

    tracing.observe = observe
    tracing.update_current_trace = lambda **kw: records.append(kw)
    monkeypatch.setitem(sys.modules, "deepeval", fake)
    monkeypatch.setitem(sys.modules, "deepeval.tracing", tracing)
    return observed, records


def test_only_question_reaches_sdk_and_reference_is_trace_only(deepeval_trace):
    observed, records = deepeval_trace
    calls, created = [], []
    def create():
        agent = FakeAgent(calls)
        created.append(agent)
        return agent

    target = DeepEvalTarget(create)
    response = asyncio.run(target.ask("What happens?", expected_output="Reject"))
    assert calls == ["What happens?"]
    assert created[0].closed
    assert response["actual_output"] == "Gateway rejects missing tokens"
    assert response["knowledge_trace"]["selected"][0]["content"] == "Observed passage"
    assert observed == [{"type": "agent"}]
    assert records[0]["input"] == "What happens?"
    assert records[0]["output"] == response["actual_output"]
    assert records[0]["expected_output"] == "Reject"
    assert records[0]["retrieval_context"] == ["Observed passage"]
    assert records[0]["metadata"]["cli_agent_response_cached"] is False


def test_replay_uses_deepeval_tracing_without_agent_call(deepeval_trace):
    _, records = deepeval_trace
    target = DeepEvalTarget(lambda: (_ for _ in ()).throw(AssertionError("no agent")))
    response = asyncio.run(target.ask(
        "Q", expected_output="R",
        cached_response={
            "actual_output": "A",
            "knowledge_trace": {
                "truncated": False, "content_included": True,
                "selected": [{"content": "Observed chunk"}],
            },
        },
    ))
    assert response["actual_output"] == "A"
    assert records[0]["metadata"]["cli_agent_response_cached"]
    assert records[0]["retrieval_context"] == ["Observed chunk"]


def test_native_sdk_tuple_trace_replays_without_json_roundtrip(deepeval_trace):
    calls = []
    target = DeepEvalTarget(lambda: FakeAgent(calls))
    first = asyncio.run(target.ask("Q", expected_output="R"))
    assert isinstance(first["knowledge_trace"]["selected"], tuple)

    # This is exactly what callers receive after a live SDK question.
    replay = DeepEvalTarget(
        lambda: (_ for _ in ()).throw(AssertionError("no second agent"))
    )
    second = asyncio.run(replay.ask(
        "Q", expected_output="R", cached_response=first
    ))
    assert second == first
    assert calls == ["Q"]


def test_absent_or_truncated_context_never_fabricates_sources(deepeval_trace):
    _, records = deepeval_trace
    target = DeepEvalTarget(lambda: None)
    response = {
        "actual_output": "A",
        "knowledge_trace": {
            "selected": [{"content": "Observed"}],
            "content_included": True,
            "truncated": True,
        },
    }
    asyncio.run(target.ask("Q", expected_output="R", cached_response=response))
    assert "retrieval_context" not in records[0]
    response["knowledge_trace"]["truncated"] = False
    response["knowledge_trace"]["selected"] = [{"content": None}]
    asyncio.run(target.ask("Q", expected_output="R", cached_response=response))
    assert "retrieval_context" not in records[1]


def test_invalid_reference_and_replay_are_rejected(deepeval_trace):
    target = DeepEvalTarget(lambda: None)
    with pytest.raises(ValueError, match="expected_output"):
        asyncio.run(target.ask("Q", expected_output=""))
    with pytest.raises(ValueError, match="actual_output"):
        asyncio.run(target.ask("Q", expected_output="R",
            cached_response={"actual_output": ""}))
    with pytest.raises(ValueError, match="selected list"):
        asyncio.run(target.ask("Q", expected_output="R",
            cached_response={"actual_output": "A", "knowledge_trace": {}}))


def test_sdk_builder_target_is_optional_and_does_not_mutate_builder():
    builder = AgentBuilder()
    target = builder.build_deepeval_target(include_knowledge_content=True)
    assert isinstance(target, DeepEvalTarget)
    assert builder.build()._knowledge_trace_include_content is None
    assert target._create_agent()._knowledge_trace_include_content is True
    with pytest.raises(TypeError, match="bool"):
        builder.build_deepeval_target(include_knowledge_content=1)
