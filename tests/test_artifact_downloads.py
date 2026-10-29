"""Export and publication regression tests."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.artifact_publication import register_artifact
from cli_agent.config import McpServerConfig
from cli_agent.data_mcp_server import create_server
from cli_agent.mcp_result_artifacts import (
    ArtifactError, McpResultArtifacts, ONLY_METADATA_TO_LLM_KEY, METADATA_FOR_LLM_KEY,
)


class FakeFastMCP:
    def __init__(self, name, *, instructions=None):
        self.tools = {}
        self.instructions = instructions

    def tool(self, **kwargs):
        def decorate(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorate


@pytest.mark.parametrize("export_format,expected", [
    ("csv", "time,value\n10:00,11\n10:10,12\n"),
    ("json", '[{"time": "10:00", "value": 11}, {"time": "10:10", "value": 12}]'),
    ("markdown", "| time | value |"),
])
def test_export_returns_only_metadata_to_model_and_preserves_full_file(
    tmp_path: Path, monkeypatch, export_format, expected,
):
    import cli_agent.data_mcp_server as data_server

    monkeypatch.setattr(data_server, "FastMCP", FakeFastMCP)
    rows = [{"time": "10:00", "value": 11}, {"time": "10:10", "value": 12}]
    operations = SimpleNamespace(
        _read_records=lambda **kwargs: (["time", "value"], rows),
        workspace=object(),
    )
    server = create_server(operations, access="read")
    tool = server.tools["export_data"]
    result = tool("input.json", export_format, records_path="/rows")
    assert result.meta[ONLY_METADATA_TO_LLM_KEY] is True
    assert "rows" not in result.meta[METADATA_FOR_LLM_KEY]
    assert "export_content" not in result.meta[METADATA_FOR_LLM_KEY]
    store = McpResultArtifacts(tmp_path, ())
    try:
        model_result = json.loads(
            store.metadata_result(result, json.dumps(result.structuredContent))
        )
        assert "export_content" not in model_result
        assert "10:00" not in json.dumps(model_result)
        assert model_result["row_count"] == 2
        assert model_result["export_format"] == export_format
        name = model_result["workspace_path"]
        assert name.endswith(".md" if export_format == "markdown" else "." + export_format)
        full = store.read_registered(name).decode("utf-8")
        assert expected in full
    finally:
        store.cleanup()


def test_publication_rejects_unregistered_workspace_files(tmp_path: Path):
    store = McpResultArtifacts(tmp_path, ())
    foreign = tmp_path / "secret.txt"
    foreign.write_text("secret", encoding="utf-8")
    with pytest.raises((ValueError, RuntimeError)):
        register_artifact(store, "secret.txt", "secret.txt")

    original = store.save("hello", structured=False)
    identifier, info = register_artifact(store, original, "report.txt")
    assert len(identifier) == 32
    assert info.path == original
    assert store.read_registered(info.path) == b"hello"

    with pytest.raises(ValueError):
        register_artifact(store, original, "../secret.txt")
    with pytest.raises((ValueError, RuntimeError)):
        register_artifact(store, "../secret.txt", "secret.txt")

    store.cleanup()
    with pytest.raises((ValueError, RuntimeError, OSError)):
        store.read_registered(original)
