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
