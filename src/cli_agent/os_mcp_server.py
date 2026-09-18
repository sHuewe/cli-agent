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
Only write, move or delete a file when the user requested a file change.
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
        Read one UTF-8 text file or extract text from a PDF in the project workspace.

        Supports common source, configuration, script, markup and text file
        types (up to 1 MB), plus PDFs (up to 10 MB, 100 pages, 1,000,000 output
        characters and 20 seconds of extraction time). PDF output includes page
        markers and notices for pages without extractable text. No OCR is used;
        images and diagrams are not interpreted, and table/column layout may be
        lost. Encrypted PDFs, secret/credential files and other binary formats
        are rejected. Limits fail explicitly rather than silently truncating.

        Args:
            path: File relative to the project workspace. Absolute paths and
                ".." are forbidden.
        """
        return workspace.read_file(path)

    @mcp.tool()
    def search_text(path: str, text: str, max_results: int = 50) -> str:
        """
        Search literal text in allowed UTF-8 text files below a workspace path.

        The search is recursive for directories, does not follow symlinks,
        excludes sensitive files and directories, skips hardlinked or oversized
        files, and returns path, line number, first match column and text.
        Columns are 1-based character positions. Long lines return an excerpt
        containing the match, with text_truncated and text_start_column metadata.
        Regex is not supported. Results are capped; truncation is reported.

        Args:
            path: Workspace-relative file or directory to search.
            text: Literal text to search for.
            max_results: Maximum number of matches, from 1 to 200.
        """
        return workspace.search_text(path, text, max_results)

    @mcp.tool()
    def find_files(path: str, pattern: str, max_results: int = 100) -> str:
        """
        Find files recursively below a workspace-relative path using a glob pattern.

        The pattern is matched against both the file name and workspace-relative
        path. Symlinks, sensitive paths and hardlinked files are excluded. Results
        are capped; truncation is reported.

        Args:
            path: Workspace-relative file or directory to search below.
            pattern: Glob pattern such as "*.py" or "src/*.java".
            max_results: Maximum number of files, from 1 to 500.
        """
        return workspace.find_files(path, pattern, max_results)

    @mcp.tool()
    def file_info(path: str) -> str:
        """
        Return safe metadata for one workspace-relative file or directory.

        Returns path, type, file size and modification timestamp. Sensitive files,
        hardlinked files and paths outside the workspace are rejected.

        Args:
            path: File or directory relative to the project workspace.
        """
        return workspace.file_info(path)

    if mcp_config.allow_write_files():

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
                path_src: Source file relative to the project workspace.
                    Absolute paths and ".." are forbidden.
                path_dst: Destination file relative to the project workspace.
                    Absolute paths and ".." are forbidden.
            """
            return workspace.copy_file(path_src, path_dst)

        @mcp.tool()
        def move_file(path_src: str, path_dst: str) -> str:
            """
            Move or rename a file within the project workspace.

            Existing destination files may be replaced, but sensitive files,
            hardlinked files, paths outside the workspace, absolute paths and
            ".." are rejected. The destination parent directory must exist.

            Args:
                path_src: Source file relative to the project workspace.
                path_dst: Destination file relative to the project workspace.
            """
            return workspace.move_file(path_src, path_dst)

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
    parser.add_argument(
        "--access",
        choices=("read", "write"),
        default=None,
        help=(
            "Explicit access mode. When set, it overrides the 'os' MCP "
            "configuration from the config file."
        ),
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


def resolve_mcp_config(
    configurations: tuple[McpServerConfig, ...],
    *,
    access: str | None,
) -> McpServerConfig:
    if access is None:
        return find_mcp_config(configurations)
    return McpServerConfig(
        name="os",
        config={"allow_write_files": access == "write"},
    )


def main() -> None:
    args = parse_args()
    config = load_config(path=args.config_file)
    configure_logging(
        config.logging,
        logger=logger,
        default_filename="cli-agent-os-mcp.log",
    )

    logger.info(
        "MCP server starting project_directory=%s access=%s",
        args.project_directory,
        args.access or "config",
    )
    try:
        mcp_config = resolve_mcp_config(
            config.mcp_servers,
            access=args.access,
        )
        workspace = Workspace.from_directory(
            args.project_directory,
            mcp_config,
            protected_paths=(args.config_file,) if args.config_file is not None else (),
        )
    except WorkspaceError as exc:
        logger.exception("MCP server initialization failed")
        raise SystemExit(str(exc)) from exc

    create_server(workspace, mcp_config).run(transport="stdio")


if __name__ == "__main__":
    main()
