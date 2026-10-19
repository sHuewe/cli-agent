"""Standalone, extensible RAG administration CLI.

Commands are explicit offline operations. They never grant additional
capabilities to the main agent or mutate the agent's user configuration.
"""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Sequence

from .admin_config import load_admin_config
from .config import VectorKnowledgeConfig
from .knowledge_vector import build_index, update_index


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli-agent-rag",
        description="RAG-Indexierung und Verwaltung für cli-agent.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    init_db = commands.add_parser(
        "init-vector-db",
        help="SQLite-Vektorindex aus einem OKF-Repository erstellen oder neu aufbauen.",
        description=(
            "Indexiert ausschließlich erreichbare, gültige OKF-Concepts. "
            "Die Embeddings werden über Ollama oder eine OpenAI-kompatible API "
            "erzeugt. Ein vorhandener Index wird erst nach erfolgreicher "
            "Indexierung atomar ersetzt."
        ),
    )
    init_db.add_argument(
        "--okf-repo", type=Path, required=True, metavar="PATH",
        help="OKF-Repository mit index.md im Root.",
    )
    init_db.add_argument(
        "--embedding-model", required=True, metavar="MODEL",
        help="Embedding-Modell des konfigurierten Providers.",
    )
    init_db.add_argument(
        "--sqlite-file", type=Path, required=True, metavar="PATH",
        help="SQLite-Zieldatei außerhalb des OKF-Repositories.",
    )
    init_db.add_argument(
        "--provider", "--embedding-provider", dest="provider",
        choices=("ollama", "openai"), default="ollama",
        help="Embedding-API: ollama (Default) oder openai.",
    )
    init_db.add_argument(
        "--base-url", metavar="URL",
        help="Embedding-Endpunkt. Default für Ollama: http://localhost:11434; "
             "für OpenAI-kompatible Provider erforderlich (einschl. /v1).",
    )
    init_db.add_argument(
        "--api-key-env", metavar="NAME",
        help="Name der Credential-Umgebungsvariable (nur OpenAI-kompatibel). "
             "Externe Credentials benötigen eine Admin-Freigabe.",
    )
    init_db.add_argument(
        "--max-files", type=int, default=10000, metavar="N",
        help="Maximale Anzahl zu indexierender OKF-Concepts (Default: 10000).",
    )
    update_db = commands.add_parser(
        "update-vector-db",
        help="Bestehenden SQLite-Vektorindex inkrementell aktualisieren.",
        description="Verwendet unveränderte Concept-Vektoren erneut, aktualisiert geänderte und entfernt nicht mehr erreichbare Concepts.",
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
            "SQLite-Zieldatei muss außerhalb des OKF-Repositories liegen."
        )
    if not isinstance(args.embedding_model, str) or not args.embedding_model.strip():
        raise ValueError("--embedding-model darf nicht leer sein.")
    if args.max_files < 1 or args.max_files > 100000:
        raise ValueError("--max-files muss zwischen 1 und 100000 liegen.")
    if args.provider == "openai" and not args.base_url:
        raise ValueError(
            "Für --provider openai muss --base-url explizit angegeben werden."
        )
    if args.provider == "ollama" and args.api_key_env:
        raise ValueError("--api-key-env wird nur für --provider openai unterstützt.")

    # Validate the OKF marker before contacting the embedding endpoint.
    from .okf_mcp_server.repository import OkfRepository

    OkfRepository.from_directory(root)

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
    }
    if incremental:
        result = await update_index(vector, **kwargs)
        print(
            f"SQLite-Knowledge-Index aktualisiert: {database} "
            f"(neu: {result.added}, geändert: {result.changed}, "
            f"unverändert: {result.unchanged}, entfernt: {result.removed})."
        )
        return result.total
    count = await build_index(vector, **kwargs)
    print(f"SQLite-Knowledge-Index erstellt: {database} ({count} OKF-Concepts).")
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
