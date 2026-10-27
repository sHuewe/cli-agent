from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.agent import CliAgent
from cli_agent.agent_tool_calls import process_tool_calls
from cli_agent.config import McpServerConfig
from cli_agent.mcp_result_artifacts import (
    ArtifactError,
    METADATA_FOR_LLM_KEY,
    ONLY_METADATA_TO_LLM_KEY,
    McpResultArtifacts,
)
from cli_agent.ollama import OllamaClient


class Session:
    async def call_tool(self, _name, _arguments):
        return self.result


async def _execute(agent: CliAgent, response: object) -> str:
    session = Session()
    session.result = response
    agent._tool_routes["external__history"] = (
        session,
        "history",
        McpServerConfig(name="external", command="unused", built_in=True),
    )
    agent._server_tools["external"] = [{
        "type": "function",
        "function": {
            "name": "external__history",
            "description": "History",
            "parameters": {"type": "object"},
        },
    }]
    agent._active_servers.add("external")
    messages = [
        {"role": "system", "content": ""},
        {"role": "assistant", "content": "", "tool_calls": []},
    ]
    await process_tool_calls(
        agent,
        assistant_message=messages[1],
        messages=messages,
        tool_calls=[{"id": "1", "function": {"name": "external__history", "arguments": {}}}],
        routes=agent._tool_routes,
        enabled_server_names=None,
        max_tool_calls=2,
        phase="main",
        knowledge_state=None,
        max_concept_reads=None,
        calls=0,
        knowledge_selection_only_mode=False,
        transient_rejections=[],
    )
    return next(message["content"] for message in messages if message.get("role") == "tool")


def _agent(
    tmp_path: Path,
    *,
    access: str = "write",
    with_data: bool = False,
    data_active: bool = False,
) -> CliAgent:
    os_config = McpServerConfig(name="os", command="unused", built_in=True)
    data_config = McpServerConfig(name="data", command="unused", built_in=True)
    agent = CliAgent(
        tmp_path,
        OllamaClient(base_url="http://localhost:11434", model="test"),
        (os_config, data_config) if with_data else (os_config,),
        workspace_access=access,
    )
    agent._server_configs["os"] = os_config
    if access in {"read", "write"}:
        agent._active_servers.add("os")
    if with_data:
        agent._server_configs["data"] = data_config
    if with_data and data_active:
        agent._active_servers.add("data")
    return agent


def _response(*, metadata: object = None, flag: object = True, error: bool = False):
    return SimpleNamespace(
        meta={
            ONLY_METADATA_TO_LLM_KEY: flag,
            METADATA_FOR_LLM_KEY: {"series": "L1", "count": 2} if metadata is None else metadata,
        },
        structuredContent={"points": [{"time": "07:06", "value": -811}, {"time": "07:23", "value": -498}]},
        content=[],
        isError=error,
    )


def test_tool_result_exposes_only_metadata_and_session_path(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    try:
        model_text = asyncio.run(_execute(agent, _response()))
        model_value = json.loads(model_text)
        assert model_value["series"] == "L1"
        assert model_value["count"] == 2
        assert "points" not in model_value
        relative_path = Path(model_value["workspace_path"])
        assert not relative_path.is_absolute()
        path = tmp_path / relative_path
        assert json.loads(path.read_text(encoding="utf-8")) == _response().structuredContent
        assert relative_path.parts[0].startswith("mcp-data-")
    finally:
        asyncio.run(agent.close())
    assert not path.exists()
    assert not path.parent.exists()


@pytest.mark.parametrize(
    ("access", "flag"),
    [
        ("none", True),
        ("read", True),
        ("write", False),
        ("write", 1),
    ],
)
def test_raw_tool_result_if_opt_in_conditions_are_absent(
    tmp_path: Path, access: str, flag: object
) -> None:
    agent = _agent(tmp_path, access=access)
    model_text = asyncio.run(_execute(agent, _response(flag=flag)))
    assert "points" in json.loads(model_text)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    ("with_data", "data_active"),
    [(False, False), (True, False), (True, True)],
)
def test_metadata_only_requires_no_data_mcp(
    tmp_path: Path, with_data: bool, data_active: bool
) -> None:
    agent = _agent(tmp_path, with_data=with_data, data_active=data_active)
    metadata = json.loads(asyncio.run(_execute(agent, _response())))
    assert metadata["series"] == "L1"
    assert "points" not in metadata
    assert (tmp_path / metadata["workspace_path"]).is_file()
    asyncio.run(agent.close())


def test_error_response_with_opt_in_still_uses_metadata(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    model_result = json.loads(asyncio.run(_execute(agent, _response(error=True))))
    assert "points" not in model_result
    assert (tmp_path / model_result["workspace_path"]).is_file()
    asyncio.run(agent.close())


def test_metadata_path_cannot_be_chosen_by_mcp_server(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    model_text = asyncio.run(_execute(agent, _response(metadata={"workspace_path": "../private", "count": 2})))
    metadata = json.loads(model_text)
    assert metadata["workspace_path"] != "../private"
    assert (tmp_path / metadata["workspace_path"]).is_file()
    asyncio.run(agent.close())


@pytest.mark.parametrize("metadata", [[], "text", {"invalid": object()}, {"x": float("nan")}])
def test_invalid_metadata_never_exposes_raw_data(tmp_path: Path, metadata: object) -> None:
    agent = _agent(tmp_path)
    result = json.loads(asyncio.run(_execute(agent, _response(metadata=metadata))))
    assert result["error"] == "mcp_result_artifact_failed"
    assert "points" not in result
    assert not list(tmp_path.iterdir())


def test_text_tool_result_is_saved_without_structured_content(tmp_path: Path) -> None:
    store = McpResultArtifacts(tmp_path, ())
    result = SimpleNamespace(meta={ONLY_METADATA_TO_LLM_KEY: True, METADATA_FOR_LLM_KEY: {"kind": "text"}}, structuredContent=None)
    metadata = json.loads(store.metadata_result(result, "time,value\n07:06,-811"))
    path = tmp_path / metadata["workspace_path"]
    assert path.suffix == ".txt"
    assert path.read_text(encoding="utf-8") == "time,value\n07:06,-811"
    store.cleanup()
    assert not path.exists()


def test_untrusted_data_server_does_not_disable_artifact_mode(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    agent._server_configs["data"] = McpServerConfig(name="data", command="unused")
    agent._active_servers.add("data")
    model_result = json.loads(asyncio.run(_execute(agent, _response())))
    assert "points" not in model_result
    assert (tmp_path / model_result["workspace_path"]).is_file()
    asyncio.run(agent.close())


def test_artifact_write_failure_does_not_fall_back_to_raw_data(tmp_path: Path) -> None:
    os_config = McpServerConfig(
        name="os", command="unused", built_in=True,
        literal_args=("--protected-path", str(tmp_path)),
    )
    agent = CliAgent(
        tmp_path,
        OllamaClient(base_url="http://localhost:11434", model="test"),
        (os_config,), workspace_access="write",
    )
    agent._server_configs["os"] = os_config
    agent._active_servers.add("os")
    metadata = json.loads(asyncio.run(_execute(agent, _response())))
    assert metadata["error"] == "mcp_result_artifact_failed"
    assert "points" not in metadata
    assert not list(tmp_path.iterdir())


def test_cleanup_keeps_user_files_and_other_sessions(tmp_path: Path) -> None:
    first = McpResultArtifacts(tmp_path, ())
    second = McpResultArtifacts(tmp_path, ())
    first_path = tmp_path / first.save("first", structured=False)
    second_path = tmp_path / second.save("second", structured=False)
    (tmp_path / "user.json").write_text("important", encoding="utf-8")
    first.cleanup()
    assert not first_path.exists()
    assert second_path.read_text(encoding="utf-8") == "second"
    assert (tmp_path / "user.json").read_text(encoding="utf-8") == "important"
    second.cleanup()
    assert not second_path.exists()


def test_cleanup_does_not_remove_replaced_artifact(tmp_path: Path) -> None:
    store = McpResultArtifacts(tmp_path, ())
    path = tmp_path / store.save("original", structured=False)
    replacement = tmp_path / "replacement.txt"
    replacement.write_text("other file", encoding="utf-8")
    replacement.replace(path)
    store.cleanup()
    assert path.read_text(encoding="utf-8") == "other file"


def test_cleanup_also_runs_when_mcp_close_fails(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    path = tmp_path / agent._result_artifacts.save("data", structured=False)

    class FailingStack:
        async def aclose(self):
            raise RuntimeError("MCP transport close failed")

    agent._exit_stack = FailingStack()
    with pytest.raises(RuntimeError, match="MCP transport close failed"):
        asyncio.run(agent.close())
    assert not path.exists()


def test_session_artifact_store_rejects_forbidden_directories(tmp_path: Path) -> None:
    os_config = McpServerConfig(
        name="os", command="unused", built_in=True,
        literal_args=("--protected-path", str(tmp_path)),
    )
    store = McpResultArtifacts(tmp_path, (os_config,))
    with pytest.raises((RuntimeError, ArtifactError)):
        store.save("cannot write", structured=False)
    assert not list(tmp_path.iterdir())
