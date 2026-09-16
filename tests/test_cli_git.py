from __future__ import annotations

import subprocess
from pathlib import Path

from cli_agent.cli import apply_mcp_cli_overrides, build_parser
from cli_agent.config import AppConfig, McpServerConfig


def _git(directory: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, check=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")


def test_with_git_read_argument_is_parsed() -> None:
    args = build_parser().parse_args(["--with-git-read"])
    assert args.with_git_read is True


def test_with_git_read_adds_builtin_server_for_nested_repository(tmp_path: Path) -> None:
    _init_repo(tmp_path / "app")

    config = apply_mcp_cli_overrides(
        AppConfig(),
        os_access=None,
        git_read=True,
        workspace=tmp_path,
    )

    assert [server.name for server in config.mcp_servers] == ["git"]
    server = config.mcp_servers[0]
    assert server.built_in is True
    assert server.command == "{python}"
    assert server.args[:2] == ("-m", "cli_agent.git_mcp_server")
    assert "--project-directory" in server.args


def test_with_git_read_exposes_no_git_server_without_repository(tmp_path: Path) -> None:
    original = AppConfig(
        mcp_servers=(
            McpServerConfig(name="external", command="external-server"),
            McpServerConfig(name="git", command="configured-git-server"),
        )
    )

    config = apply_mcp_cli_overrides(
        original,
        os_access=None,
        git_read=True,
        workspace=tmp_path,
    )

    assert [server.name for server in config.mcp_servers] == ["external"]


def test_git_read_does_not_enable_os_read(tmp_path: Path) -> None:
    _init_repo(tmp_path)

    config = apply_mcp_cli_overrides(
        AppConfig(),
        os_access=None,
        git_read=True,
        workspace=tmp_path,
    )

    assert [server.name for server in config.mcp_servers] == ["git"]
