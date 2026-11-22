"""Execute trusted, statically declared Python Flow scripts as subprocesses.

The process receives a single JSON envelope on stdin and must write exactly
one JSON value on stdout. Diagnostic text belongs on stderr. No shell is used.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import sys
from typing import Any

from cli_agent.security.filesystem_security import path_entry_is_symlink_or_reparse

MAX_SCRIPT_BYTES = 2_000_000
MAX_SCRIPT_STDERR = 65_536
MAX_SCRIPT_INPUT = 100_000_000
MAX_SCRIPT_OUTPUT = 100_000_000


def _safe_script_path(workspace: Path, value: str) -> Path:
    if not isinstance(value, str) or not value or "${" in value:
        raise ValueError("python_script.path must be a static nonempty path.")
    path = Path(value)
    windows = PureWindowsPath(value)
    if path.is_absolute() or windows.drive or ".." in path.parts or ".." in windows.parts:
        raise ValueError("python_script.path must be workspace-relative.")
    current = workspace
    for component in path.parts:
        current /= component
        if path_entry_is_symlink_or_reparse(current):
            raise ValueError("python_script.path must not contain symlinks or junctions.")
    result = current.resolve(strict=True)
    if not result.is_file():
        raise ValueError("python_script.path must reference a regular file.")
    try:
        result.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("python_script.path escapes the workspace.") from exc
    return result


def _interpreter_path(workspace: Path, raw: str | None) -> tuple[Path, Path | None]:
    """Return executable and its lexical workspace path for protection.

    Linux venv executables are normally symlinks to a system Python. Unlike
    script sources, interpreter symlinks are accepted but their lexical paths
    and resolved targets are checked on each execution.
    """
    if raw is None:
        raw = sys.executable
    if not isinstance(raw, str) or not raw.strip() or "${" in raw:
        raise ValueError("python_script.interpreter must be a static path.")
    path = Path(raw)
    win = PureWindowsPath(raw)
    if ".." in path.parts or ".." in win.parts or (win.drive and not path.is_absolute() and os.name != "nt"):
        raise ValueError("python_script.interpreter contains an unsafe path.")
    lexical = path if path.is_absolute() else workspace / path
    executable = lexical.resolve(strict=True)
    if not executable.is_file():
        raise ValueError("python_script.interpreter must reference a file.")
    # The literal path is retained for symlink-change detection. Do not
    # resolve a workspace symlink and silently forget the writable entry.
    try:
        lexical.relative_to(workspace)
    except ValueError:
        return executable, None
    return executable, lexical


def _digest(path: Path, *, limit: int | None = None) -> str:
    if not path.is_file():
        raise ValueError(f"Python execution file missing: {path}")
    sha = hashlib.sha256()
    read = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            read += len(chunk)
            if limit is not None and read > limit:
                raise ValueError("Python script exceeds maximum source size.")
            sha.update(chunk)
    return sha.hexdigest()


def prepare_script_config(workspace: Path, config: dict[str, Any]) -> dict[str, Any]:
    script = _safe_script_path(workspace, config["path"])
    interpreter, lexical = _interpreter_path(workspace, config.get("interpreter"))
    interpreter_hash = _digest(interpreter)
    return {
        **config,
        "_script": str(script),
        "_script_sha256": _digest(script, limit=MAX_SCRIPT_BYTES),
        "_interpreter": str(interpreter),
        "_interpreter_sha256": interpreter_hash,
        "_interpreter_lexical": str(lexical) if lexical else None,
    }


def verify_script_config(workspace: Path, config: dict[str, Any]) -> tuple[Path, Path]:
    script = _safe_script_path(workspace, config["path"])
    if str(script) != config["_script"] or _digest(script, limit=MAX_SCRIPT_BYTES) != config["_script_sha256"]:
        raise ValueError("Python script changed after Flow startup.")
    interpreter, lexical = _interpreter_path(workspace, config.get("interpreter"))
    if (str(interpreter) != config["_interpreter"]
            or _digest(interpreter) != config["_interpreter_sha256"]
            or (str(lexical) if lexical else None) != config["_interpreter_lexical"]):
        raise ValueError("Python interpreter changed after Flow startup.")
    return script, interpreter


async def run_script(
    *, workspace: Path, config: dict[str, Any], envelope_json: str,
) -> str:
    script, interpreter = verify_script_config(workspace, config)
    data = envelope_json.encode("utf-8")
    if len(data) > MAX_SCRIPT_INPUT:
        raise ValueError("Python script input exceeds the JSON size limit.")
    command = [str(interpreter), str(script), *config.get("args", [])]
    proc = await asyncio.create_subprocess_exec(
        *command, cwd=workspace, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )

    async def collect(stream: asyncio.StreamReader, limit: int, name: str) -> bytes:
        parts = []
        size = 0
        while True:
            block = await stream.read(65536)
            if not block:
                break
            size += len(block)
            if size > limit:
                raise ValueError(f"Python script {name} exceeds {limit} bytes.")
            parts.append(block)
        return b"".join(parts)

    async def feed() -> None:
        assert proc.stdin is not None
        proc.stdin.write(data)
        await proc.stdin.drain()
        proc.stdin.close()

    try:
        assert proc.stdout is not None and proc.stderr is not None
        _, stdout, stderr, code = await asyncio.wait_for(
            asyncio.gather(
                feed(),
                collect(proc.stdout, MAX_SCRIPT_OUTPUT, "stdout"),
                collect(proc.stderr, MAX_SCRIPT_STDERR, "stderr"),
                proc.wait(),
            ),
            timeout=config.get("timeout_seconds", 300),
        )
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise

    if stderr:
        # Never treat process diagnostics as JSON output.
        print(stderr.decode("utf-8", errors="replace"), file=sys.stderr)
    if code:
        raise ValueError(f"Python script exited with code {code}.")
    try:
        answer = stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Python script stdout is not UTF-8 JSON.") from exc
    try:
        json.loads(answer, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, json.JSONDecodeError) as exc:
        raise ValueError("Python script must emit exactly one valid JSON value on stdout.") from exc
    return answer
