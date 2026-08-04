from __future__ import annotations

import json
import re
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Callable, Literal, Sequence


class PythonValidationError(RuntimeError):
    """The Python validation request is invalid."""


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
_MODULE_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")


@dataclass(frozen=True)
class ValidatorSettings:
    python_image: str = "python:3.12-slim"
    setup_timeout_seconds: int = 180
    startup_grace_seconds: float = 3.0
    memory_limit: str = "2g"
    cpu_limit: str = "2.0"
    pids_limit: int = 256
    log_lines: int = 200


class DockerPythonValidator:
    """Build and start Python code in a short-lived Docker container."""

    def __init__(
        self,
        workspace: Path,
        settings: ValidatorSettings | None = None,
        *,
        command_runner: CommandRunner = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        resolved_workspace = workspace.resolve()
        if not resolved_workspace.is_dir():
            raise PythonValidationError(
                f"Projekt-Workspace existiert nicht: {resolved_workspace}"
            )

        self.workspace = resolved_workspace
        self.settings = settings or ValidatorSettings()
        self._run_command = command_runner
        self._sleep = sleep
        self._container_runner = Path(__file__).with_name(
            "python_validator_container.py"
        ).resolve()
        if not self._container_runner.is_file():
            raise PythonValidationError(
                f"Container-Runner fehlt: {self._container_runner}"
            )

    def _resolve_project(self, project_path: str) -> Path:
        if not isinstance(project_path, str) or not project_path.strip():
            raise PythonValidationError("Der Projektpfad darf nicht leer sein.")

        candidate = Path(project_path)
        if candidate.is_absolute():
            raise PythonValidationError(
                "Der Projektpfad muss relativ zum Workspace sein."
            )
        if ".." in PurePath(project_path).parts:
            raise PythonValidationError(
                "Der Projektpfad darf '..' nicht enthalten."
            )

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
            raise PythonValidationError(
                "Alle Startargumente müssen Strings sein."
            )
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
        return (result.stdout + result.stderr).strip()

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

    def validate(
        self,
        project_path: str,
        entrypoint: str,
        *,
        entrypoint_type: Literal["file", "module"] = "file",
        arguments: Sequence[str] | None = None,
        expect_long_running: bool = True,
    ) -> dict[str, Any]:
        project = self._resolve_project(project_path)
        self._validate_entrypoint(project, entrypoint, entrypoint_type)
        start_arguments = self._validate_arguments(arguments)

        container_name = f"cli-agent-python-validator-{uuid.uuid4().hex[:12]}"
        ready_token = f"PYTHON_VALIDATOR_READY_{uuid.uuid4().hex}"
        steps: list[dict[str, Any]] = []
        container_created = False
        final_state: dict[str, Any] | None = None
        logs = ""
        success = False
        removed = False

        try:
            try:
                docker_version = self._command(
                    ["docker", "version", "--format", "{{.Server.Version}}"],
                    timeout=10,
                )
            except FileNotFoundError:
                steps.append(
                    {
                        "name": "docker_available",
                        "success": False,
                        "detail": "Der docker-Befehl wurde nicht gefunden.",
                    }
                )
                return self._result(
                    False,
                    project_path,
                    entrypoint,
                    entrypoint_type,
                    start_arguments,
                    expect_long_running,
                    steps,
                    None,
                    "",
                    False,
                )
            except subprocess.TimeoutExpired:
                steps.append(
                    {
                        "name": "docker_available",
                        "success": False,
                        "detail": "Docker antwortete nicht innerhalb von 10 Sekunden.",
                    }
                )
                return self._result(
                    False,
                    project_path,
                    entrypoint,
                    entrypoint_type,
                    start_arguments,
                    expect_long_running,
                    steps,
                    None,
                    "",
                    False,
                )

            docker_ok = docker_version.returncode == 0
            steps.append(
                {
                    "name": "docker_available",
                    "success": docker_ok,
                    "detail": (
                        docker_version.stdout.strip()
                        if docker_ok
                        else docker_version.stderr.strip()
                    ),
                }
            )
            if not docker_ok:
                return self._result(
                    False,
                    project_path,
                    entrypoint,
                    entrypoint_type,
                    start_arguments,
                    expect_long_running,
                    steps,
                    None,
                    "",
                    False,
                )

            docker_run = [
                "docker",
                "run",
                "--detach",
                "--name",
                container_name,
                "--init",
                "--pull",
                "missing",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges:true",
                "--pids-limit",
                str(self.settings.pids_limit),
                "--memory",
                self.settings.memory_limit,
                "--cpus",
                self.settings.cpu_limit,
                "--mount",
                f"type=bind,src={project},dst=/source,readonly",
                "--mount",
                (
                    f"type=bind,src={self._container_runner},"
                    "dst=/validator/runner.py,readonly"
                ),
                "--env",
                f"PYTHON_VALIDATOR_READY_TOKEN={ready_token}",
                self.settings.python_image,
                "python",
                "/validator/runner.py",
                "--entrypoint-type",
                entrypoint_type,
                "--entrypoint",
                entrypoint,
                "--",
                *start_arguments,
            ]

            try:
                started = self._command(
                    docker_run,
                    timeout=self.settings.setup_timeout_seconds,
                )
            except subprocess.TimeoutExpired:
                steps.append(
                    {
                        "name": "container_created",
                        "success": False,
                        "detail": "Docker-Start oder Image-Pull lief in ein Timeout.",
                    }
                )
                removed, cleanup_detail = self._remove_container(container_name)
                steps.append(
                    {
                        "name": "container_removed",
                        "success": removed,
                        "detail": cleanup_detail,
                    }
                )
                return self._result(
                    False,
                    project_path,
                    entrypoint,
                    entrypoint_type,
                    start_arguments,
                    expect_long_running,
                    steps,
                    None,
                    "",
                    removed,
                )

            container_created = started.returncode == 0
            steps.append(
                {
                    "name": "container_created",
                    "success": container_created,
                    "detail": (
                        started.stdout.strip()
                        if container_created
                        else started.stderr.strip()
                    ),
                }
            )
            if not container_created:
                removed, cleanup_detail = self._remove_container(container_name)
                steps.append(
                    {
                        "name": "container_removed",
                        "success": removed,
                        "detail": cleanup_detail,
                    }
                )
                return self._result(
                    False,
                    project_path,
                    entrypoint,
                    entrypoint_type,
                    start_arguments,
                    expect_long_running,
                    steps,
                    None,
                    started.stderr.strip(),
                    removed,
                )

            deadline = time.monotonic() + self.settings.setup_timeout_seconds
            environment_ready = False
            while time.monotonic() < deadline:
                logs = self._logs(container_name)
                if any(line.strip() == ready_token for line in logs.splitlines()):
                    environment_ready = True
                    break
                final_state = self._state(container_name)
                if final_state and not final_state.get("Running", False):
                    break
                self._sleep(0.25)

            steps.append(
                {
                    "name": "project_built",
                    "success": environment_ready,
                    "detail": (
                        "Projekt kopiert, Syntax geprüft und Abhängigkeiten "
                        "installiert."
                        if environment_ready
                        else "Projektaufbau wurde nicht erfolgreich abgeschlossen."
                    ),
                }
            )
            if environment_ready:
                self._sleep(self.settings.startup_grace_seconds)
                final_state = self._state(container_name)
                logs = self._logs(container_name)
                running = bool(
                    final_state and final_state.get("Running", False)
                )
                exited_cleanly = bool(
                    final_state
                    and final_state.get("Status") == "exited"
                    and final_state.get("ExitCode") == 0
                )
                process_ok = running or (
                    not expect_long_running and exited_cleanly
                )
                steps.append(
                    {
                        "name": "process_started",
                        "success": process_ok,
                        "detail": self._state_detail(
                            final_state,
                            expect_long_running=expect_long_running,
                        ),
                    }
                )
                success = process_ok
            else:
                final_state = self._state(container_name)
                logs = self._logs(container_name)
        finally:
            if container_created:
                removed, cleanup_detail = self._remove_container(container_name)
                steps.append(
                    {
                        "name": "container_removed",
                        "success": removed,
                        "detail": cleanup_detail,
                    }
                )

        return self._result(
            success,
            project_path,
            entrypoint,
            entrypoint_type,
            start_arguments,
            expect_long_running,
            steps,
            final_state,
            logs,
            removed,
        )

    def _result(
        self,
        success: bool,
        project_path: str,
        entrypoint: str,
        entrypoint_type: str,
        arguments: list[str],
        expect_long_running: bool,
        steps: list[dict[str, Any]],
        state: dict[str, Any] | None,
        logs: str,
        container_removed: bool,
    ) -> dict[str, Any]:
        return {
            "success": success,
            "project_path": project_path,
            "python_image": self.settings.python_image,
            "start": {
                "entrypoint_type": entrypoint_type,
                "entrypoint": entrypoint,
                "arguments": arguments,
                "expect_long_running": expect_long_running,
            },
            "steps": steps,
            "container_state": state,
            "container_removed": container_removed,
            "logs": logs,
            "scope": (
                "Only dependency installation and process startup were checked; "
                "no functional test was executed."
            ),
        }

    @staticmethod
    def _state_detail(
        state: dict[str, Any] | None,
        *,
        expect_long_running: bool,
    ) -> str:
        if state is None:
            return "Containerstatus konnte nicht gelesen werden."
        if state.get("Running", False):
            return "Prozess läuft nach der Startwartezeit weiter."
        status = state.get("Status", "unknown")
        exit_code = state.get("ExitCode")
        if status == "exited" and exit_code == 0 and expect_long_running:
            return (
                "Prozess wurde fehlerfrei beendet, sollte aber dauerhaft laufen."
            )
        return f"Containerstatus={status}, ExitCode={exit_code}"
