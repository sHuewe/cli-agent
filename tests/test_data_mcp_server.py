from __future__ import annotations

import inspect
import sys

from mcp.server.fastmcp.tools.base import Tool
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent import data_mcp_server
from cli_agent.data_operations import DataOperationError


class FakeFastMCP:
    def __init__(self, name: str, *, instructions: str | None = None) -> None:
        self.name = name
        self.instructions = instructions
        self.tools: dict[str, object] = {}
        self.run_transport: str | None = None

    def tool(self):
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
        extract_markdown_tables=lambda markdown: (
            calls.append(("extract_markdown_tables", markdown)) or "tables"
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
        value_counts=lambda column, path=None, **kwargs: (
            calls.append(("counts", column, path, kwargs)) or "counted"
        ),
        aggregate_data=lambda aggregations, path=None, **kwargs: (
            calls.append(("aggregate", aggregations, path, kwargs))
            or "aggregated"
        ),
        select_data_to_file=lambda output_path, input_path=None, **kwargs: (
            calls.append(("select_write", output_path, input_path, kwargs))
            or "selected-written"
        ),
        aggregate_data_to_file=lambda output_path, aggregations, input_path=None, **kwargs: (
            calls.append(
                ("aggregate_write", output_path, aggregations, input_path, kwargs)
            )
            or "aggregated-written"
        ),
    )


BASE_TOOLS = {
    "calculate",
    "extract_markdown_tables",
    "inspect_schema",
    "inspect_data",
    "select_data",
    "value_counts",
    "aggregate_data",
}


def test_payload_only_mode_hides_path_and_write_capabilities(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)

    server = data_mcp_server.create_server(
        _operations(calls),
        access="none",
    )

    assert set(server.tools) == BASE_TOOLS
    assert "path" not in inspect.signature(server.tools["inspect_schema"]).parameters
    assert "path" not in inspect.signature(server.tools["inspect_data"]).parameters
    assert "path" not in inspect.signature(server.tools["select_data"]).parameters
    assert "path" not in inspect.signature(server.tools["value_counts"]).parameters
    assert "path" not in inspect.signature(server.tools["aggregate_data"]).parameters
    assert "Markdown is preferred" in server.instructions
    assert "Workspace file access is not available" in server.instructions
    assert "use only\nnormalized tabular output returned by Data MCP tools" in server.instructions
    assert "Use select_data whenever a subset" in server.instructions
    assert "Dependent Data MCP calls must wait" in server.instructions
    assert "Use inspect_schema to discover column names" in server.instructions
    assert "Do not use inspect_data as a substitute" in server.instructions

    assert (
        server.tools["select_data"](
            "| a | b |\n|---|---|\n| 1 | 2 |",
            limit=1,
        )
        == "selected"
    )
    assert calls == [
        (
            "select",
            None,
            {
                "data": "| a | b |\n|---|---|\n| 1 | 2 |",
                "data_format": "markdown",
                "table_index": 0,
                "columns": None,
                "filters": None,
                "sort_by": None,
                "descending": False,
                "limit": 1,
            },
        )
    ]


def test_read_mode_exposes_path_but_not_write_tools(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)

    server = data_mcp_server.create_server(
        _operations(calls),
        access="read",
    )

    assert set(server.tools) == BASE_TOOLS
    assert "path" in inspect.signature(server.tools["inspect_schema"]).parameters
    assert "path" in inspect.signature(server.tools["inspect_data"]).parameters
    assert "path" in inspect.signature(server.tools["select_data"]).parameters
    assert "input_path" not in inspect.signature(server.tools["select_data"]).parameters
    assert "Prefer a workspace path" in server.instructions

    assert server.tools["inspect_schema"](
        path="data.csv",
        distinct_values_limit=7,
    ) == "schema"
    assert server.tools["inspect_data"](
        path="data.csv",
        sample_rows=3,
    ) == "inspected"
    assert calls == [
        (
            "schema",
            "data.csv",
            {
                "data": None,
                "data_format": None,
                "table_index": 0,
                "distinct_values_limit": 7,
            },
        ),
        (
            "inspect",
            "data.csv",
            {
                "data": None,
                "data_format": None,
                "table_index": 0,
                "sample_rows": 3,
            },
        )
    ]


def test_write_mode_adds_file_tools_and_prefers_path_workflows(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)

    server = data_mcp_server.create_server(
        _operations(calls),
        access="write",
    )

    assert set(server.tools) == BASE_TOOLS | {
        "select_data_to_file",
        "aggregate_data_to_file",
    }
    assert "path" in inspect.signature(server.tools["inspect_data"]).parameters
    assert "input_path" in inspect.signature(
        server.tools["select_data_to_file"]
    ).parameters
    assert "Prefer path-based datasets" in server.instructions
    assert "*_to_file" in server.instructions

    assert (
        server.tools["select_data_to_file"](
            "filtered.md",
            input_path="raw.csv",
            columns=["x"],
        )
        == "selected-written"
    )
    assert calls == [
        (
            "select_write",
            "filtered.md",
            "raw.csv",
            {
                "data": None,
                "data_format": None,
                "table_index": 0,
                "columns": ["x"],
                "filters": None,
                "sort_by": None,
                "descending": False,
            },
        )
    ]


def test_extract_tool_description_and_result_are_markdown_oriented(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)
    server = data_mcp_server.create_server(_operations(calls), access="none")

    tool = server.tools["extract_markdown_tables"]
    assert "normalize" in (tool.__doc__ or "").lower()
    assert tool("| a |\n|---|\n| 1 |") == "tables"
    assert calls == [
        ("extract_markdown_tables", "| a |\n|---|\n| 1 |")
    ]


def test_inspection_tool_descriptions_separate_schema_from_rows(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)
    server = data_mcp_server.create_server(_operations(calls), access="read")

    schema_doc = (server.tools["inspect_schema"].__doc__ or "").lower()
    preview_doc = (server.tools["inspect_data"].__doc__ or "").lower()

    assert "never returns dataset rows" in schema_doc
    assert "distinct_values_complete" in schema_doc
    assert "select_data" in schema_doc
    assert "complete sample rows" in preview_doc
    assert "inspect_schema" in preview_doc
    assert "select_data" in preview_doc


def test_select_data_exposes_structured_filter_schema(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)
    server = data_mcp_server.create_server(_operations(calls), access="none")

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
            "data": "| a |\n|---|\n| 1 |",
            "filters": [{"column": "a", "op": "eq", "value": 1}],
        }
    )
    assert validated.filters == [
        data_mcp_server.DataFilter(column="a", op="eq", value=1)
    ]

    with pytest.raises(ValueError):
        tool.fn_metadata.arg_model.model_validate(
            {
                "data": "| a |\n|---|\n| 1 |",
                "filters": [{"column_1": {"==": "D1-D1"}}],
            }
        )

    assert function(
        "| a |\n|---|\n| 1 |",
        filters=[data_mcp_server.DataFilter(column="a", op="eq", value=1)],
    ) == "selected"
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
