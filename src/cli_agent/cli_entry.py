from __future__ import annotations

import asyncio
import sys
from dataclasses import replace

from . import cli
from .config import McpServerConfig
from .git_operations import GitWorkspace

GIT_MCP_SERVER_NAME = "git"


def _git_server_config() -> McpServerConfig:
    return McpServerConfig(
        name=GIT_MCP_SERVER_NAME,
        transport="stdio",
        command="{python}",
        args=(
            "-m",
            "cli_agent.git_mcp_server",
            "--project-directory",
            "{workspace_directory}",
            "--config-file",
            "{config_file}",
        ),
        built_in=True,
    )


def main() -> None:
    raw_args = sys.argv[1:]
    if raw_args and raw_args[0] == "admin":
        cli.main()
        return

    parser = cli.build_parser()
    parser.add_argument(
        "--with-git-read",
        action="store_true",
        help=(
            "Enable the built-in read-only Git MCP server when at least one "
            "validated Git repository is fully contained in the workspace."
        ),
    )
    args = parser.parse_args(raw_args)

    original_load_config = cli.load_config

    def load_config_with_git(path):
        config = original_load_config(path)
        if not args.with_git_read:
            return config
        workspace = GitWorkspace.from_directory(args.workspace)
        servers = tuple(server for server in config.mcp_servers if server.name != GIT_MCP_SERVER_NAME)
        if workspace.repositories:
            servers += (_git_server_config(),)
        return replace(config, mcp_servers=servers)

    cli.load_config = load_config_with_git
    try:
        asyncio.run(cli.run(args))
    except Exception as exc:
        cli.print_error(exc, debug=bool(getattr(args, "debug", False)))
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass
    finally:
        cli.load_config = original_load_config


if __name__ == "__main__":
    main()
