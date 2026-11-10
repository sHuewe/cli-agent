"""Knowledge SDK trace opt-in: data isolation, provenance, bounds and no behavior changes."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cli_agent.runtime.knowledge_trace import (
    KnowledgeTraceCollector, MAX_TRACE_CONTENT_CHARS, MAX_TRACE_HITS,
    trace_observe,
)
from cli_agent.sdk import AgentBuilder


def test_collector_search_selection_and_reentry():
    trace = KnowledgeTraceCollector(include_content=True)
    trace.begin("initial")
    trace.search(backend="chroma", stage="initial", result={
        "query": "token policy",
        "entries": [
            {"id": "a-1", "source": "policy-a.md", "content": "Tokens expire.",
             "distance": 0.15, "metadata": {"section": 4}},
            {"id": "b-2", "source": "policy-b.md", "content": "Missing token rejected.",
             "distance": 0.22},
        ],
    })
    trace.selection(backend="chroma", payload={
        "content": [{"id": "a-1", "source": "policy-a.md",
                     "content": "Tokens expire.", "distance": 0.15}]
    })
    trace.finish(result='{"found_content": true}')
    trace.begin("reentry")
    trace.search(backend="chroma", stage="followup", result={
        "query": "missing token", "entries": [
            {"id": "b-2", "source": "policy-b.md",
             "content": "Missing token rejected.", "distance": 0.12},
        ],
    })
    trace.selection(backend="chroma", payload={
        "content": [{"id": "b-2", "source": "policy-b.md",
                     "content": "Missing token rejected.", "distance": 0.12}],
    })
    trace.finish(result='{"found_content": true}')
    result = trace.snapshot()
    assert [s.stage for s in result.searches] == ["initial", "followup"]
    assert [s.hits[0].rank for s in result.searches] == [1, 1]
    assert [x.source for x in result.selected] == ["policy-a.md", "policy-b.md"]
    assert [c.origin for c in result.calls] == ["initial", "reentry"]
    assert all(c.duration_ms >= 0 for c in result.calls)
    assert result.complete_selected_content
    assert result.searches[0].hits[0].metadata == {"section": 4}


def test_metadata_only_default_and_bounded_content():
    collector = KnowledgeTraceCollector()
    collector.search(backend="chroma", stage="initial",
                     result={"entries": [{"id": "id", "source": "a.md",
                                          "content": "secret content"}]})
    collector.selection(backend="chroma",
                        payload={"content": [{"id": "id", "source": "a.md",
                                              "content": "secret content"}]})
    assert collector.snapshot().selected[0].content is None
    assert "secret content" not in repr(collector.snapshot())
    assert not collector.snapshot().truncated

    bounded = KnowledgeTraceCollector(include_content=True)
    bounded.selection(backend="okf", payload={"content": [
        {"concept": "a.md", "content": "x" * (MAX_TRACE_CONTENT_CHARS + 1)}
    ]})
    assert bounded.snapshot().truncated
    assert bounded.snapshot().selected[0].content is None
    assert not bounded.snapshot().complete_selected_content

    limited = KnowledgeTraceCollector()
    limited.search(backend="chroma", stage="initial",
                   result={"entries": [{"id": str(i)} for i in range(MAX_TRACE_HITS + 2)]})
    assert limited.snapshot().truncated
    assert len(limited.snapshot().searches[0].hits) == MAX_TRACE_HITS


def test_okf_verifiable_read_and_selected_payload():
    collector = KnowledgeTraceCollector(include_content=True)
    collector.read({"path": "topic/a.md", "kind": "concept",
                    "content": "# Authentication\nTokens expire."})
    collector.selection(backend="okf", payload={
        "content": [{"concept": "topic/a.md", "content": "# Authentication\nTokens expire."}]
    })
    snap = collector.snapshot()
    assert snap.searches[0].stage == "read"
    assert snap.searches[0].hits[0].source == "topic/a.md"
    assert snap.selected[0].source == "topic/a.md"
    assert snap.selected[0].content == "# Authentication\nTokens expire."


def test_trace_failures_dont_interrupt_agent():
    class BrokenCollector:
        truncated = False

        def search(self, **_kwargs):
            raise ValueError("Simulated telemetry failure")

    class Agent:
        _sdk_knowledge_trace_collector = BrokenCollector()

    agent = Agent()
    trace_observe(agent, "search", backend="chroma", stage="initial", result={})
    # Telemetry is fail-open and exposes that its result is incomplete.
    assert agent._sdk_knowledge_trace_collector.truncated is True


def test_builder_opt_in_and_no_option_leak():
    assert AgentBuilder().build()._knowledge_trace_include_content is None
    b = AgentBuilder().with_knowledge_trace()
    assert b.build()._knowledge_trace_include_content is False
    b.with_knowledge_trace(include_content=True)
    assert b.build()._knowledge_trace_include_content is True
    with pytest.raises(TypeError, match="bool"):
        b.with_knowledge_trace(include_content="yes")


def test_sdk_result_has_per_request_trace_without_changing_answer(monkeypatch, tmp_path):
    from cli_agent.sdk.agent import AgentResult

    class Runtime:
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def ask(self, prompt):
            trace_observe(self, "begin", origin="initial")
            trace_observe(self, "search", backend="chroma", stage="initial",
                          result={"query": prompt, "entries": [
                              {"id": prompt, "source": "a.md", "content": "Secret"}
                          ]})
            trace_observe(self, "selection", backend="chroma",
                          payload={"content": [{"id": prompt, "source": "a.md",
                                                "content": "Secret"}]})
            trace_observe(self, "finish", result='{"found_content": true}')
            return "answer-" + prompt

    monkeypatch.setattr("cli_agent.sdk.agent.prepare_agent",
                        lambda *_a, **_kw: SimpleNamespace(agent=Runtime()))

    async def check():
        async with AgentBuilder().with_project_config(workspace=tmp_path).build() as agent:
            ordinary = await agent.ask("no-trace")
            assert ordinary.answer == "answer-no-trace"
            assert ordinary.knowledge_trace is None
        async with (AgentBuilder()
                    .with_project_config(workspace=tmp_path)
                    .with_knowledge_trace(include_content=True)
                    .build()) as agent:
            first = await agent.ask("one")
            second = await agent.ask("two")
            assert first.answer == "answer-one"
            assert first.knowledge_trace is not None
            assert first.knowledge_trace.selected[0].content == "Secret"
            assert first.knowledge_trace.searches[0].hits[0].id == "one"
            assert second.knowledge_trace.searches[0].hits[0].id == "two"
            assert first.knowledge_trace is not second.knowledge_trace

    asyncio.run(check())



def test_explicit_optional_knowledge_outcomes_are_monotonic():
    from cli_agent.runtime.knowledge_trace import KnowledgeTraceCollector
    collector = KnowledgeTraceCollector()
    collector.begin("initial")
    collector.outcome("not_applicable")
    collector.finish(result=None)
    collector.begin("reentry")
    collector.outcome("error")
    collector.finish(result=None)
    assert [call.status for call in collector.snapshot().calls] == [
        "not_applicable", "error",
    ]


def test_followup_chroma_preserves_query_specific_distance():
    from cli_agent.context.agent_knowledge import _KnowledgeRunState, _register_chroma_results

    collector = KnowledgeTraceCollector(include_content=True)
    state = _KnowledgeRunState()
    first = {"query": "first", "entries": [
        {"id": "same-id", "content": "host verified text",
         "source": "doc.md", "metadata": {}, "distance": 0.8},
    ]}
    subsequent = {"query": "second", "entries": [
        {"id": "same-id", "content": "host verified text",
         "source": "doc.md", "metadata": {}, "distance": 0.1},
    ]}
    collector.search(backend="chroma", stage="initial", result=first)
    registered_first = _register_chroma_results(state, first)
    collector.search(backend="chroma", stage="followup", result=subsequent)
    registered_subsequent = _register_chroma_results(state, subsequent)
    # Runtime's tokenized payload intentionally keeps the first snapshot.
    assert registered_first["entries"][0]["distance"] == 0.8
    assert registered_subsequent["entries"][0]["distance"] == 0.8
    # Retrieval trace must keep the REAL distance from the second query.
    assert [s.hits[0].distance for s in collector.snapshot().searches] == [0.8, 0.1]


def test_backend_selection_budget_safely_marks_incomplete_trace():
    collector = KnowledgeTraceCollector(include_content=True)
    collector.search(backend="chroma", stage="followup",
                     result={"query": "search", "entries": [
                         {"id": "chunk", "content": "text", "distance": 0.2},
                     ]})
    collector.mark_truncated()
    assert collector.snapshot().truncated
    assert not collector.snapshot().complete_selected_content
