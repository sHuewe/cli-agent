from __future__ import annotations

import argparse
import asyncio
import os
import traceback
from pathlib import Path

from .agent import CliAgent
from .config import default_config_file, load_config
from .logging_setup import configure_logging
from .ollama import OllamaClient


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
        "--model",
        default=os.getenv("OLLAMA_MODEL", "qwen3.5:9b"),
        help="Ollama model (default: OLLAMA_MODEL or qwen3.5:9b)",
    )
    parser.add_argument(
        "--ollama-url",
        default=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        help="Ollama base URL (default: OLLAMA_BASE_URL or localhost:11434)",
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
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Show a complete traceback when an error occurs",
    )
    return parser


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
    configure_logging(config.logging)
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"Arbeitsordner existiert nicht: {workspace}")
    ollama = OllamaClient(base_url=args.ollama_url, model=args.model)
    agent = CliAgent(
        workspace,
        ollama,
        config.mcp_servers,
        logging_config=config.logging,
        config_file=args.config or default_config_file()
    )

    print(f"Arbeitsordner: {workspace}")
    print(
        "MCP-Server: "
        + (", ".join(server.name for server in config.mcp_servers) or "(keine)")
    )
    print(f"Modell: {args.model} ({args.ollama_url})")
    if config.logging.enabled:
        print(f"Logdatei: {config.logging.file}")

    async with agent:
        if args.prompt:
            print(await agent.ask(" ".join(args.prompt)))
            return

        print("Interaktiver Modus; 'exit' oder 'quit' beendet die Sitzung.")
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
