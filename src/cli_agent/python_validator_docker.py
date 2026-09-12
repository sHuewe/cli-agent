from __future__ import annotations

import json
import re
import subprocess  # nosec B404
from collections.abc import Sequence
from pathlib import Path, PurePath
from typing import Any, Literal

from .python_validator_types import PythonValidationError


_MODULE_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")

# The host-side staging copy is the only project path mounted into Docker.
# Keep this defense-in-depth list aligned with the container runner's list.
VALIDATOR_IGNORED_NAMES = (
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
MAX_VALIDATOR_LOG_CHARS = 200_000


class DockerValidatorSupportMixin:
    def _resolve_project(self, project_path: str) -> Path:
        if not isinstance(project_path, str) or not project_path.strip():
            raise PythonValidationError("Der Projektpfad darf nicht leer sein.")

        candidate = Path(project_path)
        if candidate.is_absolute():
            raise PythonValidationError(
                "Der Projektpfad muss relativ zum Workspace sein."
            )
        if ".." in PurePath(project_path).parts:
            raise PythonValidationError("Der Projektpfad darf '..' nicht enthalten.")

        resolved = (self.workspace / candidate).resolve()
        try:
            resolved.relative_to(self.workspace)
        except ValueError as exc:
            raise PythonValidationError(
                "Der Projektpfad verweist außerhalb des Workspaces."
            ) from exc
        if not resolved.is_dir():
            raise PythonValidationError(
                f"Python-Projekt existiert nicht: {project_path!r}"
            )
        return resolved

    @staticmethod
    def _validate_entrypoint(
        project: Path,
        entrypoint: str,
        entrypoint_type: Literal["file", "module"],
    ) -> None:
        if entrypoint_type not in {"file", "module"}:
            raise PythonValidationError(
                "entrypoint_type muss 'file' oder 'module' sein."
            )
        if not isinstance(entrypoint, str) or not entrypoint.strip():
            raise PythonValidationError("Der Python-Einstiegspunkt fehlt.")

        if entrypoint_type == "module":
            if not _MODULE_NAME.fullmatch(entrypoint):
                raise PythonValidationError(
                    f"Ungültiger Python-Modulname: {entrypoint!r}"
                )
            return

        candidate = Path(entrypoint)
        if candidate.is_absolute() or ".." in PurePath(entrypoint).parts:
            raise PythonValidationError(
                "Der Einstiegspunkt muss innerhalb des Projekts liegen."
            )
        resolved = (project / candidate).resolve()
        try:
            resolved.relative_to(project)
        except ValueError as exc:
            raise PythonValidationError(
                "Der Einstiegspunkt verweist außerhalb des Projekts."
            ) from exc
        if not resolved.is_file():
            raise PythonValidationError(
                f"Python-Einstiegspunkt existiert nicht: {entrypoint!r}"
            )
        if resolved.suffix.lower() != ".py":
            raise PythonValidationError(
                "Ein Datei-Einstiegspunkt muss eine .py-Datei sein."
            )

    @staticmethod
    def _validate_arguments(arguments: Sequence[str] | None) -> list[str]:
        values = list(arguments or [])
        if not all(isinstance(value, str) for value in values):
            raise PythonValidationError("Alle Startargumente müssen Strings sein.")
        return values

    def _command(
        self,
        args: list[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        return self._run_command(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def _logs(self, container_name: str) -> str:
        result = self._command(
            [
                "docker",
                "logs",
                "--tail",
                str(self.settings.log_lines),
                container_name,
            ],
            timeout=10,
        )
        output = (result.stdout or "") + (result.stderr or "")
        if len(output) > MAX_VALIDATOR_LOG_CHARS:
            return (
                output[:MAX_VALIDATOR_LOG_CHARS]
                + "\n[Validator-Logs wegen Größenlimit abgeschnitten]"
            ).strip()
        return output.strip()

    def _state(self, container_name: str) -> dict[str, Any] | None:
        result = self._command(
            [
                "docker",
                "inspect",
                "--format",
                "{{json .State}}",
                container_name,
            ],
            timeout=10,
        )
        if result.returncode != 0:
            return None
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def _remove_container(self, container_name: str) -> tuple[bool, str]:
        try:
            cleanup = self._command(
                ["docker", "rm", "--force", container_name],
                timeout=20,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        detail = (
            cleanup.stdout.strip()
            if cleanup.returncode == 0
            else cleanup.stderr.strip()
        )
        return cleanup.returncode == 0, detail
