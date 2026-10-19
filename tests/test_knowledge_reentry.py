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


@pytest.mark.parametrize("mode", ["okf", "hybrid", "vector"])
def test_knowledge_prompt_matches_available_backends(mode):
    agent = object.__new__(CliAgent)
    agent._vector_config = None if mode == "okf" else SimpleNamespace(
        mode="okf" if mode == "hybrid" else "raw",
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
