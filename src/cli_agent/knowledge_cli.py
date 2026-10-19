"""Offline knowledge indexing command.

Usage: cli-agent-knowledge index --config path/to/config.toml
"""
from __future__ import annotations

import argparse
import asyncio

from .admin_config import load_admin_config
from .config import default_config_file, load_config
from .knowledge_vector import build_index, db_path, source_root


async def _run_index(config_file):
    config = load_config(config_file)
    if config.knowledge_vector is None:
        raise ValueError("[knowledge.vector] ist nicht konfiguriert.")
    admin = load_admin_config()
    vector = config.knowledge_vector
    root = source_root(
        vector,
        okf_root=(
            source_root_placeholder(config.okf.repository, config_file)
            if config.okf is not None else None
        ),
        config_file=config_file,
    )
    db = db_path(vector, config_file=config_file)
    if db.is_relative_to(root):
        raise ValueError("Knowledge-Index muss außerhalb des Quellenverzeichnisses liegen.")
    count = await build_index(
        vector, root=root, path=db,
        network=admin.network, credential_rules=admin.model_credentials,
        okf_max_read_bytes=config.okf.max_read_bytes,
    )
    print(f"Knowledge-Index aktualisiert: {count} Quelldokumente; Datenbank: {db}")


def source_root_placeholder(path: str, config_file):
    from pathlib import Path
    root = Path(path).expanduser()
    if not root.is_absolute():
        root = config_file.parent / root
    return root.resolve()


def main() -> None:
    parser = argparse.ArgumentParser(prog="cli-agent-knowledge")
    parser.add_argument("command", choices=["index"])
    parser.add_argument("--config", type=__import__("pathlib").Path, default=None)
    args = parser.parse_args()
    path = args.config or default_config_file()
    asyncio.run(_run_index(path.expanduser().resolve()))


if __name__ == "__main__":
    main()
