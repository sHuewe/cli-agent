from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from mcp.server.fastmcp.tools.base import Tool
import pytest

from cli_agent import data_mcp_server
from cli_agent.agent_knowledge import tool_result_text
from cli_agent.config import McpServerConfig
from cli_agent.data_operations import DataOperationError, DataOperations
from cli_agent.mcp_result_artifacts import (
    METADATA_FOR_LLM_KEY,
    ONLY_METADATA_TO_LLM_KEY,
    McpResultArtifacts,
)
from cli_agent.os_operations import Workspace


class FakeFastMCP:
    def __init__(self, name: str, *, instructions: str | None = None) -> None:
        self.name = name
        self.instructions = instructions
        self.tools: dict[str, object] = {}
        self.run_transport: str | None = None

    def tool(self, **_options):
        def decorator(function):
            self.tools[function.__name__] = function
            return function

        return decorator

    def run(self, *, transport: str) -> None:
        self.run_transport = transport


def _operations(calls):
    return SimpleNamespace(
        calculate=lambda expression: (
            calls.append(("calculate", expression)) or "calculated"
        ),
        wrap_data=lambda content: (
            calls.append(("wrap_data", content)) or [(["x"], [{"x": 1}])]
        ),
        inspect_schema=lambda path=None, **kwargs: (
            calls.append(("schema", path, kwargs)) or "schema"
        ),
        inspect_data=lambda path=None, **kwargs: (
            calls.append(("inspect", path, kwargs)) or "inspected"
        ),
        select_data=lambda path=None, **kwargs: (
            calls.append(("select", path, kwargs)) or "selected"
        ),
        _select_records=lambda path=None, **kwargs: (
            calls.append(("select_records", path, kwargs)) or (["x"], [{"x": 1}])
        ),
        value_counts=lambda column, path=None, **kwargs: (
            calls.append(("counts", column, path, kwargs)) or "counted"
        ),
        _value_counts_records=lambda column, path=None, **kwargs: (
            calls.append(("counts_records", column, path, kwargs)) or ([{"value": 1, "count": 1}], 1)
        ),
        aggregate_data=lambda aggregations, path=None, **kwargs: (
            calls.append(("aggregate", aggregations, path, kwargs))
            or "aggregated"
        ),
        _aggregate_records=lambda path=None, **kwargs: (
            calls.append(("aggregate_records", path, kwargs)) or (["total"], [{"total": 3}])
        ),
        _result=lambda *args, **kwargs: "Rows: 1/1\n\n| x |\n|---|\n| 1 |",
    )


BASE_TOOLS = {
    "calculate",
    "wrap_data",
    "inspect_schema",
    "preview_data",
    "select_data",
    "value_counts",
    "aggregate_data",
}


def test_payload_only_mode_exposes_calculate_only(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)

    server = data_mcp_server.create_server(
        _operations(calls),
        access="none",
    )

    assert set(server.tools) == {"calculate"}
    assert "--with-os-read" in server.instructions
    assert server.tools["calculate"]("1+2") == "calculated"
    assert calls == [("calculate", "1+2")]


def test_wrap_is_single_text_ingress_and_json_inspection_is_path_only(monkeypatch) -> None:
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)
    calls = []
    server = data_mcp_server.create_server(_operations(calls), access="read")
    assert set(inspect.signature(server.tools["wrap_data"]).parameters) == {"content"}
    assert "extract_markdown_tables" not in server.tools
    for name in ("inspect_schema", "preview_data", "select_data", "aggregate_data", "value_counts"):
        parameters = inspect.signature(server.tools[name]).parameters
        assert "path" in parameters and parameters["path"].default is inspect.Parameter.empty
        assert "data" not in parameters and "data_format" not in parameters
    assert set(inspect.signature(server.tools["inspect_json"]).parameters) == {"path"}
    result = server.tools["wrap_data"]('| x |\n|---|\n| 1 |')
    assert result.structuredContent["rows"] == [{"x": 1}]
    assert calls == [("wrap_data", '| x |\n|---|\n| 1 |')]


def test_read_mode_exposes_path_and_no_write_tools(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)

    server = data_mcp_server.create_server(
        _operations(calls),
        access="read",
    )

    assert set(server.tools) == BASE_TOOLS | {"inspect_json"}
    assert "path" in inspect.signature(server.tools["inspect_schema"]).parameters
    assert "path" in inspect.signature(server.tools["preview_data"]).parameters
    assert "path" in inspect.signature(server.tools["select_data"]).parameters
    assert "input_path" not in inspect.signature(server.tools["select_data"]).parameters
    assert "workspace files" in server.instructions.lower()

    assert server.tools["inspect_schema"](
        path="data.csv",
        distinct_values_limit=7,
    ) == "schema"
    assert server.tools["preview_data"](
        path="data.csv",
        sample_rows=3,
    ) == "inspected"
    assert calls == [
        (
            "schema",
            "data.csv",
            {
                "table_index": 0,
                "distinct_values_limit": 7,
                "records_path": None,
            },
        ),
        (
            "inspect",
            "data.csv",
            {
                "table_index": 0,
                "sample_rows": 3,
                "records_path": None,
            },
        )
    ]


def test_write_mode_reuses_read_mode_tools_and_metadata(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)

    server = data_mcp_server.create_server(
        _operations(calls),
        access="write",
    )

    assert set(server.tools) == BASE_TOOLS | {"inspect_json"}
    assert "path" in inspect.signature(server.tools["preview_data"]).parameters
    assert "wrap_data(content)" in server.instructions
    assert "no normal tabular tool accepts inline data" in server.instructions
    assert "limit" not in inspect.signature(server.tools["select_data"]).parameters
    assert "limit" not in inspect.signature(server.tools["aggregate_data"]).parameters

    result = server.tools["select_data"](path="raw.csv", columns=["x"])
    assert result.meta["io.github.shuewe.cli-agent/only_metadata_to_llm"] is True
    assert result.meta["io.github.shuewe.cli-agent/meta_data_for_llm"]["row_count"] == 1
    assert calls == [
        (
            "select_records",
            "raw.csv",
            {
                "table_index": 0,
                "columns": ["x"],
                "filters": None,
                "sort_by": None,
                "descending": False,
                "records_path": None,
            },
        )
    ]


@pytest.mark.asyncio
async def test_real_read_only_mcp_chains_complete_select_and_aggregate_via_artifacts(
    tmp_path: Path,
) -> None:
    (tmp_path / "source.csv").write_text(
        "group,value\n" + "".join(f"g{i:04d},{i}\n" for i in range(1_201)),
        encoding="utf-8",
    )
    workspace = Workspace.from_directory(tmp_path, McpServerConfig(name="data", built_in=True))
    server = data_mcp_server.create_server(DataOperations(workspace), access="read")
    assert server._tool_manager.get_tool("join_data") is not None
    assert server._tool_manager.get_tool("derive_columns") is not None
    assert server._tool_manager.get_tool("resample_data") is not None
    for tool_name in ("describe_data", "analyze_intervals", "integrate_data", "concat_data"):
        assert server._tool_manager.get_tool(tool_name) is not None
    assert server._tool_manager.get_tool("select_data_to_file") is None
    assert server._tool_manager.get_tool("aggregate_data_to_file") is None
    assert "limit" not in server._tool_manager.get_tool("select_data").parameters["properties"]
    assert "limit" not in server._tool_manager.get_tool("aggregate_data").parameters["properties"]

    store = McpResultArtifacts(tmp_path, ())
    try:
        selected = await server._tool_manager.call_tool(
            "select_data", {"path": "source.csv"}, convert_result=True,
        )
        assert selected.meta[ONLY_METADATA_TO_LLM_KEY] is True
        assert len(selected.structuredContent["rows"]) == 1_201
        selected_preview = selected.meta[METADATA_FOR_LLM_KEY]
        assert "result" not in selected_preview
        assert selected_preview["sample_rows"][0] == {"group": "g0000", "value": 0}
        assert selected_preview["sample_rows_complete"] is False
        assert {stat["column"]: stat for stat in selected_preview["column_stats"]}["value"]["max"] == 1200
        selected_metadata = store.metadata_result(selected, tool_result_text(selected))
        result_metadata = json.loads(selected_metadata)
        path = result_metadata["workspace_path"]
        assert len(selected_metadata.encode("utf-8")) <= 5_000
        assert result_metadata["sample_rows"] == selected_preview["sample_rows"]
        assert (tmp_path / path).suffix == ".json"
        assert (tmp_path / path).is_file()

        grouped = await server._tool_manager.call_tool(
            "aggregate_data",
            {"path": path, "aggregations": [{"column": "value", "function": "sum", "alias": "total"}],
             "group_by": ["group"], "records_path": "/rows"},
            convert_result=True,
        )
        assert grouped.meta[ONLY_METADATA_TO_LLM_KEY] is True
        assert len(grouped.structuredContent["rows"]) == 1_201
        grouped_metadata = store.metadata_result(grouped, tool_result_text(grouped))
        grouped_summary = json.loads(grouped_metadata)
        assert (tmp_path / grouped_summary["workspace_path"]).is_file()
        assert len(grouped_metadata.encode("utf-8")) <= 5_000
        assert {stat["column"]: stat for stat in grouped_summary["column_stats"]}["total"]["max"] == 1200
    finally:
        store.cleanup()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source.csv"]


@pytest.mark.asyncio
async def test_new_dataframe_tools_expose_typed_mcp_schemas_and_chain_artifacts(tmp_path: Path) -> None:
    (tmp_path / "day1.json").write_text(json.dumps({"points": [
        {"time": "2026-06-24T00:00:00Z", "value": 100},
        {"time": "2026-06-24T01:00:00Z", "value": 200},
    ]}), encoding="utf-8")
    (tmp_path / "day2.json").write_text(json.dumps({"points": [
        {"time": "2026-06-24T02:00:00Z", "value": 300},
        {"time": "2026-06-24T03:00:00Z", "value": 400},
    ]}), encoding="utf-8")
    workspace = Workspace.from_directory(tmp_path, McpServerConfig(name="data", built_in=True))
    server = data_mcp_server.create_server(DataOperations(workspace), access="read")
    concat_tool = server._tool_manager.get_tool("concat_data")
    assert concat_tool.parameters["$defs"]["DataFrameInput"]["required"] == ["path"]
    interval_tool = server._tool_manager.get_tool("analyze_intervals")
    assert set(interval_tool.parameters["properties"]["condition"]["enum"]) == {
        "lt", "lte", "gt", "gte", "eq", "ne",
    }
    assert "max_gap_seconds" in interval_tool.parameters["required"]

    description = await server._tool_manager.call_tool(
        "describe_data", {"path": "day1.json", "records_path": "/points", "time_column": "time"},
        convert_result=True,
    )
    assert json.loads(description[0][0].text)["statistics"]["value"]["mean"] == 150

    concatenated = await server._tool_manager.call_tool(
        "concat_data", {"sources": [
            {"path": "day1.json", "records_path": "/points"},
            {"path": "day2.json", "records_path": "/points"},
        ]}, convert_result=True,
    )
    assert concatenated.meta[ONLY_METADATA_TO_LLM_KEY] is True
    store = McpResultArtifacts(tmp_path, ())
    try:
        stored = json.loads(store.metadata_result(concatenated, tool_result_text(concatenated)))
        assert stored["row_count"] == 4
        integrated = await server._tool_manager.call_tool(
            "integrate_data", {
                "path": stored["workspace_path"], "records_path": stored["records_path"],
                "time_column": "time", "value_column": "value", "max_gap_seconds": 4000,
            }, convert_result=True,
        )
        assert json.loads(integrated[0][0].text)["integral"] == 750
        intervals = await server._tool_manager.call_tool(
            "analyze_intervals", {
                "path": stored["workspace_path"], "records_path": stored["records_path"],
                "time_column": "time", "value_column": "value", "condition": "gt",
                "threshold": 150, "max_gap_seconds": 4000,
            }, convert_result=True,
        )
        assert intervals.meta[METADATA_FOR_LLM_KEY]["summary"]["total_seconds"] == 7200
    finally:
        store.cleanup()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["day1.json", "day2.json"]


def test_wrap_tool_description_covers_all_formats(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)
    server = data_mcp_server.create_server(_operations(calls), access="read")

    tool = server.tools["wrap_data"]
    assert "normalize" in (tool.__doc__ or "").lower()
    assert "NDJSON" in tool.__doc__
    assert tool("| a |\n|---|\n| 1 |").structuredContent["rows"] == [{"x": 1}]
    assert calls == [
        ("wrap_data", "| a |\n|---|\n| 1 |")
    ]


def test_inspection_tool_descriptions_separate_schema_from_rows(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)
    server = data_mcp_server.create_server(_operations(calls), access="read")

    schema_doc = (server.tools["inspect_schema"].__doc__ or "").lower()
    preview_doc = (server.tools["preview_data"].__doc__ or "").lower()

    assert "never returns dataset rows" in schema_doc
    assert "low-cardinality" in schema_doc
    assert "select_data" in schema_doc
    assert "complete records" in preview_doc
    assert "inspect_schema" in preview_doc
    assert "select_data" in preview_doc


def test_select_data_exposes_structured_filter_schema(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)
    server = data_mcp_server.create_server(_operations(calls), access="read")

    function = server.tools["select_data"]
    tool = Tool.from_function(function)
    filter_schema = tool.parameters["$defs"]["DataFilter"]

    assert set(filter_schema["required"]) == {"column", "op"}
    assert filter_schema["properties"]["column"]["type"] == "string"
    assert set(filter_schema["properties"]["op"]["enum"]) == {
        "eq",
        "ne",
        "lt",
        "lte",
        "gt",
        "gte",
        "in",
        "not_in",
        "is_null",
        "not_null",
        "contains",
    }
    assert "value" in filter_schema["properties"]

    validated = tool.fn_metadata.arg_model.model_validate(
        {
            "path": "sample.csv",
            "filters": [{"column": "a", "op": "eq", "value": 1}],
        }
    )
    assert validated.filters == [
        data_mcp_server.DataFilter(column="a", op="eq", value=1)
    ]

    with pytest.raises(ValueError):
        tool.fn_metadata.arg_model.model_validate(
            {
                "path": "sample.csv",
                "filters": [{"column_1": {"==": "D1-D1"}}],
            }
        )

    assert function(
        "sample.csv",
        filters=[data_mcp_server.DataFilter(column="a", op="eq", value=1)],
    ).structuredContent["rows"] == [{"x": 1}]
    assert calls[-1][2]["filters"] == [
        {"column": "a", "op": "eq", "value": 1}
    ]
    assert "Multiple filters are combined with AND" in server.instructions
    assert 'op="in"' in server.instructions


def test_create_server_rejects_unknown_access(monkeypatch) -> None:
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)

    with pytest.raises(ValueError, match="Unsupported"):
        data_mcp_server.create_server(_operations([]), access="invalid")


def test_parse_args_requires_access_and_reads_workspace_options(
    tmp_path,
    monkeypatch,
) -> None:
    config_file = tmp_path / "config.toml"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "data-mcp",
            "--project-directory",
            str(tmp_path),
            "--config-file",
            str(config_file),
            "--access",
            "write",
            "--protected-path",
            "private.csv",
        ],
    )

    args = data_mcp_server.parse_args()

    assert args.project_directory == tmp_path
    assert args.config_file == config_file
    assert args.access == "write"
    assert args.protected_path == [Path("private.csv")]


def test_parse_args_accepts_payload_only_access(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "data-mcp",
            "--project-directory",
            str(tmp_path),
            "--access",
            "none",
        ],
    )

    args = data_mcp_server.parse_args()

    assert args.access == "none"


def test_main_uses_shared_workspace_and_runs_stdio(
    tmp_path,
    monkeypatch,
) -> None:
    captured = {}
    runner = FakeFastMCP("runner")
    workspace = object()
    operations = object()

    monkeypatch.setattr(
        data_mcp_server,
        "parse_args",
        lambda: SimpleNamespace(
            project_directory=tmp_path,
            config_file=tmp_path / "config.toml",
            access="write",
            protected_path=[],
            mutation_protected_path=[tmp_path / "answer.csv"],
        ),
    )
    monkeypatch.setattr(
        data_mcp_server,
        "load_config",
        lambda path: SimpleNamespace(logging=object()),
    )
    monkeypatch.setattr(
        data_mcp_server,
        "configure_logging",
        lambda *_args, **_kwargs: None,
    )

    class WorkspaceFactory:
        @classmethod
        def from_directory(
            cls,
            directory,
            mcp_config,
            *,
            protected_paths=(),
            mutation_protected_paths=(),
        ):
            captured["workspace"] = (
                directory,
                mcp_config,
                protected_paths,
                mutation_protected_paths,
            )
            return workspace

    monkeypatch.setattr(data_mcp_server, "Workspace", WorkspaceFactory)
    monkeypatch.setattr(
        data_mcp_server,
        "DataOperations",
        lambda actual_workspace: operations
        if actual_workspace is workspace
        else None,
    )
    monkeypatch.setattr(
        data_mcp_server,
        "create_server",
        lambda actual_operations, *, access: runner
        if actual_operations is operations and access == "write"
        else None,
    )

    data_mcp_server.main()

    directory, config, protected, mutation_protected = captured["workspace"]
    assert directory == tmp_path
    assert config.name == "data"
    assert config.allow_write_files() is True
    assert protected == (tmp_path / "config.toml",)
    assert mutation_protected == (tmp_path / "answer.csv",)
    assert runner.run_transport == "stdio"


def test_main_payload_only_mode_does_not_create_workspace(
    tmp_path,
    monkeypatch,
) -> None:
    runner = FakeFastMCP("runner")
    captured = {}

    monkeypatch.setattr(
        data_mcp_server,
        "parse_args",
        lambda: SimpleNamespace(
            project_directory=tmp_path,
            config_file=None,
            access="none",
            protected_path=[],
            mutation_protected_path=[],
        ),
    )
    monkeypatch.setattr(
        data_mcp_server,
        "load_config",
        lambda path: SimpleNamespace(logging=object()),
    )
    monkeypatch.setattr(
        data_mcp_server,
        "configure_logging",
        lambda *_args, **_kwargs: None,
    )

    class WorkspaceFactory:
        @classmethod
        def from_directory(cls, *_args, **_kwargs):
            raise AssertionError("Workspace must not be created")

    monkeypatch.setattr(data_mcp_server, "Workspace", WorkspaceFactory)

    def make_operations(workspace):
        captured["workspace"] = workspace
        return object()

    monkeypatch.setattr(data_mcp_server, "DataOperations", make_operations)
    monkeypatch.setattr(
        data_mcp_server,
        "create_server",
        lambda _operations, *, access: runner
        if access == "none"
        else None,
    )

    data_mcp_server.main()

    assert captured["workspace"] is None
    assert runner.run_transport == "stdio"


def test_main_converts_workspace_initialization_error_to_system_exit(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        data_mcp_server,
        "parse_args",
        lambda: SimpleNamespace(
            project_directory=tmp_path,
            config_file=None,
            access="read",
            protected_path=[],
            mutation_protected_path=[],
        ),
    )
    monkeypatch.setattr(
        data_mcp_server,
        "load_config",
        lambda path: SimpleNamespace(logging=object()),
    )
    monkeypatch.setattr(
        data_mcp_server,
        "configure_logging",
        lambda *_args, **_kwargs: None,
    )

    class WorkspaceFactory:
        @classmethod
        def from_directory(cls, *_args, **_kwargs):
            raise data_mcp_server.WorkspaceError("blocked")

    monkeypatch.setattr(data_mcp_server, "Workspace", WorkspaceFactory)

    with pytest.raises(SystemExit, match="blocked"):
        data_mcp_server.main()
