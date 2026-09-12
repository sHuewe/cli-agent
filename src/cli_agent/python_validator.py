from __future__ import annotations

import shutil
import subprocess  # nosec B404
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Literal

from .python_validator_docker import (
    VALIDATOR_IGNORED_NAMES,
    DockerValidatorSupportMixin,
)
from .python_validator_types import (
    CommandRunner,
    PythonValidationError,
    ValidatorSettings,
    is_pinned_image,
)


class DockerPythonValidator(DockerValidatorSupportMixin):
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
        self._container_runner = (
            Path(__file__).with_name("python_validator_container.py").resolve()
        )
        if not self._container_runner.is_file():
            raise PythonValidationError(
                f"Container-Runner fehlt: {self._container_runner}"
            )

    @staticmethod
    def _stage_project(
        project: Path,
    ) -> tuple[tempfile.TemporaryDirectory[str], Path]:
        """Create a sanitized, temporary copy for the container mount.

        The original workspace must never be mounted into the container: a
        read-only mount still exposes any credentials that happen to be there
        to untrusted project code.
        """
        staging_directory = tempfile.TemporaryDirectory(prefix="cli-agent-validator-")
        staged_project = Path(staging_directory.name) / "project"
        try:
            shutil.copytree(
                project,
                staged_project,
                symlinks=True,
                ignore=shutil.ignore_patterns(*VALIDATOR_IGNORED_NAMES),
            )
        except (OSError, shutil.Error) as exc:
            staging_directory.cleanup()
            raise PythonValidationError(
                "Das Python-Projekt konnte nicht für die isolierte Validierung "
                "bereitgestellt werden."
            ) from exc
        return staging_directory, staged_project

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
        staging_directory, staged_project = self._stage_project(project)

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
                "never",
                "--user",
                "65532:65532",
                "--read-only",
                "--tmpfs",
                "/" + "tmp:rw,nosuid,nodev",
                "--network",
                self.settings.network_mode,
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
                f"type=bind,src={staged_project},dst=/source,readonly",
                "--mount",
                (
                    f"type=bind,src={self._container_runner},"
                    "dst=/validator/runner.py,readonly"
                ),
                "--env",
                f"PYTHON_VALIDATOR_READY_TOKEN={ready_token}",
                "--env",
                "HOME=/tmp/home",
                "--env",
                "PYTHONUSERBASE=/tmp/python-user",
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
                running = bool(final_state and final_state.get("Running", False))
                exited_cleanly = bool(
                    final_state
                    and final_state.get("Status") == "exited"
                    and final_state.get("ExitCode") == 0
                )
                process_ok = running or (not expect_long_running and exited_cleanly)
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
            try:
                if container_created:
                    removed, cleanup_detail = self._remove_container(container_name)
                    steps.append(
                        {
                            "name": "container_removed",
                            "success": removed,
                            "detail": cleanup_detail,
                        }
                    )
            finally:
                staging_directory.cleanup()

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
            "validator_policy": {
                "image_pinned": is_pinned_image(self.settings.python_image),
                "require_pinned_image": self.settings.require_pinned_image,
                "network_mode": self.settings.network_mode,
                "source_mount": "sanitized-read-only",
                "container_user": "65532:65532",
                "root_filesystem": "read-only",
            },
            "start": {
                "entrypoint_type": entrypoint_type,
                "entrypoint": entrypoint,
                "argument_count": len(arguments),
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
            return "Prozess wurde fehlerfrei beendet, sollte aber dauerhaft laufen."
        return f"Containerstatus={status}, ExitCode={exit_code}"
