from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cli_agent.agent import CliAgent


@pytest.mark.asyncio
async def test_reentry_limits_duplicates_and_preserves_structured_payload():
    agent = object.__new__(CliAgent)
    agent._knowledge_reentry_calls = 0
    agent._knowledge_reentry_seen = set()
    agent._collect_knowledge = AsyncMock(return_value=json.dumps({
        "found_content": True, "content": [{"concept": "architecture.md", "content": "x"}]
    }))

    first = await agent._request_additional_knowledge("Architecture details?")
    assert not first.isError
    assert first.structuredContent["knowledge"]["found_content"] is True
    duplicate = await agent._request_additional_knowledge(" architecture   DETAILS? ")
    assert duplicate.structuredContent["status"] == "no_new_information"
    assert agent._collect_knowledge.await_count == 1
    second = await agent._request_additional_knowledge("Deployment requirements?")
    assert second.structuredContent["status"] == "ok"
    third = await agent._request_additional_knowledge("Third unrelated question?")
    assert third.structuredContent["status"] == "limit_reached"
    assert agent._collect_knowledge.await_count == 2


@pytest.mark.asyncio
async def test_reentry_rejects_invalid_and_handles_failure():
    agent = object.__new__(CliAgent)
    agent._knowledge_reentry_calls = 0
    agent._knowledge_reentry_seen = set()
    agent._collect_knowledge = AsyncMock(side_effect=RuntimeError("unavailable"))
    assert (await agent._request_additional_knowledge("")).isError
    assert (await agent._request_additional_knowledge("x" * 2001)).isError
    result = await agent._request_additional_knowledge("What is Service A?")
    assert result.isError
    assert result.structuredContent["status"] == "error"


@pytest.mark.parametrize("mode", ["okf", "hybrid"])
def test_knowledge_prompt_matches_available_backends(mode):
    agent = object.__new__(CliAgent)
    agent._vector_config = None if mode == "okf" else SimpleNamespace(
        mode="okf",
    )
    agent._vector_session = None if mode == "okf" else object()
    agent._okf_options = None
    agent._knowledge_instructions = None
    agent._knowledge_tools = [{"function": {"name": "vector__knowledge_search"}}]
    prompt = agent._build_knowledge_system_prompt()
    if mode == "okf":
        assert "root_index" in prompt
    else:
        assert "initial_search" in prompt
    assert "vector__knowledge_search" in prompt


@pytest.mark.asyncio
async def test_disabled_okf_hybrid_does_not_call_embedding_or_knowledge_loop():
    from cli_agent.agent_conversation import ConversationMixin

    class Harness(ConversationMixin):
        pass

    agent = Harness()
    agent._okf_options = SimpleNamespace(required=True)
    agent._okf_enabled = False
    agent._vector_config = SimpleNamespace(mode="okf", required=True)
    agent._vector_session = SimpleNamespace(search=AsyncMock(side_effect=AssertionError("Embedding service must not be contacted")))
    agent._knowledge_session = object()
    agent._run_model_loop = AsyncMock(side_effect=AssertionError("Knowledge loop must not run"))
    assert await agent._collect_knowledge("A private OKF question") is None
    agent._vector_session.search.assert_not_awaited()
    agent._run_model_loop.assert_not_awaited()


@pytest.mark.asyncio
async def test_optional_hybrid_vector_start_failure_uses_okf_only():
    from cli_agent.agent_conversation import ConversationMixin

    class Harness(ConversationMixin):
        pass

    agent = Harness()
    agent._okf_options = SimpleNamespace(required=True, max_tool_calls=4, max_concept_reads=3)
    agent._okf_enabled = True
    agent._vector_config = SimpleNamespace(mode="okf", required=False)
    agent._vector_session = None
    agent._knowledge_session = object()
    agent._knowledge_tools = [{"function": {"name": "okf__knowledge_read"}}, {"function": {"name": "vector__knowledge_search"}}]
    agent._knowledge_routes = {
        "okf__knowledge_index": (SimpleNamespace(call_tool=AsyncMock(return_value=SimpleNamespace(
            structuredContent={"kind": "index", "path": ".", "entries": []}, isError=False, content=[]
        ))), "knowledge_index", SimpleNamespace(name="okf")),
        "vector__knowledge_search": (object(), "knowledge_search", SimpleNamespace(name="vector")),
    }
    agent.history = []
    agent._build_knowledge_system_prompt = lambda **kwargs: "okf-only" if not kwargs["vector_active"] else "hybrid"
    agent._dump_value = lambda *args: None
    agent._run_model_loop = AsyncMock(return_value='{"found_content":false,"selected_okf_tokens":[],"warnings":[],"reason_code":"not_applicable"}')
    assert await agent._collect_knowledge("question") is None
    kwargs = agent._run_model_loop.await_args.kwargs
    assert kwargs["messages"][0]["content"] == "okf-only"
    assert [tool["function"]["name"] for tool in kwargs["tools"]] == ["okf__knowledge_read"]
