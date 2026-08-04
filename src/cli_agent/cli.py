from __future__ import annotations

import argparse
import asyncio
import traceback
from pathlib import Path

from .agent import CliAgent
from .config import default_config_file, load_config
from .logging_setup import configure_logging
from .model_factory import create_model_client


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
    model_client = create_model_client(config.model)
    agent = CliAgent(
        workspace,
        model_client,
        config.mcp_servers,
        logging_config=config.logging,
        config_file=args.config or default_config_file(),
        dump_llm_context=config.dump_llm_context
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
