"""Standalone, extensible RAG administration CLI.

Commands are explicit offline operations. They never grant additional
capabilities to the main agent or mutate the agent's user configuration.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Sequence

from cli_agent.settings.admin_config import load_admin_config
from cli_agent.settings.config import VectorKnowledgeConfig
from cli_agent.context.knowledge_vector import build_index, update_index


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli-agent-rag",
        description="RAG indexing and management for cli-agent.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init_db = commands.add_parser(
        "init-vector-db",
        help="Create or rebuild a SQLite vector index from an OKF repository.",
        description=(
            "Indexes only accessible, valid OKF concepts. "
            "Embeddings are generated using Ollama or an OpenAI-compatible API. "
            "An existing index is replaced atomically only after successful "
            "indexing."
        ),
    )
    init_db.add_argument(
        "--okf-repo", type=Path, required=True, metavar="PATH",
        help="OKF repository with index.md at its root.",
    )
    init_db.add_argument(
        "--embedding-model", required=True, metavar="MODEL",
        help="Embedding model of the configured provider.",
    )
    init_db.add_argument(
        "--sqlite-file", type=Path, required=True, metavar="PATH",
        help="Target SQLite file outside the OKF repository.",
    )
    init_db.add_argument(
        "--provider", "--embedding-provider", dest="provider",
        choices=("ollama", "openai"), default="ollama",
        help="Embedding API: ollama (default) or openai.",
    )
    init_db.add_argument(
        "--base-url", metavar="URL",
        help="Embedding endpoint. Default for Ollama: http://localhost:11434; "
             "required for OpenAI-compatible providers (including /v1).",
    )
    init_db.add_argument(
        "--api-key-env", metavar="NAME",
        help="Credential environment variable name (OpenAI-compatible only). "
             "External credentials require admin approval.",
    )
    init_db.add_argument(
        "--max-files", type=int, default=10000, metavar="N",
        help="Maximum number of OKF concepts to index (default: 10000).",
    )
    init_db.add_argument(
        "--max-read-bytes", type=int, default=2_560_000, metavar="N",
        help="Effective OKF read limit in bytes, same as [okf].max_read_bytes.",
    )
    update_db = commands.add_parser(
        "update-vector-db",
        help="Incrementally update an existing SQLite vector index.",
        description="Reuses unchanged concept vectors, updates changed ones and removes unreachable concepts.",
    )
    # Both commands accept the same source/model/backend arguments.
    for action in init_db._actions:
        if action.dest in {"help", "command"}:
            continue
        kwargs = {
            "dest": action.dest,
            "default": action.default,
            "required": action.required,
            "help": action.help,
        }
        if action.type is not None:
            kwargs["type"] = action.type
        if action.choices is not None:
            kwargs["choices"] = action.choices
        update_db.add_argument(*action.option_strings, **kwargs)
    return parser


async def init_vector_db(args: argparse.Namespace, *, incremental: bool = False) -> int:
    """Build the index using the same backend as the configured Knowledge-Loop."""
    root = args.okf_repo.expanduser().resolve()
    database = args.sqlite_file.expanduser().resolve()

    if not root.is_dir():
        raise ValueError(f"OKF-Repository ist kein vorhandenes Verzeichnis: {root}")
    if database.is_relative_to(root):
        raise ValueError(
            "Target SQLite file must be outside the OKF repository."
        )
    if not isinstance(args.embedding_model, str) or not args.embedding_model.strip():
        raise ValueError("--embedding-model darf nicht leer sein.")
    if args.max_read_bytes < 1:
        raise ValueError("--max-read-bytes muss positiv sein.")
    if args.max_files < 1 or args.max_files > 100000:
        raise ValueError("--max-files muss zwischen 1 und 100000 liegen.")
    if args.provider == "openai" and not args.base_url:
        raise ValueError(
            "--provider openai requires an explicit --base-url."
        )
    if args.provider == "ollama" and args.api_key_env:
        raise ValueError("--api-key-env is supported only for --provider openai.")

    # Validate the OKF marker before contacting the embedding endpoint.
    from cli_agent.okf_mcp_server.repository import OkfRepository

    OkfRepository.from_directory(root, max_read_bytes=args.max_read_bytes)

    vector = VectorKnowledgeConfig(
        mode="okf",
        database=str(database),
        provider=args.provider,
        model=args.embedding_model.strip(),
        base_url=args.base_url or "http://localhost:11434",
        api_key_env=args.api_key_env,
        max_files=args.max_files,
    )
    admin = load_admin_config()
    kwargs = {
        "root": root,
        "path": database,
        "network": admin.network,
        "credential_rules": admin.model_credentials,
        "okf_max_read_bytes": args.max_read_bytes,
    }
    if incremental:
        result = await update_index(vector, **kwargs)
        print(
            f"SQLite Knowledge index updated: {database} "
            f"(added: {result.added}, changed: {result.changed}, "
            f"unchanged: {result.unchanged}, removed: {result.removed})."
        )
        return result.total
    count = await build_index(vector, **kwargs)
    print(f"SQLite Knowledge index created: {database} ({count} OKF-Concepts).")
    return count


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command in {"init-vector-db", "update-vector-db"}:
            asyncio.run(init_vector_db(args, incremental=args.command == "update-vector-db"))
        else:
            parser.error("Unbekanntes cli-agent-rag-Kommando.")
    except (OSError, ValueError, RuntimeError) as exc:
        parser.exit(1, f"cli-agent-rag: {exc}\n")


if __name__ == "__main__":
    main()
