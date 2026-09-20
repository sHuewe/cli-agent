from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from cli_agent.config import McpServerConfig
from cli_agent.okf_mcp_server import server as okf_server
from cli_agent import os_mcp_server


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


def test_okf_create_server_registers_read_only_tools_and_delegates(monkeypatch) -> None:
    calls = []

    class Repository:
        def knowledge_index(self, path):
            calls.append(("index", path))
            return {"kind": "index", "path": path}

        def knowledge_read(self, path):
            calls.append(("read", path))
            return {"kind": "document", "path": path}

    monkeypatch.setattr(okf_server, "FastMCP", FakeFastMCP)

    server = okf_server.create_server(Repository())

    assert server.name == "Open Knowledge Format"
    assert "untrusted knowledge data" in server.instructions
    assert set(server.tools) == {"knowledge_index", "knowledge_read"}
    assert server.tools["knowledge_index"](".") == {"kind": "index", "path": "."}
    assert server.tools["knowledge_read"]("concepts/a.md") == {
        "kind": "document",
        "path": "concepts/a.md",
    }
    assert calls == [("index", "."), ("read", "concepts/a.md")]
    assert "non-conforming" in server.tools["knowledge_read"].__doc__.lower()
    assert "rejected" in server.tools["knowledge_read"].__doc__.lower()


def test_okf_parse_args_reads_all_supported_options(tmp_path, monkeypatch) -> None:
    config_file = tmp_path / "config.toml"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "okf-mcp",
            "--project-directory",
            str(tmp_path),
            "--max-read-bytes",
            "12345",
            "--max-index-entries",
            "17",
            "--log-level",
            "DEBUG",
            "--config-file",
            str(config_file),
        ],
    )

    args = okf_server.parse_args()

    assert args.project_directory == tmp_path
    assert args.max_read_bytes == 12345
    assert args.max_index_entries == 17
    assert args.log_level == "DEBUG"
    assert args.config_file == config_file


def test_okf_main_configures_repository_and_runs_stdio(tmp_path, monkeypatch) -> None:
    captured = {}
    logging_config = object()
    repository = SimpleNamespace(workspace=SimpleNamespace(directory=tmp_path))
    runner = FakeFastMCP("runner")

    monkeypatch.setattr(
        okf_server,
        "parse_args",
        lambda: SimpleNamespace(
            project_directory=tmp_path,
            max_read_bytes=777,
            max_index_entries=33,
            log_level="INFO",
            config_file=tmp_path / "config.toml",
        ),
    )
    monkeypatch.setattr(
        okf_server,
        "load_config",
        lambda path: SimpleNamespace(logging=logging_config),
    )

    def fake_configure_logging(config, **kwargs):
        captured["logging"] = (config, kwargs)

    monkeypatch.setattr(okf_server, "configure_logging", fake_configure_logging)

    class RepositoryFactory:
        @classmethod
        def from_directory(cls, directory, **kwargs):
            captured["repository"] = (directory, kwargs)
            return repository

    monkeypatch.setattr(okf_server, "OkfRepository", RepositoryFactory)
    monkeypatch.setattr(okf_server, "create_server", lambda value: runner if value is repository else None)

    okf_server.main()

    assert captured["logging"][0] is logging_config
    assert captured["logging"][1]["logger"] is okf_server.logger
    assert captured["logging"][1]["default_filename"] == "cli-agent-knowledge.log"
    assert captured["repository"] == (
        tmp_path,
        {"max_read_bytes": 777, "max_index_entries": 33},
    )
    assert runner.run_transport == "stdio"


def test_okf_main_converts_repository_error_to_system_exit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        okf_server,
        "parse_args",
        lambda: SimpleNamespace(
            project_directory=tmp_path,
            max_read_bytes=100,
            max_index_entries=10,
            log_level="INFO",
            config_file=None,
        ),
    )
    monkeypatch.setattr(
        okf_server,
        "load_config",
        lambda path: SimpleNamespace(logging=object()),
    )
    monkeypatch.setattr(okf_server, "configure_logging", lambda *_args, **_kwargs: None)

    class RepositoryFactory:
        @classmethod
        def from_directory(cls, *_args, **_kwargs):
            raise okf_server.OkfRepositoryError("invalid repository")

    monkeypatch.setattr(okf_server, "OkfRepository", RepositoryFactory)

    with pytest.raises(SystemExit, match="invalid repository"):
        okf_server.main()


def _workspace_stub(calls):
    return SimpleNamespace(
        list_files=lambda path: calls.append(("list", path)) or "listed",
        read_file=lambda path: calls.append(("read", path)) or "contents",
        search_text=lambda path, text, max_results=50: calls.append(("search", path, text, max_results)) or "matches",
        find_files=lambda path, pattern, max_results=100: calls.append(("find", path, pattern, max_results)) or "files",
        file_info=lambda path: calls.append(("info", path)) or "metadata",
        write_file=lambda path, content: calls.append(("write", path, content)) or "written",
        delete_file=lambda path: calls.append(("delete", path)) or "deleted",
        make_directory=lambda path: calls.append(("mkdir", path)) or "created",
        copy_file=lambda src, dst: calls.append(("copy", src, dst)) or "copied",
        move_file=lambda src, dst: calls.append(("move", src, dst)) or "moved",
    )


def test_os_create_server_read_only_registers_only_read_tools(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(os_mcp_server, "FastMCP", FakeFastMCP)
    config = McpServerConfig(name="os", config={"allow_write_files": False})

    server = os_mcp_server.create_server(_workspace_stub(calls), config)

    assert server.name == "Workspace OS Operations"
    assert set(server.tools) == {"list_files", "read_file", "search_text", "find_files", "file_info"}
    assert server.tools["list_files"](".") == "listed"
    assert server.tools["read_file"]("README.md") == "contents"
    assert server.tools["search_text"]("src", "needle", 12) == "matches"
    assert server.tools["find_files"](".", "*.py", 15) == "files"
    assert server.tools["file_info"]("README.md") == "metadata"
    assert calls == [
        ("list", "."),
        ("read", "README.md"),
        ("search", "src", "needle", 12),
        ("find", ".", "*.py", 15),
        ("info", "README.md"),
    ]


def test_os_read_file_contract_and_pdf_description(monkeypatch) -> None:
    import inspect
    from typing import get_type_hints

    calls = []
    monkeypatch.setattr(os_mcp_server, "FastMCP", FakeFastMCP)
    server = os_mcp_server.create_server(_workspace_stub(calls), McpServerConfig(name="os"))
    read_file = server.tools["read_file"]
    assert list(inspect.signature(read_file).parameters) == ["path"]
    assert get_type_hints(read_file) == {"path": str, "return": str}
    assert "PDF" in read_file.__doc__
    assert "No OCR" in read_file.__doc__
    assert read_file("document.pdf") == "contents"
    assert calls == [("read", "document.pdf")]


def test_os_create_server_write_mode_registers_and_delegates_write_tools(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(os_mcp_server, "FastMCP", FakeFastMCP)
    config = McpServerConfig(name="os", config={"allow_write_files": True})

    server = os_mcp_server.create_server(_workspace_stub(calls), config)

    assert set(server.tools) == {
        "list_files",
        "read_file",
        "search_text",
        "find_files",
        "file_info",
        "write_file",
        "delete_file",
        "make_directory",
        "copy_file",
        "move_file",
    }
    assert server.tools["write_file"]("a.txt", "hello") == "written"
    assert server.tools["delete_file"]("a.txt") == "deleted"
    assert server.tools["make_directory"]("folder") == "created"
    assert server.tools["copy_file"]("src.txt", "dst.txt") == "copied"
    assert server.tools["move_file"]("old.txt", "new.txt") == "moved"
    assert calls == [
        ("write", "a.txt", "hello"),
        ("delete", "a.txt"),
        ("mkdir", "folder"),
        ("copy", "src.txt", "dst.txt"),
        ("move", "old.txt", "new.txt"),
    ]


def test_os_parse_args_reads_workspace_config_and_access(tmp_path, monkeypatch) -> None:
    config_file = tmp_path / "config.toml"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "os-mcp",
            "--project-directory",
            str(tmp_path),
            "--config-file",
            str(config_file),
            "--access",
            "write",
        ],
    )

    args = os_mcp_server.parse_args()

    assert args.project_directory == tmp_path
    assert args.config_file == config_file
    assert args.access == "write"


def test_find_mcp_config_returns_os_entry_and_rejects_missing() -> None:
    external = McpServerConfig(name="external", command="external")
    os_config = McpServerConfig(name="os", command="os")

    assert os_mcp_server.find_mcp_config((external, os_config)) is os_config
    with pytest.raises(os_mcp_server.WorkspaceError, match="Kein MCP-Server"):
        os_mcp_server.find_mcp_config((external,))


def test_resolve_mcp_config_explicit_access_does_not_require_configured_os() -> None:
    read_config = os_mcp_server.resolve_mcp_config((), access="read")
    write_config = os_mcp_server.resolve_mcp_config((), access="write")

    assert read_config.name == "os"
    assert read_config.allow_write_files() is False
    assert write_config.allow_write_files() is True


def test_os_main_uses_resolved_config_workspace_and_stdio(tmp_path, monkeypatch) -> None:
    captured = {}
    logging_config = object()
    runner = FakeFastMCP("runner")
    workspace = object()

    monkeypatch.setattr(
        os_mcp_server,
        "parse_args",
        lambda: SimpleNamespace(
            project_directory=tmp_path,
            config_file=tmp_path / "config.toml",
            access="write",
        ),
    )
    monkeypatch.setattr(
        os_mcp_server,
        "load_config",
        lambda path: SimpleNamespace(logging=logging_config, mcp_servers=()),
    )

    def fake_configure_logging(config, **kwargs):
        captured["logging"] = (config, kwargs)

    monkeypatch.setattr(os_mcp_server, "configure_logging", fake_configure_logging)

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

    monkeypatch.setattr(os_mcp_server, "Workspace", WorkspaceFactory)
    monkeypatch.setattr(
        os_mcp_server,
        "create_server",
        lambda actual_workspace, mcp_config: runner
        if actual_workspace is workspace and mcp_config.allow_write_files()
        else None,
    )

    os_mcp_server.main()

    assert captured["logging"][0] is logging_config
    assert captured["logging"][1]["logger"] is os_mcp_server.logger
    assert captured["logging"][1]["default_filename"] == "cli-agent-os-mcp.log"
    assert captured["workspace"][0] == tmp_path
    assert captured["workspace"][1].allow_write_files() is True
    assert captured["workspace"][2] == (tmp_path / "config.toml",)
    assert captured["workspace"][3] == ()
    assert runner.run_transport == "stdio"


def test_os_main_protects_effective_default_config_when_config_flag_is_omitted(tmp_path, monkeypatch) -> None:
    captured = {}
    default_config = tmp_path / "cli-agent" / "config.toml"
    runner = FakeFastMCP("runner")
    workspace = object()

    monkeypatch.setattr(
        os_mcp_server,
        "parse_args",
        lambda: SimpleNamespace(
            project_directory=tmp_path,
            config_file=None,
            access="read",
        ),
    )
    monkeypatch.setattr(os_mcp_server, "default_config_file", lambda: default_config)
    monkeypatch.setattr(
        os_mcp_server,
        "load_config",
        lambda path: SimpleNamespace(logging=object(), mcp_servers=()),
    )
    monkeypatch.setattr(os_mcp_server, "configure_logging", lambda *_args, **_kwargs: None)

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

    monkeypatch.setattr(os_mcp_server, "Workspace", WorkspaceFactory)
    monkeypatch.setattr(os_mcp_server, "create_server", lambda *_args: runner)

    os_mcp_server.main()

    assert captured["workspace"][0] == tmp_path
    assert captured["workspace"][2] == (default_config,)
    assert captured["workspace"][3] == ()
    assert runner.run_transport == "stdio"


def test_os_main_converts_workspace_error_to_system_exit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        os_mcp_server,
        "parse_args",
        lambda: SimpleNamespace(
            project_directory=tmp_path,
            config_file=None,
            access=None,
        ),
    )
    monkeypatch.setattr(
        os_mcp_server,
        "load_config",
        lambda path: SimpleNamespace(logging=object(), mcp_servers=()),
    )
    monkeypatch.setattr(os_mcp_server, "configure_logging", lambda *_args, **_kwargs: None)

    with pytest.raises(SystemExit, match="Kein MCP-Server"):
        os_mcp_server.main()
