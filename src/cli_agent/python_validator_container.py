from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


SOURCE = Path("/source")
PROJECT = Path("/app")
IGNORED_NAMES = (
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--entrypoint-type",
        required=True,
        choices=("file", "module"),
    )
    parser.add_argument("--entrypoint", required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.arguments[:1] == ["--"]:
        args.arguments = args.arguments[1:]
    return args


def run_checked(command: list[str]) -> None:
    print(f"[python-validator] running: {command!r}", flush=True)
    subprocess.run(command, check=True)


def install_dependencies() -> None:
    pip = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-cache-dir",
    ]
    if Path("requirements.txt").is_file():
        run_checked([*pip, "-r", "requirements.txt"])

    if (
        Path("pyproject.toml").is_file()
        or Path("setup.py").is_file()
        or Path("setup.cfg").is_file()
    ):
        run_checked([*pip, "."])


def start_command(args: argparse.Namespace) -> list[str]:
    if args.entrypoint_type == "module":
        return [sys.executable, "-m", args.entrypoint, *args.arguments]

    entrypoint = (PROJECT / args.entrypoint).resolve()
    try:
        entrypoint.relative_to(PROJECT)
    except ValueError as exc:
        raise RuntimeError("Entrypoint is outside /app") from exc
    if not entrypoint.is_file():
        raise RuntimeError(f"Entrypoint does not exist: {args.entrypoint}")
    return [sys.executable, str(entrypoint), *args.arguments]


def main() -> None:
    args = parse_args()
    if not SOURCE.is_dir():
        raise RuntimeError("Project mount /source is missing")

    shutil.copytree(
        SOURCE,
        PROJECT,
        symlinks=True,
        ignore=shutil.ignore_patterns(*IGNORED_NAMES),
    )
    os.chdir(PROJECT)
    run_checked([sys.executable, "-m", "compileall", "-q", "."])
    install_dependencies()

    ready_token = os.environ.get("PYTHON_VALIDATOR_READY_TOKEN")
    if not ready_token:
        raise RuntimeError("PYTHON_VALIDATOR_READY_TOKEN is missing")
    print(ready_token, flush=True)

    command = start_command(args)
    print(f"[python-validator] starting: {command!r}", flush=True)
    os.execv(command[0], command)


if __name__ == "__main__":
    main()
