from __future__ import annotations

import argparse
import asyncio
import traceback
from dataclasses import replace
from pathlib import Path

from .agent import CliAgent
from .config import AppConfig, McpServerConfig, default_config_file, load_config
from .logging_setup import configure_logging
from .model_factory import create_model_client


OS_MCP_SERVER_NAME = "os"
PYTHON_VALIDATOR_MCP_SERVER_NAME = "python-validator"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli-agent",
        description="General local agent using configurable MCP servers",
    )
    parser.add_argument(
        "prompt",
        nargs="*",
        help="Optional one-shot prompt; omit it for interactive mode",
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        default=Path.cwd(),
        help="Fixed workspace directory (default: current directory)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Configuration file "
            f"(default: {default_config_file()})"
        ),
    )
    os_access = parser.add_mutually_exclusive_group()
    os_access.add_argument(
        "--with-os-read",
        action="store_const",
        const="read",
        dest="os_access",
        help=(
            "Enable the built-in workspace OS MCP server with read-only "
            "access. Overrides an 'os' MCP server from the config."
        ),
    )
    os_access.add_argument(
        "--with-os-write",
        action="store_const",
        const="write",
        dest="os_access",
        help=(
            "Enable the built-in workspace OS MCP server with read and write "
            "access. Overrides an 'os' MCP server from the config."
        ),
    )
    parser.add_argument(
        "--with-python-validator",
        action="store_true",
        help=(
            "Enable the built-in Python validator MCP server. Overrides a "
            "'python-validator' MCP server from the config."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show a complete traceback when an error occurs",
    )
    return parser


def _os_mcp_server_config(access: str) -> McpServerConfig:
    if access not in {"read", "write"}:
        raise ValueError(f"Unsupported OS MCP access mode: {access!r}")

    return McpServerConfig(
        name=OS_MCP_SERVER_NAME,
        transport="stdio",
        command="{python}",
        args=(
            "-m",
            "cli_agent.os_mcp_server",
            "--project-directory",
            "{workspace_directory}",
            "--config-file",
            "{config_file}",
            "--access",
            access,
        ),
        config={"allow_write_files": access == "write"},
    )


def _python_validator_mcp_server_config() -> McpServerConfig:
    return McpServerConfig(
        name=PYTHON_VALIDATOR_MCP_SERVER_NAME,
        transport="stdio",
        command="{python}",
        args=(
            "-m",
            "cli_agent.python_validator_mcp",
            "--project-directory",
            "{workspace_directory}",
            "--python-image",
            "python:3.12-slim",
        ),
    )


def apply_mcp_cli_overrides(
    config: AppConfig,
    *,
    os_access: str | None,
    with_python_validator: bool = False,
) -> AppConfig:
    """Apply command-line MCP settings with precedence over file config."""
    servers = config.mcp_servers
    changed = False

    if os_access is not None:
        servers = tuple(
            server
            for server in servers
            if server.name != OS_MCP_SERVER_NAME
        ) + (_os_mcp_server_config(os_access),)
        changed = True

    if with_python_validator:
        servers = tuple(
            server
            for server in servers
            if server.name != PYTHON_VALIDATOR_MCP_SERVER_NAME
        ) + (_python_validator_mcp_server_config(),)
        changed = True

    if not changed:
        return config
    return replace(config, mcp_servers=servers)


def exception_details(exc: BaseException) -> str:
    """Return useful leaf messages instead of ExceptionGroup's generic title."""
    leaves: list[str] = []

    def collect(current: BaseException) -> None:
        nested = getattr(current, "exceptions", None)
        if nested:
            for child in nested:
                collect(child)
            return
        message = str(current).strip()
        label = type(current).__name__
        leaves.append(f"{label}: {message}" if message else label)

    collect(exc)
    return "\n".join(dict.fromkeys(leaves))


def print_error(exc: BaseException, *, debug: bool) -> None:
    if debug:
        traceback.print_exception(exc)
    else:
        print(f"Fehler: {exception_details(exc)}")


async def run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    config = apply_mcp_cli_overrides(
        config,
        os_access=args.os_access,
        with_python_validator=args.with_python_validator,
    )
    configure_logging(config.logging)
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"Arbeitsordner existiert nicht: {workspace}")
    model_client = create_model_client(config.model)
    agent = CliAgent(
        workspace,
        model_client,
        config.mcp_servers,
        logging_config=config.logging,
        config_file=args.config or default_config_file(),
        dump_llm_context=config.dump_llm_context,
        okf=config.okf
    )

    print(f"Arbeitsordner: {workspace}")
    print(
        "MCP-Server: "
        + (", ".join(server.name for server in config.mcp_servers) or "(keine)")
    )
    print(
    f"Modell: {config.model.model} "
    f"({config.model.provider}, {config.model.base_url})"
)
    if config.logging.enabled:
        print(f"Logdatei: {config.logging.file}")

    if config.okf:
        print(
            f"OKF-Repository: {config.okf.repository}"
        )

    async with agent:
        if args.prompt:
            print(await agent.ask(" ".join(args.prompt)))
            return

        print(
            "Interaktiver Modus; 'enable <server>' und 'disable <server>' "
            "steuern MCP-Server, 'exit' oder 'quit' beendet die Sitzung."
        )
        while True:
            try:
                prompt = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if prompt.lower() in {"exit", "quit"}:
                return
            if not prompt:
                continue
            try:
                print(await agent.ask(prompt))
            except Exception as exc:
                print_error(exc, debug=args.debug)


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except Exception as exc:
        print_error(exc, debug=args.debug)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
