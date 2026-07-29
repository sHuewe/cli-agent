from __future__ import annotations

import argparse
import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import McpServerConfig, load_config
from .logging_setup import configure_logging
from .os_operations import Workspace, WorkspaceError

logger = logging.getLogger(__name__)


def create_server(
    workspace: Workspace,
    mcp_config: McpServerConfig,
) -> FastMCP:
    instructions = """\
Use these tools only for files and directories below the project workspace
fixed when this server started. All tool paths must be relative to that
workspace. Never use absolute paths or '..'. Read files before changing them.
Only write a file when the user requested a file change.
"""
    logger.info("MCP server instructions: %s", instructions)
    mcp = FastMCP(
        "Workspace OS Operations",
        instructions=instructions,
    )

    @mcp.tool()
    def list_files(path: str) -> str:
        """
        List files and directories directly below a workspace-relative path.

        Args:
            path: Directory relative to the project workspace. Use "." for
                the workspace root. Absolute paths and ".." are forbidden.
        """
        return workspace.list_files(path)

    @mcp.tool()
    def read_file(path: str) -> str:
        """
        Read one UTF-8 text file from the project workspace.

        Supports common source, configuration, script, markup and text file
        types. Binary files are rejected.

        Args:
            path: File relative to the project workspace. Absolute paths and
                ".." are forbidden.
        """
        return workspace.read_file(path)

    if mcp_config.config.get("allow_write_files", False):

        @mcp.tool()
        def write_file(path: str, content: str) -> str:
            """
            Create or completely overwrite one UTF-8 text file.

            Use only when the user explicitly requests a file change. The
            parent directory must already exist. Read an existing file before
            overwriting it.

            Args:
                path: File relative to the project workspace. Absolute paths
                    and ".." are forbidden.
                content: Complete new UTF-8 text content of the file.
            """
            return workspace.write_file(path, content)

        @mcp.tool()
        def delete_file(path: str) -> str:
            """
            Delete a file from the project workspace.

            Args:
                path: File relative to the project workspace. Absolute paths
                    and ".." are forbidden.
            """
            return workspace.delete_file(path)

        @mcp.tool()
        def make_directory(path: str) -> str:
            """
            Create a directory within the project workspace.

            Args:
                path: Directory relative to the project workspace. Absolute paths
                    and ".." are forbidden.
            """
            return workspace.make_directory(path)


        @mcp.tool()
        def copy_file(path_src: str, path_dst: str) -> str:
            """
            Copy a file within the project workspace.

            Args:
                path_src: Source file relative to the project workspace. Absolute paths
                    and ".." are forbidden.
                path_dst: Destination file relative to the project workspace. Absolute paths
                    and ".." are forbidden.
            """
            return workspace.copy_file(path_src, path_dst)
   
                
    return mcp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MCP server for workspace OS operations"
    )
    parser.add_argument(
        "--project-directory",
        required=True,
        type=Path,
        help="Project workspace directory fixed for this MCP process",
    )
    parser.add_argument(
        "--config-file",
        type=Path,
        default=None,
        help="Configuration file",
    )
    return parser.parse_args()


def find_mcp_config(
    configurations: tuple[McpServerConfig, ...],
) -> McpServerConfig:
    for mcp_server in configurations:
        if mcp_server.name == "os":
            return mcp_server
    raise WorkspaceError(
        "Kein MCP-Server mit dem Namen 'os' in der Konfiguration gefunden."
    )


def main() -> None:
    args = parse_args()
    config = load_config(path=args.config_file)
    configure_logging(
        config.logging,
        default_filename="cli-agent-os-mcp.log",
    )

    logger.info(
        "MCP server starting project_directory=%s",
        args.project_directory,
    )
    try:
        mcp_config = find_mcp_config(config.mcp_servers)
        workspace = Workspace.from_directory(
            args.project_directory,
            mcp_config,
        )
    except WorkspaceError as exc:
        logger.exception("MCP server initialization failed")
        raise SystemExit(str(exc)) from exc

    create_server(workspace, mcp_config).run(transport="stdio")


if __name__ == "__main__":
    main()
