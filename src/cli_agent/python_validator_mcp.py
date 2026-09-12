from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP

from .config import load_config
from .logging_setup import configure_logging
from .python_validator import DockerPythonValidator, ValidatorSettings

logger = logging.getLogger(__name__)


def create_server(validator: DockerPythonValidator) -> FastMCP:
    mcp = FastMCP(
        "Python Docker Validator",
        instructions="""\
Use validate_python_project after changing Python code when build/start
validation is relevant. Select an actual Python file or module as entrypoint.
Treat success only as evidence that dependencies installed and the process
started; it is not a functional test. If validation fails, use the returned
step, container state, and logs to diagnose the failure, repair the project,
and validate again. Never claim that untested behavior works.
""",
    )

    @mcp.tool()
    def validate_python_project(
        project_path: str,
        entrypoint: str,
        entrypoint_type: Literal["file", "module"] = "file",
        arguments: list[str] | None = None,
        expect_long_running: bool = True,
    ) -> dict[str, Any]:
        """Install and start a Python project in a temporary Docker container.

        The project must be below the workspace fixed when this MCP server was
        started. A sanitized temporary copy is mounted read-only, copied inside
        the container, and left unchanged on the host. Dependencies are
        installed from requirements.txt and/or the Python package metadata. The
        container is removed afterwards. No functional tests or endpoint
        requests are performed.

        Args:
            project_path: Project directory relative to the workspace; use "."
                for the workspace root. Absolute paths and ".." are forbidden.
            entrypoint: A project-relative .py file, or a Python module name
                when entrypoint_type is "module".
            entrypoint_type: Either "file" or "module".
            arguments: Optional arguments passed directly to Python without a
                shell. Do not include "python" itself.
            expect_long_running: True for servers that must still be running
                after the startup grace period. False also accepts exit code 0.
        """
        logger.info(
            "Validating Python project project_path=%s entrypoint=%s type=%s",
            project_path,
            entrypoint,
            entrypoint_type,
        )
        return validator.validate(
            project_path,
            entrypoint,
            entrypoint_type=entrypoint_type,
            arguments=arguments,
            expect_long_running=expect_long_running,
        )

    return mcp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="MCP server for isolated Python build/start validation"
    )
    parser.add_argument(
        "--project-directory",
        required=True,
        type=Path,
        help="Workspace directory fixed for this MCP process",
    )
    parser.add_argument(
        "--python-image",
        required=True,
        help=(
            "Python base image used for validation; provide an immutable "
            "@sha256 digest from the approved internal registry"
        ),
    )
    parser.add_argument(
        "--require-pinned-image",
        action="store_true",
        help=(
            "Reject mutable image tags; require a full @sha256 digest for "
            "the validator image."
        ),
    )
    parser.add_argument(
        "--network-mode",
        choices=("none", "bridge"),
        default="none",
        help=(
            "Container network mode. 'none' is the secure default; use "
            "'bridge' only for an explicitly trusted validation run."
        ),
    )
    parser.add_argument("--setup-timeout", type=int, default=180)
    parser.add_argument("--startup-grace", type=float, default=3.0)
    parser.add_argument("--memory-limit", default="2g")
    parser.add_argument("--cpu-limit", default="2.0")
    parser.add_argument(
        "--config-file",
        type=Path,
        default=None,
        help="Configuration file used for logging settings",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    config = load_config(args.config_file)
    configure_logging(
        config.logging,
        logger=logger,
        default_filename="cli-agent-python-validator-mcp.log",
    )
    settings = ValidatorSettings(
        python_image=args.python_image,
        # Image pinning is mandatory for every validator process. The flag is
        # retained as a compatibility marker for existing invocations.
        require_pinned_image=True,
        network_mode=args.network_mode,
        setup_timeout_seconds=args.setup_timeout,
        startup_grace_seconds=args.startup_grace,
        memory_limit=args.memory_limit,
        cpu_limit=args.cpu_limit,
    )
    validator = DockerPythonValidator(args.project_directory, settings)
    create_server(validator).run(transport="stdio")


if __name__ == "__main__":
    main()
