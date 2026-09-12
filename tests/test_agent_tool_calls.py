from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from cli_agent.agent_knowledge import _KnowledgeRunState
from cli_agent.agent_tool_calls import process_tool_calls
from cli_agent.config import LoggingConfig, McpServerConfig


class FakeSession:
    def __init__(self, result=None, *, error: Exception | None = None) -> None:
        self.result = result or SimpleNamespace(content=[])
        self.error = error
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        if self.error is not None:
            raise self.error
        return self.result


class ToolAgent:
    def __init__(self, *, approval=True) -> None:
        self.logging_config = LoggingConfig()
        self.approval = approval
        self.approvals: list[tuple[str, dict]] = []
        self.dumps = []
        self.compressions = []

    def _append_tool_error(self, messages, tool_call, tool_name, message):
        tool_message = {
            "role": "tool",
            "content": f"FEHLER: {message}",
        }
        if tool_call.get("id"):
            tool_message["tool_call_id"] = tool_call["id"]
        else:
            tool_message["tool_name"] = str(tool_name)
        messages.append(tool_message)
        return tool_message

    def _requires_approval(self, server_config, tool_name, exposed_name=None):
        return not getattr(server_config, "built_in", False)

    async def _approve_tool_call(self, tool_name, arguments):
        self.approvals.append((tool_name, arguments))
        return self.approval

    async def _compress_tool_result(self, **kwargs):
        self.compressions.append(kwargs)
        return "compressed"

    def _dump_context(self, messages, *, phase):
        self.dumps.append((phase, list(messages)))


def _tool_call(name: str, arguments, *, call_id: str = "call-1") -> dict:
    return {
        "id": call_id,
        "function": {"name": name, "arguments": arguments},
    }


def _run(*, agent, tool_calls, routes=None, enabled=None, phase="main", state=None,
         max_calls=10, calls=0, max_concept_reads=None, selection_only=False):
    assistant = {"role": "assistant", "content": "", "tool_calls": tool_calls}
    messages = [{"role": "system", "content": "system"}, assistant]
    transient = []
    result = asyncio.run(
        process_tool_calls(
            agent,
            assistant_message=assistant,
            messages=messages,
            tool_calls=tool_calls,
            routes=routes or {},
            enabled_server_names=enabled,
            max_tool_calls=max_calls,
            phase=phase,
            knowledge_state=state,
            max_concept_reads=max_concept_reads,
            calls=calls,
            knowledge_selection_only_mode=selection_only,
            transient_rejections=transient,
        )
    )
    return result, messages, transient


def test_unknown_tool_is_rejected_without_execution() -> None:
    agent = ToolAgent()
    call = _tool_call("missing__tool", {})
    (calls, _), messages, transient = _run(agent=agent, tool_calls=[call])
    assert calls == 1
    assert transient
    assert "existiert nicht" in messages[-1]["content"]


def test_non_object_json_arguments_are_rejected() -> None:
    agent = ToolAgent()
    call = _tool_call("server__read", "[1, 2]")
    session = FakeSession()
    config = McpServerConfig(name="server", command="unused", built_in=True)
    routes = {"server__read": (session, "read", config)}
    _result, messages, transient = _run(agent=agent, tool_calls=[call], routes=routes)
    assert session.calls == []
    assert transient
    assert "JSON-Objekt" in messages[-1]["content"]


def test_disabled_server_tool_is_rejected() -> None:
    agent = ToolAgent()
    session = FakeSession()
    config = McpServerConfig(name="server", command="unused", built_in=True)
    routes = {"server__read": (session, "read", config)}
    call = _tool_call("server__read", {})
    _result, messages, transient = _run(
        agent=agent,
        tool_calls=[call],
        routes=routes,
        enabled=set(),
    )
    assert session.calls == []
    assert transient
    assert "deaktiviert" in messages[-1]["content"]


def test_external_tool_requires_approval_and_denial_prevents_execution() -> None:
    agent = ToolAgent(approval=False)
    session = FakeSession()
    config = McpServerConfig(name="external", command="unused")
    routes = {"external__run": (session, "run", config)}
    call = _tool_call("external__run", {"x": 1})
    _result, messages, transient = _run(agent=agent, tool_calls=[call], routes=routes)
    assert agent.approvals == [("external__run", {"x": 1})]
    assert session.calls == []
    assert transient
    assert "Benutzerfreigabe" in messages[-1]["content"]


def test_approved_external_tool_is_executed_and_result_is_appended() -> None:
    result = SimpleNamespace(
        content=[SimpleNamespace(text="result text")],
        isError=False,
    )
    agent = ToolAgent(approval=True)
    session = FakeSession(result)
    config = McpServerConfig(name="external", command="unused")
    routes = {"external__run": (session, "run", config)}
    call = _tool_call("external__run", {"x": 1})
    (calls, notice), messages, transient = _run(agent=agent, tool_calls=[call], routes=routes)
    assert calls == 1
    assert notice is False
    assert transient == []
    assert session.calls == [("run", {"x": 1})]
    assert messages[-1] == {
        "role": "tool",
        "content": "result text",
        "tool_call_id": "call-1",
    }


def test_tool_call_limit_is_enforced_before_execution() -> None:
    agent = ToolAgent()
    call = _tool_call("server__read", {})
    session = FakeSession()
    config = McpServerConfig(name="server", command="unused", built_in=True)
    routes = {"server__read": (session, "read", config)}
    with pytest.raises(RuntimeError, match="nach 1 Tool-Aufrufen"):
        _run(agent=agent, tool_calls=[call], routes=routes, max_calls=1, calls=1)
    assert session.calls == []


def test_knowledge_parallel_calls_only_execute_first() -> None:
    agent = ToolAgent()
    result = SimpleNamespace(
        structuredContent={"internal_links": []},
        content=[],
        isError=False,
    )
    session = FakeSession(result)
    config = McpServerConfig(name="okf", command="unused", built_in=True)
    routes = {
        "okf__knowledge_index": (session, "knowledge_index", config),
        "okf__knowledge_read": (session, "knowledge_read", config),
    }
    state = _KnowledgeRunState(
        allowed_calls={".": {"knowledge_index"}, "a.md": {"knowledge_read"}}
    )
    calls = [
        _tool_call("okf__knowledge_index", {"path": "."}, call_id="first"),
        _tool_call("okf__knowledge_read", {"path": "a.md"}, call_id="second"),
    ]
    _result, messages, transient = _run(
        agent=agent,
        tool_calls=calls,
        routes=routes,
        phase="knowledge",
        state=state,
    )
    assert session.calls == [("knowledge_index", {"path": "."})]
    assert len(transient) == 1
    assert "genau ein" in messages[-2]["content"] or "genau ein" in transient[0][2]["content"]


def test_knowledge_undiscovered_path_is_rejected() -> None:
    agent = ToolAgent()
    session = FakeSession()
    config = McpServerConfig(name="okf", command="unused", built_in=True)
    routes = {"okf__knowledge_read": (session, "knowledge_read", config)}
    state = _KnowledgeRunState(allowed_calls={"known.md": {"knowledge_read"}})
    call = _tool_call("okf__knowledge_read", {"path": "invented.md"})
    _result, messages, transient = _run(
        agent=agent,
        tool_calls=[call],
        routes=routes,
        phase="knowledge",
        state=state,
    )
    assert session.calls == []
    assert transient
    assert "nicht für" in messages[-1]["content"]


def test_duplicate_successful_knowledge_call_is_rejected() -> None:
    agent = ToolAgent()
    session = FakeSession()
    config = McpServerConfig(name="okf", command="unused", built_in=True)
    routes = {"okf__knowledge_read": (session, "knowledge_read", config)}
    state = _KnowledgeRunState(allowed_calls={"a.md": {"knowledge_read"}})
    state.seen_calls.add(("knowledge_read", '{"path":"a.md"}'))
    call = _tool_call("okf__knowledge_read", {"path": "a.md"})
    _result, messages, transient = _run(
        agent=agent,
        tool_calls=[call],
        routes=routes,
        phase="knowledge",
        state=state,
    )
    assert session.calls == []
    assert transient
    assert "bereits erfolgreich ausgeführt" in messages[-1]["content"]


def test_successful_knowledge_read_registers_concept_and_hides_repository_id() -> None:
    structured = {
        "path": "a.md",
        "kind": "concept",
        "content": "full text",
        "warning": None,
        "summary": {"concept_id": "a", "title": "A"},
        "internal_links": [],
    }
    result = SimpleNamespace(
        structuredContent=structured,
        content=[],
        isError=False,
    )
    agent = ToolAgent()
    session = FakeSession(result)
    config = McpServerConfig(name="okf", command="unused", built_in=True)
    routes = {"okf__knowledge_read": (session, "knowledge_read", config)}
    state = _KnowledgeRunState(allowed_calls={"a.md": {"knowledge_read"}})
    call = _tool_call("okf__knowledge_read", {"path": "a.md"})
    (_calls, notice), messages, _ = _run(
        agent=agent,
        tool_calls=[call],
        routes=routes,
        phase="knowledge",
        state=state,
        max_concept_reads=1,
    )
    assert notice is True
    assert len(state.concepts) == 1
    token = next(iter(state.concepts))
    payload = json.loads(messages[-1]["content"])
    assert payload["agent_selection"]["token"] == token
    assert payload["agent_selection"]["path"] == "a.md"
    assert "concept_id" not in json.dumps(payload)
    assert state.successful_followup_calls == 1


def test_compression_failure_falls_back_to_original_result() -> None:
    class FailingCompressionAgent(ToolAgent):
        async def _compress_tool_result(self, **kwargs):
            raise RuntimeError("compression failed")

    text = "x" * 50
    result = SimpleNamespace(content=[SimpleNamespace(text=text)], isError=False)
    agent = FailingCompressionAgent()
    session = FakeSession(result)
    config = McpServerConfig(
        name="server",
        command="unused",
        built_in=True,
        compress_result=True,
        compress_min_chars=10,
    )
    routes = {"server__read": (session, "read", config)}
    call = _tool_call("server__read", {})
    _result, messages, _ = _run(agent=agent, tool_calls=[call], routes=routes)
    assert messages[-1]["content"] == text


def test_tool_exception_is_propagated() -> None:
    agent = ToolAgent()
    session = FakeSession(error=ValueError("boom"))
    config = McpServerConfig(name="server", command="unused", built_in=True)
    routes = {"server__read": (session, "read", config)}
    call = _tool_call("server__read", {})
    with pytest.raises(ValueError, match="boom"):
        _run(agent=agent, tool_calls=[call], routes=routes)
