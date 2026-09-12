from __future__ import annotations

import argparse
import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .compose import ComposeError, ComposeProject
from .config import McpServerConfig, load_config
from .logging_setup import configure_logging

logger = logging.getLogger(__name__)


def create_server(project: ComposeProject, mcp_config: McpServerConfig) -> FastMCP:
    instructions = ""
    if project.is_available():
        instructions = """\
Use these tools only for the Docker Compose project fixed when this server
started. Inspect the Compose file, service status, and logs when information is
missing. Never invent service names. Service-control tools have real effects:
only start, stop, or restart services when the user requested that action.
After an operational action, inspect the service status to verify the result.
"""
    logger.info("MCP server instructions: %s", instructions)
    mcp = FastMCP(
        "Docker Compose",
        instructions=instructions,
    )

    if not project.is_available():
        return mcp

    @mcp.tool()
    def get_compose_file() -> str:
        """Return the Compose YAML selected by the user at agent startup."""
        return project.read_compose_file()

    @mcp.tool()
    def compose_ps() -> str:
        """Show the current status of services in the selected Compose project."""
        return project.ps()

    if mcp_config.allow_modify_services():

        @mcp.tool()
        def compose_up_all() -> str:
            """
            Start all services of the Docker Compose project.

            Use this tool only when the user explicitly asks to start the
            complete project, the entire Compose stack, or all services.
            Never use it when the user names only one specific service.
            """
            return project.up_all()

        @mcp.tool()
        def compose_up(service_name: str) -> str:
            """
            Start exactly one Docker Compose service.

            Use this tool when the user names a specific service.
            Do not use compose_up_all() in that case.

            Args:
                service_name: Exact name of the single service to start.
            """
            return project.start(service_name)

        @mcp.tool()
        def compose_down(service_name: str) -> str:
            """
            Stop exactly one existing Docker Compose service.

            Args:
                service_name: Exact name of the single service to stop.
            """
            return project.stop(service_name)

        @mcp.tool()
        def compose_restart(service_name: str) -> str:
            """
            Calls docker compose restart for one existing Compose service.

            Args:
                service_name: Exact name of the single service to restart.
            """
            return project.restart(service_name)

    @mcp.tool()
    def compose_logs(service_name: str) -> str:
        """Return the last 200 log lines for one service in the selected project.

        Args:
            service_name: A service name defined in the selected Compose file.
        """
        return project.logs(service_name)

    return mcp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MCP server for Compose tools")
    parser.add_argument(
        "--project-directory",
        required=True,
        type=Path,
        help="Compose project directory fixed for this MCP process",
    )
    parser.add_argument(
        "--config-file", type=Path, default=None, help=("Configuration file ")
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(path=args.config_file)
    mcp_config = McpServerConfig(name="compose")
    for mcp_server in config.mcp_servers:
        if mcp_server.name == "compose":
            mcp_config = mcp_server
            break
    configure_logging(
        config.logging,
        default_filename="cli-agent-compose-mcp.log",
    )
    logger = logging.getLogger(__name__)
    logger.info(
        "MCP server starting project_directory=%s",
        args.project_directory,
    )
    try:
        project = ComposeProject.from_directory(args.project_directory, mcp_config)
    except ComposeError as exc:
        logger.exception("MCP server initialization failed")
        raise SystemExit(str(exc)) from exc

    create_server(project, mcp_config).run(transport="stdio")


if __name__ == "__main__":
    main()
