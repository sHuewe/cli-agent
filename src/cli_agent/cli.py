from __future__ import annotations

import argparse
import asyncio
import json
import sys
import traceback
from dataclasses import replace
from pathlib import Path

from .admin_config import AdminConfig, default_admin_config_file, load_admin_config
from .config import AppConfig, McpServerConfig, default_config_file, load_config
from .logging_setup import configure_logging
from .model_factory import create_model_client
from .web_context_agent import WebContextCliAgent

OS_MCP_SERVER_NAME = "os"
SENSITIVE_ARGUMENT_MARKERS = ("auth", "api_key", "apikey", "authorization", "credential", "password", "secret", "token")


def _is_sensitive_argument_name(name: str) -> bool:
    normalized = name.casefold().replace("-", "_")
    return normalized in {"arguments", "body", "content", "data", "env", "headers", "payload"} or any(marker in normalized for marker in SENSITIVE_ARGUMENT_MARKERS)


def _approval_value(name: str, value: object) -> object:
    if _is_sensitive_argument_name(name):
        if isinstance(value, (str, bytes, list, tuple, dict)):
            return f"<{len(value)} Elemente verborgen>"
        return "<verborgen>"
    if isinstance(value, dict):
        return {str(key): _approval_value(str(key), item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_approval_value(name, item) for item in value]
    if isinstance(value, str) and len(value) > 160:
        return f"<{len(value)} Zeichen>"
    return value


def _approval_arguments(arguments: dict[str, object]) -> str:
    summary = {str(name): _approval_value(str(name), value) for name, value in arguments.items()}
    return json.dumps(summary, ensure_ascii=False, sort_keys=True)


async def approve_tool_call(tool_name: str, arguments: dict[str, object]) -> bool | str:
    if not sys.stdin.isatty():
        return False
    print("\nExplizite Freigabe erforderlich: " f"{tool_name}({_approval_arguments(arguments)})")
    answer = await asyncio.to_thread(
        input,
        "Aktion ausführen? [j]a / [s] dieses Tool für die Session / [N]ein ",
    )
    normalized = answer.strip().casefold()
    if normalized in {"s", "session"}:
        return "session"
    return normalized in {"j", "ja", "y", "yes"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli-agent", description="General local agent using configurable MCP servers")
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt; omit it for interactive mode")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Fixed workspace directory (default: current directory)")
    parser.add_argument("--config", type=Path, default=None, help=f"Configuration file (default: {default_config_file()})")
    parser.add_argument("--model", default=None, help="Override model.model from the configuration file")
    os_access = parser.add_mutually_exclusive_group()
    os_access.add_argument("--with-os-read", action="store_const", const="read", dest="os_access", help="Enable the built-in workspace OS MCP server with read-only access. Overrides an 'os' MCP server from the config.")
    os_access.add_argument("--with-os-write", action="store_const", const="write", dest="os_access", help="Enable the built-in workspace OS MCP server with read and write access. Overrides an 'os' MCP server from the config.")
    parser.add_argument("--debug", action="store_true", help="Show a complete traceback when an error occurs")
    return parser


def _os_mcp_server_config(access: str) -> McpServerConfig:
    if access not in {"read", "write"}:
        raise ValueError(f"Unsupported OS MCP access mode: {access!r}")
    return McpServerConfig(
        name=OS_MCP_SERVER_NAME,
        transport="stdio",
        command="{python}",
        args=("-m", "cli_agent.os_mcp_server", "--project-directory", "{workspace_directory}", "--config-file", "{config_file}", "--access", access),
        config={"allow_write_files": access == "write"},
        built_in=True,
    )


def apply_mcp_cli_overrides(config: AppConfig, *, os_access: str | None) -> AppConfig:
    if os_access is None:
        return config
    servers = tuple(server for server in config.mcp_servers if server.name != OS_MCP_SERVER_NAME) + (_os_mcp_server_config(os_access),)
    return replace(config, mcp_servers=servers)


def apply_model_cli_override(config: AppConfig, *, model: str | None) -> AppConfig:
    if model is None:
        return config
    return replace(config, model=replace(config.model, model=model))


def exception_details(exc: BaseException) -> str:
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
    admin_config: AdminConfig = load_admin_config()
    config = apply_model_cli_override(config, model=args.model)
    config = apply_mcp_cli_overrides(config, os_access=args.os_access)
    configure_logging(config.logging)
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"Arbeitsordner existiert nicht: {workspace}")
    model_client = create_model_client(config.model, network=admin_config.network)
    agent = WebContextCliAgent(
        workspace,
        model_client,
        config.mcp_servers,
        logging_config=config.logging,
        config_file=args.config or default_config_file(),
        dump_llm_context=config.dump_llm_context,
        network=admin_config.network,
        mcp_policy=admin_config.mcp,
        approval_callback=approve_tool_call,
        okf=config.okf,
    )
    print(f"Arbeitsordner: {workspace}")
    print(f"Admin-Policy: {default_admin_config_file()}")
    print("MCP-Server: " + (", ".join(server.name for server in config.mcp_servers) or "(keine)"))
    print(f"Modell: {config.model.model} ({config.model.provider}, {config.model.base_url})")
    if config.logging.enabled:
        print(f"Logdatei: {config.logging.file}")
    if config.okf:
        print(f"OKF-Repository: {config.okf.repository}")
    async with agent:
        if args.prompt:
            print(await agent.ask(" ".join(args.prompt)))
            return
        print("Interaktiver Modus; 'enable <server>' und 'disable <server>' steuern MCP-Server, 'add_web_context <url>' lädt Web-Kontext, 'clear_web_context' entfernt ihn, 'tokens' zeigt die Usage des letzten Agentenlaufs, 'exit' oder 'quit' beendet die Sitzung.")
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
