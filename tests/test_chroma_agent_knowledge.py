"""Integration contracts for the externally managed Chroma Knowledge backend."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cli_agent.settings.config import VectorKnowledgeConfig, load_config
from cli_agent.context.agent_knowledge import (
    _KnowledgeRunState,
    _assemble_chroma_payload,
    _register_chroma_results,
)
from cli_agent.context.chroma_knowledge import ChromaKnowledgeSession
from cli_agent.runtime.agent import CliAgent
from cli_agent.security.network_policy import NetworkConfig


def _config(**overrides) -> VectorKnowledgeConfig:
    return VectorKnowledgeConfig(mode="chroma", database="chroma-dir", **overrides)


def test_chroma_configuration_does_not_require_okf(tmp_path):
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[knowledge.vector]\nbackend = "chroma"\n'
        'database = "./chroma-db"\ncollection = "reports"\n'
        'provider = "ollama"\nmodel = "embeddinggemma"\n'
        'search_limit = 3\nrequired = false\n',
        encoding="utf-8",
    )
    loaded = load_config(config_file)
    assert loaded.okf is None
    assert loaded.knowledge_vector is not None
    assert loaded.knowledge_vector.mode == "chroma"
    assert loaded.knowledge_vector.collection == "reports"
    assert loaded.knowledge_vector.search_limit == 3
    assert loaded.knowledge_vector.required is False


@pytest.mark.parametrize("declaration", [
    'backend = "unknown"\n',
    'backend = "chroma"\nmode = "okf"\n',
    'backend = "chroma"\ncollection = ""\n',
    'backend = "chroma"\nmax_files = 3\n',
    'backend = "chroma"\nsearch_limit = 21\n',
    'backend = "sqlite"\ncollection = "docs"\n',
])
def test_invalid_chroma_config_fails_closed(tmp_path, declaration):
    config_file = tmp_path / "config.toml"
    config_file.write_text('[knowledge.vector]\n' + declaration, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(config_file)


class StubReader:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    async def search(self, query, *, limit):
        self.calls.append((query, limit))
        return self.hits


def _hit(identifier="c-12", *, content="Die Authentifizierung erfolgt mittels OAuth2."):
    return SimpleNamespace(
        id=identifier, content=content, source="handbuch.pdf",
        metadata={"page": 17}, distance=0.23,
    )


@pytest.mark.asyncio
async def test_chroma_adapter_exposes_real_chunks_with_provenance():
    rag = StubReader([_hit()])
    session = ChromaKnowledgeSession(config=_config(search_limit=3), rag=rag)
    result = await session.search("OAuth2")
    assert rag.calls == [("OAuth2", 3)]
    assert result["entries"] == [{
        "id": "c-12", "content": "Die Authentifizierung erfolgt mittels OAuth2.",
        "source": "handbuch.pdf", "metadata": {"page": 17}, "distance": 0.23,
    }]
    tool_result = await session.call_tool("knowledge_search", {"query": "OAuth2"})
    assert not tool_result.isError
    assert tool_result.structuredContent["entries"][0]["source"] == "handbuch.pdf"
    assert (await session.call_tool("not_allowed", {"query": "OAuth2"})).isError


@pytest.mark.asyncio
async def test_chroma_adapter_rejects_oversized_untrusted_chunks():
    session = ChromaKnowledgeSession(config=_config(), rag=StubReader([
        _hit(content="x" * 16_001),
    ]))
    with pytest.raises(ValueError, match="maximale Kontextgröße"):
        await session.search("what")


def test_only_host_issued_chroma_tokens_may_be_selected():
    state = _KnowledgeRunState(allow_empty_not_found=True)
    enriched = _register_chroma_results(state, {
        "query": "OAuth2",
        "entries": [{
            "id": "record-1", "content": "OAuth2 verification",
            "source": "pdf-A", "metadata": {"page": 4}, "distance": 0.15,
        }],
    })
    token = enriched["entries"][0]["selection_token"]
    assert token.startswith("OKFSEL-")
    selected = _assemble_chroma_payload(
        {"selected_okf_tokens": [token], "warnings": []}, state,
    )
    assert selected["backend"] == "chroma"
    assert selected["content"] == [{
        "id": "record-1", "source": "pdf-A", "metadata": {"page": 4},
        "distance": 0.15, "content": "OAuth2 verification",
    }]
    with pytest.raises(ValueError, match="Unbekannter"):
        _assemble_chroma_payload(
            {"selected_okf_tokens": ["UNISSUED"], "warnings": []}, state,
        )


def _agent_with_chroma(session):
    agent = CliAgent.__new__(CliAgent)
    agent._vector_config = _config()
    agent._vector_session = session
    agent._okf_options = None
    agent._okf_enabled = False
    agent._knowledge_session = None
    agent._knowledge_tools = [{"type": "function", "function": {
        "name": "vector__knowledge_search",
    }}]
    agent._knowledge_routes = {
        "vector__knowledge_search": (session, "knowledge_search", SimpleNamespace(name="vector")),
    }
    agent._knowledge_instructions = None
    agent.history = []
    agent._dump_value = lambda *args, **kwargs: None
    return agent


@pytest.mark.asyncio
async def test_chroma_agent_knowledge_selection_reaches_main_context():
    session = ChromaKnowledgeSession(config=_config(), rag=StubReader([_hit()]))
    agent = _agent_with_chroma(session)

    async def model_select(**kwargs):
        request = json.loads(kwargs["messages"][-1]["content"])
        assert kwargs["phase"] == "knowledge"
        assert kwargs["tools"][0]["function"]["name"] == "vector__knowledge_search"
        token = request["initial_search"]["entries"][0]["selection_token"]
        assert "OAuth2" in request["initial_search"]["entries"][0]["content"]
        return json.dumps({
            "found_content": True, "selected_okf_tokens": [token], "warnings": [],
        })

    agent._run_model_loop = AsyncMock(side_effect=model_select)
    knowledge = await agent._collect_knowledge("Wie funktioniert OAuth2?")
    assert knowledge is not None
    payload = json.loads(knowledge)
    assert payload["backend"] == "chroma"
    assert payload["content"][0]["source"] == "handbuch.pdf"
    assert payload["content"][0]["metadata"]["page"] == 17

    # The normal main-loop reference context must contain the same verified text.
    agent._server_untrusted_instructions = {}
    agent._active_servers = set()
    main_context = agent._build_reference_context_message(knowledge=knowledge)
    assert "retrieved_chroma_knowledge" in main_context
    assert "Die Authentifizierung erfolgt" in main_context


@pytest.mark.asyncio
async def test_chroma_required_missing_index_aborts_not_silently_skipped():
    agent = _agent_with_chroma(None)
    with pytest.raises(RuntimeError, match="nicht verfügbar"):
        await agent._collect_knowledge("Anfrage")


@pytest.mark.asyncio
async def test_chroma_search_failure_with_optional_backend_is_not_fatal():
    class BrokenReader:
        async def search(self, *_args, **_kwargs):
            raise ValueError("embedding unavailable")
    session = ChromaKnowledgeSession(config=_config(), rag=BrokenReader())
    agent = _agent_with_chroma(session)
    agent._vector_config = _config(required=False)
    assert await agent._collect_knowledge("Anfrage") is None


@pytest.mark.asyncio
async def test_chroma_session_starts_without_okf(tmp_path, monkeypatch):
    from cli_agent.context import chroma_knowledge
    stub = ChromaKnowledgeSession(config=_config(), rag=StubReader([_hit()]))
    opened = []

    def fake_open(config, *, config_file, admin_config):
        opened.append((config, config_file, admin_config))
        return stub

    monkeypatch.setattr(chroma_knowledge.ChromaKnowledgeSession, "open", fake_open)
    agent = _agent_with_chroma(None)
    agent.config_file = tmp_path / "config.toml"
    agent.network = NetworkConfig()
    agent._model_credential_rules = ()
    await agent._start_vector_knowledge()
    assert agent._vector_session is stub
    assert len(opened) == 1
    assert agent._knowledge_tools[1]["function"]["name"] == "vector__knowledge_search"
    assert "vector__knowledge_search" in agent._knowledge_routes



def test_chroma_cumulative_budget_is_enforced_across_searches():
    state = _KnowledgeRunState(allow_empty_not_found=True)
    for index in range(4):
        result = _register_chroma_results(state, {
            "query": f"search-{index}",
            "entries": [{
                "id": f"chunk-{index}-{j}",
                "source": "handbook.pdf",
                "content": f"chunk text {index}-{j} " + ("x" * 14_000),
                "metadata": {"page": j}, "distance": 0.1,
            } for j in range(5)],
        })
        if result["budget_exhausted"]:
            break
    assert state.chroma_budget_exhausted
    assert len(state.concepts) <= 40
    assert state.chroma_chars <= 128_000
    assert all(
        item["selection_token"] in state.concepts
        for item in result["entries"]
    )
    assert not any("chunk-3" in item["path"] for item in state.concepts.values())


def test_chroma_duplicate_hits_keep_first_verified_snapshot():
    state = _KnowledgeRunState(allow_empty_not_found=True)
    one = _register_chroma_results(state, {
        "query": "first", "entries": [{
            "id": "same", "source": "first.pdf",
            "content": "First verified text", "metadata": {"page": 1},
            "distance": 0.1,
        }],
    })
    two = _register_chroma_results(state, {
        "query": "second", "entries": [{
            "id": "same", "source": "changed.pdf",
            "content": "Changed text", "metadata": {"page": 99},
            "distance": 0.9,
        }],
    })
    assert len(state.concepts) == 1
    assert two["entries"][0]["selection_token"] == one["entries"][0]["selection_token"]
    assert two["entries"][0]["content"] == "First verified text"
    assert state.chroma_chars == len("First verified text")


@pytest.mark.asyncio
async def test_chroma_retrieval_is_independent_of_unavailable_optional_okf():
    session = ChromaKnowledgeSession(config=_config(), rag=StubReader([_hit()]))
    agent = _agent_with_chroma(session)
    agent._okf_options = SimpleNamespace(required=False)
    agent._okf_enabled = True
    agent._knowledge_session = None  # unrelated optional OKF failed startup

    async def model_select(**kwargs):
        request = json.loads(kwargs["messages"][-1]["content"])
        token = request["initial_search"]["entries"][0]["selection_token"]
        return json.dumps({
            "found_content": True, "selected_okf_tokens": [token], "warnings": [],
        })

    agent._run_model_loop = AsyncMock(side_effect=model_select)
    payload = json.loads(await agent._collect_knowledge("Wie funktioniert OAuth2?"))
    assert payload["backend"] == "chroma"
    assert payload["content"][0]["source"] == "handbuch.pdf"


@pytest.mark.asyncio
async def test_initial_chroma_retrieval_emits_bounded_tool_events():
    session = ChromaKnowledgeSession(config=_config(), rag=StubReader([_hit()]))
    agent = _agent_with_chroma(session)
    events = []

    async def on_event(kind, payload):
        events.append((kind, payload))

    agent.event_callback = on_event

    async def model_select(**kwargs):
        request = json.loads(kwargs["messages"][-1]["content"])
        token = request["initial_search"]["entries"][0]["selection_token"]
        return json.dumps({
            "found_content": True, "selected_okf_tokens": [token], "warnings": [],
        })

    agent._run_model_loop = AsyncMock(side_effect=model_select)
    knowledge = await agent._collect_knowledge("What does handbook.pdf say?")
    assert json.loads(knowledge)["found_content"] is True
    assert [event[0] for event in events] == ["tool_started", "tool_finished"]
    assert events[0][1]["tool"] == "vector__knowledge_search"
    assert events[1][1]["call_id"] == events[0][1]["call_id"]
    assert events[1][1]["status"] == "success"
    assert events[1][1]["search_summary"] == {
        "matches": 1, "collection": "knowledge", "initial": True,
    }
    assert "handbuch.pdf" not in str(events)
    assert "Authentifizierung" not in str(events)
    assert "What does handbook" not in str(events)


@pytest.mark.asyncio
async def test_failed_initial_chroma_retrieval_balances_tool_events():
    class BrokenReader:
        async def search(self, *_args, **_kwargs):
            raise ValueError("sensitive internal embedding failure")

    session = ChromaKnowledgeSession(config=_config(), rag=BrokenReader())
    agent = _agent_with_chroma(session)
    agent._vector_config = _config(required=False)
    events = []

    async def on_event(kind, payload):
        events.append((kind, payload))

    agent.event_callback = on_event
    assert await agent._collect_knowledge("question") is None
    assert [event[0] for event in events] == ["tool_started", "tool_finished"]
    assert events[1][1]["status"] == "error"
    assert events[1][1]["call_id"] == events[0][1]["call_id"]
    assert "sensitive internal" not in str(events)


def test_chroma_summary_never_includes_raw_result_data():
    from cli_agent.context.chroma_knowledge import chroma_search_event_summary

    result = {"query": "secret question", "entries": [{
        "id": "secret-id", "content": "sensitive document text",
        "source": "internal.pdf", "metadata": {"private": "value"},
    }]}
    assert chroma_search_event_summary(result, collection="knowledge", initial=False) == {
        "matches": 1, "collection": "knowledge", "initial": False,
    }
