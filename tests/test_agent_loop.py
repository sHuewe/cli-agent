from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from cli_agent.agent_knowledge import _KnowledgeRunState
from cli_agent.agent_loop import run_model_loop
from cli_agent.config import LoggingConfig


class SequenceModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.tool_sets = []

    async def chat(self, *, messages, tools):
        self.tool_sets.append(list(tools))
        return self.responses.pop(0)


class LoopAgent:
    def __init__(self, responses):
        self.model_client = SequenceModel(responses)
        self.logging_config = LoggingConfig()
        self.dumps = []
        self.discarded = []

    def _dump_context(self, messages, *, phase):
        return None

    def _dump_value(self, filename, value):
        self.dumps.append((filename, value))

    def _discard_rejected_tool_call(self, messages, assistant_message, tool_call, tool_message):
        self.discarded.append((assistant_message, tool_call, tool_message))

    def _append_tool_error(self, messages, tool_call, exposed_name, error):
        message = {
            "role": "tool",
            "tool_call_id": tool_call.get("id"),
            "name": exposed_name,
            "content": error,
        }
        messages.append(message)
        return message


def _run(agent, *, phase="main", state=None, tools=None, routes=None, max_concept_reads=10):
    return asyncio.run(
        run_model_loop(
            agent,
            messages=[{"role": "system", "content": "system"}],
            tools=tools or [],
            routes=routes or {},
            enabled_server_names=None,
            max_tool_calls=10,
            phase=phase,
            knowledge_state=state,
            max_concept_reads=max_concept_reads,
        )
    )


def test_main_loop_retries_one_empty_response_then_returns_answer() -> None:
    agent = LoopAgent(
        [
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": "answer"},
        ]
    )
    assert _run(agent) == "answer"
    assert len(agent.model_client.tool_sets) == 2


def test_main_loop_returns_placeholder_after_two_empty_responses() -> None:
    agent = LoopAgent(
        [
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": ""},
        ]
    )
    assert _run(agent) == "(Das Modell hat keine Antwort erzeugt.)"


def test_knowledge_loop_retries_invalid_selection_then_accepts_correction() -> None:
    state = _KnowledgeRunState()
    token = state.register_concept(
        {"kind": "concept", "path": "a.md", "content": "A"}
    )
    assert token is not None
    agent = LoopAgent(
        [
            {
                "role": "assistant",
                "content": json.dumps(
                    {"found_content": True, "selected_okf_tokens": ["bad"]}
                ),
            },
            {
                "role": "assistant",
                "content": json.dumps(
                    {"found_content": True, "selected_okf_tokens": [token]}
                ),
            },
        ]
    )
    tools = [{"type": "function", "function": {"name": "okf__knowledge_read"}}]
    result = json.loads(_run(agent, phase="knowledge", state=state, tools=tools))
    assert result["selected_okf_tokens"] == [token]
    assert agent.model_client.tool_sets[0] == tools
    assert agent.model_client.tool_sets[1] == []


def test_knowledge_loop_falls_back_after_repeated_invalid_selections() -> None:
    state = _KnowledgeRunState()
    token = state.register_concept(
        {"kind": "concept", "path": "a.md", "content": "A"}
    )
    assert token is not None
    invalid = {
        "role": "assistant",
        "content": json.dumps(
            {"found_content": True, "selected_okf_tokens": ["bad"]}
        ),
    }
    agent = LoopAgent([invalid.copy(), invalid.copy(), invalid.copy()])
    result = json.loads(_run(agent, phase="knowledge", state=state))
    assert result["found_content"] is True
    assert result["selected_okf_tokens"] == [token]
    assert result["agent_fallback"]["strategy"] == "all_read_concepts"
    assert any(name == "knowledge_selection_fallback.json" for name, _ in agent.dumps)


def test_knowledge_loop_falls_back_after_two_empty_responses() -> None:
    state = _KnowledgeRunState()
    token = state.register_concept(
        {"kind": "concept", "path": "a.md", "content": "A"}
    )
    assert token is not None
    agent = LoopAgent(
        [
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": ""},
        ]
    )
    result = json.loads(_run(agent, phase="knowledge", state=state))
    assert result["selected_okf_tokens"] == [token]
    assert result["agent_fallback"]["strategy"] == "all_read_concepts"


def test_knowledge_concept_limit_disables_tools_on_next_model_request() -> None:
    state = _KnowledgeRunState()
    state.add_allowed_calls({"a.md": {"knowledge_read"}})

    tool_call = {
        "id": "call-1",
        "type": "function",
        "function": {
            "name": "okf__knowledge_read",
            "arguments": {"path": "a.md"},
        },
    }

    class KnowledgeSession:
        async def call_tool(self, name, arguments):
            assert name == "knowledge_read"
            assert arguments == {"path": "a.md"}
            return SimpleNamespace(
                isError=False,
                content=[
                    SimpleNamespace(
                        text=json.dumps(
                            {"kind": "concept", "path": "a.md", "content": "A"}
                        )
                    )
                ],
            )

    server_config = SimpleNamespace(
        name="okf",
        compress_result=False,
        compress_min_chars=12_000,
    )
    agent = LoopAgent(
        [
            {"role": "assistant", "content": "", "tool_calls": [tool_call]},
            {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "found_content": False,
                        "selected_okf_tokens": [],
                        "reason_code": "not_found",
                    }
                ),
            },
        ]
    )
    agent._requires_approval = lambda *args, **kwargs: False
    tools = [{"type": "function", "function": {"name": "okf__knowledge_read"}}]
    routes = {"okf__knowledge_read": (KnowledgeSession(), "knowledge_read", server_config)}

    result = json.loads(
        _run(
            agent,
            phase="knowledge",
            state=state,
            tools=tools,
            routes=routes,
            max_concept_reads=1,
        )
    )

    assert result["found_content"] is False
    assert result["reason_code"] == "not_found"
    assert agent.model_client.tool_sets[0] == tools
    assert agent.model_client.tool_sets[1] == []
