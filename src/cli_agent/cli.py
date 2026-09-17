from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import traceback
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .admin_config import AdminConfig, default_admin_config_file, load_admin_config
from .agent import CliAgent
from .approval_display import approval_arguments as _approval_arguments
from .config import AppConfig, McpServerConfig, default_config_file, load_config
from .file_context import (
    ContextFileCliAgent as WebContextCliAgent,
    is_local_agent_command,
    prepare_file_options,
)
from .git_runtime import RuntimeGitWorkspace
from .logging_setup import configure_logging
from .mcp_contracts import tool_contract_fingerprint
from .model_factory import create_model_client

OS_MCP_SERVER_NAME = "os"
GIT_MCP_SERVER_NAME = "git"
REDACTED_CONFIG_VALUE = "<WERT AUS KONFIGURATION ÜBERNEHMEN>"


@dataclass(frozen=True)
class CliArgs:
    config: Path
    admin_config: Path
    model: str | None
    os_access: str | None
    with_git_read: bool
    directory: Path | None
    web: str | None
    file: tuple[str, ...]
    verbose: bool
    print_config: bool


def parse_args(argv: list[str] | None = None) -> CliArgs:
    parser = argparse.ArgumentParser(description="CLI agent with MCP tool support")
    parser.add_argument("--config", type=Path, default=default_config_file())
    parser.add_argument("--admin-config", type=Path, default=default_admin_config_file())
    parser.add_argument("--model")
    os_group = parser.add_mutually_exclusive_group()
    os_group.add_argument("--with-os-read", action="store_true")
    os_group.add_argument("--with-os-write", action="store_true")
    parser.add_argument("--with-git-read", action="store_true")
    parser.add_argument("--directory", type=Path)
    parser.add_argument("--web")
    parser.add_argument("--file", action="append", default=[])
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--print-config", action="store_true")
    args = parser.parse_args(argv)
    os_access = "write" if args.with_os_write else "read" if args.with_os_read else None
    return CliArgs(
        config=args.config,
        admin_config=args.admin_config,
        model=args.model,
        os_access=os_access,
        with_git_read=args.with_git_read,
        directory=args.directory,
        web=args.web,
        file=tuple(args.file),
        verbose=args.verbose,
        print_config=args.print_config,
    )


def _resolve_workspace(directory: Path | None) -> Path:
    return (directory or Path.cwd()).expanduser().resolve()


def _os_mcp_server_config(access: str) -> McpServerConfig:
    if access not in {"read", "write"}:
        raise ValueError(f"Unsupported OS MCP access mode: {access!r}")
    return McpServerConfig(name=OS_MCP_SERVER_NAME, transport="stdio", command="{python}", args=("-m", "cli_agent.os_mcp_server", "--project-directory", "{workspace_directory}", "--config-file", "{config_file}", "--access", access), config={"allow_write_files": access == "write"}, built_in=True)


def _git_mcp_server_config() -> McpServerConfig:
    return McpServerConfig(name=GIT_MCP_SERVER_NAME, transport="stdio", command="{python}", args=("-m", "cli_agent.git_mcp_server", "--project-directory", "{workspace_directory}", "--config-file", "{config_file}"), built_in=True)


def apply_mcp_cli_overrides(config: AppConfig, *, os_access: str | None, git_read: bool = False, workspace: Path | None = None) -> AppConfig:
    servers = config.mcp_servers
    changed = False
    if os_access is not None:
        servers = tuple(server for server in servers if server.name != OS_MCP_SERVER_NAME) + (_os_mcp_server_config(os_access),)
        changed = True
    if git_read:
        if workspace is None:
            raise ValueError("--with-git-read benötigt einen aufgelösten Workspace.")
        servers = tuple(server for server in servers if server.name != GIT_MCP_SERVER_NAME)
        git_workspace = RuntimeGitWorkspace.from_directory(workspace)
        if git_workspace.repositories:
            servers += (_git_mcp_server_config(),)
        changed = True
    return replace(config, mcp_servers=servers) if changed else config


def apply_model_cli_override(config: AppConfig, *, model: str | None) -> AppConfig:
    if model is None:
        return config
    return replace(config, model=replace(config.model, model=model))


def _config_for_display(config: AppConfig) -> dict[str, Any]:
    payload = config.model_dump(mode="json")
    model = payload.get("model")
    if isinstance(model, dict) and model.get("api_key"):
        model["api_key"] = REDACTED_CONFIG_VALUE
    for server in payload.get("mcp_servers", []):
        if isinstance(server, dict):
            env = server.get("env")
            if isinstance(env, dict):
                for key in env:
                    env[key] = REDACTED_CONFIG_VALUE
            headers = server.get("headers")
            if isinstance(headers, dict):
                for key in headers:
                    headers[key] = REDACTED_CONFIG_VALUE
    return payload


def _print_config(config: AppConfig) -> None:
    print(json.dumps(_config_for_display(config), ensure_ascii=False, indent=2))


async def _run_agent(
    config: AppConfig,
    *,
    admin_config: AdminConfig,
    workspace: Path,
    web: str | None,
    files: tuple[str, ...],
) -> None:
    model = create_model_client(config.model)
    agent = CliAgent(config, model, workspace_directory=workspace, admin_config=admin_config)
    context_agent = WebContextCliAgent(agent)
    await context_agent.initialize()
    try:
        prepared = prepare_file_options(files, workspace)
        if web is not None:
            await context_agent.add_web_context(web)
        for path in prepared:
            await context_agent.add_file_context(path)
        while True:
            try:
                user_input = await asyncio.to_thread(input, "> ")
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not user_input.strip():
                continue
            if is_local_agent_command(user_input):
                handled = await context_agent.handle_local_command(user_input)
                if handled:
                    continue
            response = await context_agent.run(user_input)
            print(response)
    finally:
        await context_agent.close()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    configure_logging(verbose=args.verbose)
    try:
        config = load_config(args.config)
        admin_config = load_admin_config(args.admin_config)
        workspace = _resolve_workspace(args.directory)
        config = apply_model_cli_override(config, model=args.model)
        config = apply_mcp_cli_overrides(
            config,
            os_access=args.os_access,
            git_read=args.with_git_read,
            workspace=workspace,
        )
        if args.print_config:
            _print_config(config)
            return 0
        asyncio.run(
            _run_agent(
                config,
                admin_config=admin_config,
                workspace=workspace,
                web=args.web,
                files=args.file,
            )
        )
        return 0
    except Exception as exc:
        logging.getLogger(__name__).error("%s", exc)
        if args.verbose:
            traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
