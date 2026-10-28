"""The single text ingress and structured-artifact chaining contract."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from cli_agent.agent_knowledge import tool_result_text
from cli_agent.config import McpServerConfig
from cli_agent.data_mcp_server import create_server
from cli_agent.data_operations import DataOperationError, DataOperations, MAX_DATA_PAYLOAD_CHARS
from cli_agent.data_result_metadata import DECIMAL_SCALAR_KEY
from cli_agent.mcp_result_artifacts import (
    McpResultArtifacts, METADATA_FOR_LLM_KEY, ONLY_METADATA_TO_LLM_KEY,
)
from cli_agent.os_operations import Workspace


@pytest.fixture
def configured(tmp_path: Path):
    workspace = Workspace.from_directory(tmp_path, McpServerConfig(name="data", built_in=True))
    return create_server(DataOperations(workspace), access="read"), McpResultArtifacts(tmp_path, ()), tmp_path


@pytest.mark.parametrize("content", [
    "| category | amount |\n|---|---|\n| A | 2 |\n| B | 4 |",
    '[{"category":"A","amount":2},{"category":"B","amount":4}]',
    '{"points":[{"category":"A","amount":2},{"category":"B","amount":4}]}',
    "category,amount\nA,2\nB,4\n",
    "category\tamount\nA\t2\nB\t4\n",
    '{"category":"A","amount":2}\n{"category":"B","amount":4}\n',
    '\ufeffcategory,amount\nA,2\nB,4\n',
])
@pytest.mark.asyncio
async def test_every_supported_text_format_chains_through_paths(configured, content):
    server, store, directory = configured
    try:
        wrapped = await server._tool_manager.call_tool("wrap_data", {"content": content}, convert_result=True)
        assert wrapped.meta[ONLY_METADATA_TO_LLM_KEY] is True
        assert wrapped.structuredContent["rows"] == [
            {"category": "A", "amount": 2}, {"category": "B", "amount": 4},
        ]
        meta = wrapped.meta[METADATA_FOR_LLM_KEY]
        assert meta["row_count"] == 2 and meta["records_path"] == "/rows"
        assert meta["sample_rows_complete"] is True
        assert "rows" not in meta and "result" not in meta
        info = json.loads(store.metadata_result(wrapped, tool_result_text(wrapped)))
        source = directory / info["workspace_path"]
        assert source.is_file() and source.suffix == ".json"
        selected = await server._tool_manager.call_tool("select_data", {
            "path": info["workspace_path"], "records_path": info["records_path"],
            "filters": [{"column": "category", "op": "eq", "value": "B"}],
        }, convert_result=True)
        assert selected.structuredContent["rows"] == [{"category": "B", "amount": 4}]
        selected_info = json.loads(store.metadata_result(selected, tool_result_text(selected)))
        aggregate = await server._tool_manager.call_tool("aggregate_data", {
            "path": selected_info["workspace_path"],
            "records_path": selected_info["records_path"],
            "aggregations": [{"column": "amount", "function": "sum", "alias": "total"}],
        }, convert_result=True)
        assert aggregate.structuredContent["rows"] == [{"total": 4}]
        assert "rows" not in aggregate.meta[METADATA_FOR_LLM_KEY]
    finally:
        store.cleanup()


@pytest.mark.parametrize("content", [
    "## First\n\n| a |\n|---|\n| 1 |\n\n## Second\n\n| a |\n|---|\n| 2 |",
    '{"first":[{"a":1}],"second":[{"a":2}]}',
])
@pytest.mark.asyncio
async def test_multiple_record_sets_have_explicit_pointers(configured, content):
    server, store, directory = configured
    try:
        wrapped = await server._tool_manager.call_tool("wrap_data", {"content": content}, convert_result=True)
        assert [item["rows"][0]["a"] for item in wrapped.structuredContent["tables"]] == [1, 2]
        metadata = wrapped.meta[METADATA_FOR_LLM_KEY]
        assert [item["records_path"] for item in metadata["record_sets"]] == [
            "/tables/0/rows", "/tables/1/rows",
        ]
        assert all(item["sample_rows_complete"] for item in metadata["record_sets"])
        info = json.loads(store.metadata_result(wrapped, tool_result_text(wrapped)))
        second = await server._tool_manager.call_tool("select_data", {
            "path": info["workspace_path"], "records_path": "/tables/1/rows",
        }, convert_result=True)
        assert second.structuredContent["rows"] == [{"a": 2}]
    finally:
        store.cleanup()


@pytest.mark.parametrize("content", [
    "", "hello, world", "{bad json}", '[{"x":1,}]',
    "a,b\n1,2,3", "[]",
])
def test_unsupported_or_ambiguous_inline_data_fails(configured, content):
    server, store, _ = configured
    try:
        with pytest.raises((ToolError, DataOperationError)):
            # The actual Python parser and the public MCP wrapper have the
            # same validation semantics, irrespective of the transport.
            server._tool_manager.get_tool("wrap_data").fn(content)
    finally:
        store.cleanup()


def test_inline_limit_and_invalid_jsonl_are_rejected():
    with pytest.raises(DataOperationError, match="Größenlimit"):
        DataOperations.wrap_data("a" * (MAX_DATA_PAYLOAD_CHARS + 1))
    with pytest.raises(DataOperationError, match="Ungültiges JSON"):
        DataOperations.wrap_data('{"a":1}\n{"a":broken}\n')


@pytest.mark.parametrize("content", [
    '{"id":1,"value":0.1234567890123456789012345}\n',
    'id,value\n1,0.1234567890123456789012345\n',
    'id\tvalue\n1\t0.1234567890123456789012345\n',
])
def test_precise_decimals_survive_jsonl_csv_and_tsv_wrapping(content):
    sets = DataOperations.wrap_data(content)
    assert sets[0][1][0]["value"] == Decimal("0.1234567890123456789012345")


@pytest.mark.asyncio
async def test_precision_boolean_and_timestamps_survive_artifact_boundary(configured):
    server, store, directory = configured
    content = ('[{"enabled":true,"id":9007199254740997,'
               '"value":0.1234567890123456789012345,'
               '"timestamp":"2026-10-28T08:00:00.123456789+02:00"}]')
    try:
        wrapped = await server._tool_manager.call_tool("wrap_data", {"content": content}, convert_result=True)
        row = wrapped.structuredContent["rows"][0]
        assert row["enabled"] is True and row["id"] == 9007199254740997
        assert row["value"] == {DECIMAL_SCALAR_KEY: "0.1234567890123456789012345"}
        assert row["timestamp"] == "2026-10-28T08:00:00.123456789+02:00"
        source = json.loads(store.metadata_result(wrapped, tool_result_text(wrapped)))
        columns, rows = DataOperations(Workspace.from_directory(
            directory, McpServerConfig(name="data", built_in=True)
        ))._read_records(source["workspace_path"], records_path="/rows")
        assert rows[0]["value"] == Decimal("0.1234567890123456789012345")
        assert isinstance(rows[0]["value"], Decimal)
        selected = await server._tool_manager.call_tool("select_data", {
            "path": source["workspace_path"], "records_path": "/rows",
        }, convert_result=True)
        assert selected.structuredContent["rows"] == wrapped.structuredContent["rows"]
    finally:
        store.cleanup()


@pytest.mark.asyncio
async def test_existing_json_file_with_known_pointer_skips_wrap(configured):
    server, store, directory = configured
    (directory / "existing.json").write_text(
        '{"points":[{"id":42,"v":7}]}', encoding="utf-8",
    )
    try:
        selected = await server._tool_manager.call_tool("select_data", {
            "path": "existing.json", "records_path": "/points",
        }, convert_result=True)
        assert selected.structuredContent["rows"] == [{"id": 42, "v": 7}]
        assert server._tool_manager.get_tool("inspect_json") is not None
        counts = await server._tool_manager.call_tool("value_counts", {
            "column": "v", "path": "existing.json", "records_path": "/points",
        }, convert_result=True)
        assert counts.structuredContent["rows"] == [{"value": 7, "count": 1}]
        assert counts.meta[METADATA_FOR_LLM_KEY]["records_path"] == "/rows"
        assert "rows" not in counts.meta[METADATA_FOR_LLM_KEY]
    finally:
        store.cleanup()


def test_legacy_inline_fields_are_rejected_even_with_path(configured):
    server, store, _ = configured
    try:
        for name in ("inspect_schema", "preview_data", "select_data", "aggregate_data", "value_counts"):
            tool = server._tool_manager.get_tool(name)
            assert "data" not in tool.parameters["properties"]
            assert "data_format" not in tool.parameters["properties"]
            assert tool.parameters["additionalProperties"] is False
            with pytest.raises(ValueError, match="Extra inputs are not permitted"):
                tool.fn_metadata.arg_model.model_validate({
                    "path": "existing.json", "data": "[]",
                    **({"aggregations": []} if name == "aggregate_data" else {}),
                    **({"column": "id"} if name == "value_counts" else {}),
                })
    finally:
        store.cleanup()
