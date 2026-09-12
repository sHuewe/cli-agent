from __future__ import annotations

import argparse
import os
import shutil

# The container runner invokes fixed commands without a shell.
import subprocess  # nosec B404
import sys
from pathlib import Path

SOURCE = Path("/source")
# The validator runs without root; /tmp is the writable container filesystem.
PROJECT = Path(os.sep) / "tmp" / "python-validator"
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
    ".cli-agent",
    ".env",
    ".env.*",
    ".aws",
    ".azure",
    ".docker",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".ssh",
    "credentials",
    "credentials.*",
    "secrets",
    "secrets.*",
    "*.key",
    "*.pem",
    "*.p12",
    "*.pfx",
    "*.log",
    "*.log.*",
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
    # Do not echo argv: validator arguments can contain user-provided secrets.
    print("[python-validator] running command", flush=True)
    subprocess.run(command, check=True)  # nosec B603


def install_dependencies() -> None:
    pip = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--user",
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
        raise RuntimeError("Entrypoint is outside the validator workspace") from exc
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
    print("[python-validator] starting validated process", flush=True)
    # execv starts the validated executable directly and does not invoke a shell.
    os.execv(command[0], command)  # nosec B606


if __name__ == "__main__":
    main()
