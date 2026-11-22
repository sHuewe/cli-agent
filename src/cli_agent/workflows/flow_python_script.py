"""Execute trusted, statically declared Python Flow scripts as subprocesses.

The process receives a single JSON envelope on stdin and must write exactly
one JSON value on stdout. Diagnostic text belongs on stderr. No shell is used.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import subprocess
from pathlib import Path, PureWindowsPath
import sys
from typing import Any

from cli_agent.security.filesystem_security import path_entry_is_symlink_or_reparse
from cli_agent.interfaces.terminal_output import sanitize_terminal_text

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
    raw_executable = config.get("interpreter") or sys.executable
    launch = Path(raw_executable)
    if not launch.is_absolute():
        launch = workspace / launch
    return {
        **config,
        "_script": str(script),
        "_script_sha256": _digest(script, limit=MAX_SCRIPT_BYTES),
        "_interpreter": str(interpreter),
        "_interpreter_launch": str(launch),
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
    raw_executable = config.get("interpreter") or sys.executable
    launch = Path(raw_executable)
    if not launch.is_absolute():
        launch = workspace / launch
    if str(launch) != config["_interpreter_launch"]:
        raise ValueError("Python interpreter selection changed after Flow startup.")
    # Use the lexical entry point for execution: venv/bin/python is commonly a
    # symlink whose path (not just its target) controls sys.prefix and imports.
    return script, launch


async def _terminate_process_tree(proc: asyncio.subprocess.Process, workspace: Path) -> None:
    """Terminate the entire spawned process tree where supported.

    POSIX starts scripts in their own session so group termination also
    reaches descendants holding inherited stdout/stderr pipes. Windows
    uses taskkill /T /F and falls back to process.kill.
    """
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        taskkill = system_root / "System32" / "taskkill.exe"
        executable = str(taskkill) if taskkill.is_file() else "taskkill"
        try:
            killer = await asyncio.create_subprocess_exec(
                executable, "/PID", str(proc.pid), "/T", "/F",
                cwd=workspace, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                await asyncio.wait_for(killer.wait(), timeout=3)
            except asyncio.TimeoutError:
                killer.kill()
                await killer.wait()
        except (OSError, ValueError):
            pass
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass


async def run_script(
    *, workspace: Path, config: dict[str, Any], envelope_json: str,
) -> str:
    script, interpreter = verify_script_config(workspace, config)
    data = envelope_json.encode("utf-8")
    if len(data) > MAX_SCRIPT_INPUT:
        raise ValueError("Python script input exceeds the JSON size limit.")
    command = [str(interpreter), str(script), *config.get("args", [])]
    kwargs = (
        {"start_new_session": True} if os.name == "posix"
        else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
        if os.name == "nt" else {}
    )
    proc = await asyncio.create_subprocess_exec(
        *command, cwd=workspace, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )
    overflow = asyncio.Event()
    overflow_message: str | None = None

    async def collect(stream: asyncio.StreamReader, limit: int, name: str) -> bytes:
        nonlocal overflow_message
        chunks: list[bytes] = []
        size = 0
        while True:
            block = await stream.read(65536)
            if not block:
                break
            size += len(block)
            if size > limit:
                if overflow_message is None:
                    overflow_message = f"Python script {name} exceeds {limit} bytes."
                overflow.set()
                # Keep draining without retaining additional bytes. Raising
                # here would pause the pipe and deadlock proc.wait() on cleanup.
                continue
            chunks.append(block)
        return b"".join(chunks)

    async def feed() -> None:
        assert proc.stdin is not None
        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        finally:
            proc.stdin.close()

    assert proc.stdout is not None and proc.stderr is not None
    tasks = (
        asyncio.create_task(feed()),
        asyncio.create_task(collect(proc.stdout, MAX_SCRIPT_OUTPUT, "stdout")),
        asyncio.create_task(collect(proc.stderr, MAX_SCRIPT_STDERR, "stderr")),
        asyncio.create_task(proc.wait()),
    )
    completed = asyncio.gather(*tasks)
    exceeded = asyncio.create_task(overflow.wait())
    failed = True
    try:
        done, _ = await asyncio.wait(
            {completed, exceeded}, timeout=config.get("timeout_seconds", 300),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if overflow.is_set():
            raise ValueError(overflow_message or "Python script output exceeded its limit.")
        if completed not in done:
            raise asyncio.TimeoutError("Python script exceeded its timeout.")
        _, stdout, stderr, code = completed.result()
        if overflow.is_set():
            raise ValueError(overflow_message or "Python script output exceeded its limit.")
        failed = False
    finally:
        exceeded.cancel()
        if failed:
            # Kill descendants first so no inherited pipe writer survives the
            # script and prevents EOF. Readers continue to drain until cleanup.
            await _terminate_process_tree(proc, workspace)
            try:
                await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True),
                    timeout=5,
                )
            except asyncio.TimeoutError:
                for task in tasks:
                    task.cancel()
                # Defensive last resort: detach paused asyncio pipe transports
                # if the platform failed to terminate a surviving descendant.
                for stream in (proc.stdout, proc.stderr):
                    transport = getattr(stream, "_transport", None)
                    if transport is not None:
                        transport.close()
                await asyncio.sleep(0)

    if stderr:
        # Never treat process diagnostics as JSON output.
        print(sanitize_terminal_text(
            stderr.decode("utf-8", errors="replace"), multiline=True,
            escape_invisible_formatting=True, escape_literal_backslashes=True,
        ), file=sys.stderr)
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
