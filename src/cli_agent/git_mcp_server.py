from __future__ import annotations

import argparse
import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from .config import load_config
from .git_operations import GitWorkspace, GitWorkspaceError
from .logging_setup import configure_logging

logger = logging.getLogger(__name__)


def create_server(workspace: GitWorkspace) -> FastMCP:
    instructions = """Use these read-only Git tools only for repositories discovered inside the fixed project workspace. Repository paths are workspace-relative. Never infer or access repositories above or outside the workspace."""
    mcp = FastMCP("Workspace Git Read Operations", instructions=instructions)

    @mcp.tool()
    def git_repositories() -> str:
        """List validated Git repositories inside the project workspace."""
        return workspace.git_repositories()

    @mcp.tool()
    def git_status(repository: str) -> str:
        """Show branch and working-tree status for one workspace-relative repository path."""
        return workspace.git_status(repository)

    @mcp.tool()
    def git_current_branch(repository: str) -> str:
        """Return the current branch, or detached HEAD state, for one repository."""
        return workspace.git_current_branch(repository)

    @mcp.tool()
    def git_branches(repository: str) -> str:
        """List local branches for one repository."""
        return workspace.git_branches(repository)

    @mcp.tool()
    def git_diff(repository: str, path: str | None = None) -> str:
        """Return unstaged diff, optionally limited to one repository-relative path."""
        return workspace.git_diff(repository, path)

    @mcp.tool()
    def git_diff_staged(repository: str, path: str | None = None) -> str:
        """Return staged diff, optionally limited to one repository-relative path."""
        return workspace.git_diff_staged(repository, path)

    @mcp.tool()
    def git_log(repository: str, max_count: int = 20) -> str:
        """Return recent commits with author, date, hash and subject. max_count is 1..200."""
        return workspace.git_log(repository, max_count)

    @mcp.tool()
    def git_commit_info(repository: str, commit_hash: str) -> str:
        """Return metadata for a commit identified by hexadecimal commit hash."""
        return workspace.git_commit_info(repository, commit_hash)

    @mcp.tool()
    def git_commit_diff(repository: str, commit_hash: str, path: str | None = None) -> str:
        """Return a commit patch, optionally limited to one repository-relative path."""
        return workspace.git_commit_diff(repository, commit_hash, path)

    @mcp.tool()
    def git_file_history(repository: str, path: str, max_count: int = 20) -> str:
        """Return file history including authors and commits; follows renames when possible."""
        return workspace.git_file_history(repository, path, max_count)

    @mcp.tool()
    def git_commit_files(repository: str, commit_hash: str) -> str:
        """Return files changed by one commit, including change type and rename source when applicable."""
        return workspace.git_commit_files(repository, commit_hash)

    return mcp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Built-in read-only Git MCP server")
    parser.add_argument("--project-directory", required=True, type=Path)
    parser.add_argument("--config-file", type=Path, default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(path=args.config_file)
    configure_logging(config.logging, logger=logger, default_filename="cli-agent-git-mcp.log")
    try:
        workspace = GitWorkspace.from_directory(args.project_directory)
    except GitWorkspaceError as exc:
        raise SystemExit(str(exc)) from exc
    if not workspace.repositories:
        raise SystemExit("Kein zulässiges Git-Repository innerhalb des Projekt-Workspaces gefunden.")
    create_server(workspace).run(transport="stdio")


if __name__ == "__main__":
    main()
