from __future__ import annotations

import sys
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
        inspect_data=lambda path, sample_rows=5: (
            calls.append(("inspect", path, sample_rows)) or "inspected"
        ),
        select_data=lambda path, **kwargs: (
            calls.append(("select", path, kwargs)) or "selected"
        ),
        value_counts=lambda path, column, **kwargs: (
            calls.append(("counts", path, column, kwargs)) or "counted"
        ),
        aggregate_data=lambda path, **kwargs: (
            calls.append(("aggregate", path, kwargs)) or "aggregated"
        ),
        select_data_to_file=lambda input_path, output_path, **kwargs: (
            calls.append(("select_write", input_path, output_path, kwargs))
            or "selected-written"
        ),
        aggregate_data_to_file=lambda input_path, output_path, **kwargs: (
            calls.append(("aggregate_write", input_path, output_path, kwargs))
            or "aggregated-written"
        ),
    )


def test_create_server_read_mode_exposes_only_non_mutating_data_tools(
    monkeypatch,
) -> None:
    calls = []
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)

    server = data_mcp_server.create_server(
        _operations(calls),
        allow_write=False,
    )

    assert server.name == "Workspace Data Operations"
    assert "untrusted data" in server.instructions
    assert set(server.tools) == {
        "inspect_data",
        "select_data",
        "value_counts",
        "aggregate_data",
    }
    assert server.tools["inspect_data"]("data.csv", 3) == "inspected"
    assert (
        server.tools["value_counts"](
            "data.csv",
            "kind",
            filters=None,
            limit=7,
        )
        == "counted"
    )
    assert calls == [
        ("inspect", "data.csv", 3),
        (
            "counts",
            "data.csv",
            "kind",
            {"filters": None, "limit": 7},
        ),
    ]


def test_create_server_write_mode_adds_only_derived_data_mutations(
    monkeypatch,
) -> None:
    calls = []
    monkeypatch.setattr(data_mcp_server, "FastMCP", FakeFastMCP)

    server = data_mcp_server.create_server(
        _operations(calls),
        allow_write=True,
    )

    assert set(server.tools) == {
        "inspect_data",
        "select_data",
        "value_counts",
        "aggregate_data",
        "select_data_to_file",
        "aggregate_data_to_file",
    }
    assert (
        server.tools["select_data_to_file"](
            "raw.csv",
            "filtered.csv",
            columns=["x"],
        )
        == "selected-written"
    )
    assert (
        server.tools["aggregate_data_to_file"](
            "raw.csv",
            "summary.csv",
            [{"column": "x", "function": "sum"}],
        )
        == "aggregated-written"
    )
    assert calls[0][0] == "select_write"
    assert calls[1][0] == "aggregate_write"


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
        lambda actual_operations, *, allow_write: runner
        if actual_operations is operations and allow_write is True
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
