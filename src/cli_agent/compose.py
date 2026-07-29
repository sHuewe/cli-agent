from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import AppConfig, McpServerConfig

logger = logging.getLogger(__name__)


COMPOSE_FILENAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)


class ComposeError(RuntimeError):
    """A Docker Compose command could not be executed."""


def find_compose_file(project_directory: Path) -> Path:
    project_directory = project_directory.resolve()
    if not project_directory.is_dir():
        raise ComposeError(f"Projektverzeichnis existiert nicht: {project_directory}")

    matches = [project_directory / name for name in COMPOSE_FILENAMES]
    matches = [path for path in matches if path.is_file()]
    if not matches:
        expected = ", ".join(COMPOSE_FILENAMES)
        logger.warning(
            "Keine Compose-Datei in %s gefunden (erwartet: %s). Docker Compose-Tools nicht verfügbar.",
            project_directory,
            expected,
        )
        return None
    return matches[0].relative_to(project_directory)


@dataclass(frozen=True)
class ComposeProject:
    directory: Path
    compose_file: Path
    config: McpServerConfig

    @classmethod
    def from_directory(cls, directory: Path, config: McpServerConfig) -> "ComposeProject":
        resolved = directory.resolve()
        return cls(resolved, find_compose_file(resolved), config)
        raise ComposeError(
            "Kein MCP-Server mit dem Namen 'compose' in der Konfiguration gefunden.")

    def command(self, *arguments: str) -> list[str]:
        docker_cmd = ["docker","compose"]
        if self.config.config.get("wsl", False):
            docker_cmd = ["wsl"] + docker_cmd
        return [
            *docker_cmd,
            "-f",
            str(self.compose_file),
            *arguments,
        ]

    def is_available(self) -> bool:
        return self.compose_file != None

    def run(self, *arguments: str, timeout: int = 60) -> str:
        command = self.command(*arguments)
        logger.info(
            "Running command=%r executable=%r cwd=%r",
            command,
            shutil.which(command[0]),
            str(self.directory),
        )
        try:
            completed = subprocess.run(
                command,
                cwd=self.directory,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise ComposeError(
                "Die Docker-CLI wurde nicht gefunden. Ist Docker installiert "
                "und im PATH verfügbar?"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ComposeError(
                f"Zeitüberschreitung bei Docker Compose nach {timeout} Sekunden."
            ) from exc

        if completed.returncode != 0:
            details = (completed.stderr or completed.stdout).strip()
            logger.error(
                "Command failed returncode=%s stdout=%r stderr=%r",
                completed.returncode,
                completed.stdout,
                completed.stderr,
            )
            raise ComposeError(
                f"Docker Compose endete mit Code {completed.returncode}: {details}"
            )
        output = "\n".join(
            part.strip()
            for part in (completed.stdout, completed.stderr)
                if part.strip()
        )
        return output.strip()

    def services(self) -> list[str]:
        output = self.run("config", "--services")
        return [line.strip() for line in output.splitlines() if line.strip()]

    def validate_service(self, service_name: str) -> str:
        if not service_name or service_name != service_name.strip():
            raise ComposeError("Der Servicename darf nicht leer sein.")
        services = self.services()
        if service_name not in services:
            available = ", ".join(services) if services else "(keine)"
            raise ComposeError(
                f"Unbekannter Service {service_name!r}. Verfügbar: {available}"
            )
        return service_name

    def read_compose_file(self) -> str:
        return self.compose_file.read_text(encoding="utf-8")

    def ps(self) -> str:
        return self.run("ps")

    def up_all(self) -> str:
        return self.run("up", "-d", timeout=120)

    def start(self, service_name: str) -> str:
        service = self.validate_service(service_name)
        return self.run("up", "-d", service, timeout=120)

    def stop(self, service_name: str) -> str:
        service = self.validate_service(service_name)
        return self.run("stop", service, timeout=120)

    def restart(self, service_name: str) -> str:
        service = self.validate_service(service_name)
        return self.run("restart", service, timeout=120)

    def logs(self, service_name: str) -> str:
        service = self.validate_service(service_name)
        return self.run("logs", "--tail", "20", "--no-color", service)
