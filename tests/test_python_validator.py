from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cli_agent.python_validator import (
    DockerPythonValidator,
    PythonValidationError,
    ValidatorSettings,
)


class FakeDocker:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.ready_token = ""

    def __call__(
        self,
        command: list[str],
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(command)
        if command[:2] == ["docker", "version"]:
            return subprocess.CompletedProcess(command, 0, "27.0.0\n", "")
        if command[:2] == ["docker", "run"]:
            env_index = command.index("--env")
            self.ready_token = command[env_index + 1].split("=", 1)[1]
            return subprocess.CompletedProcess(
                command, 0, "container-id\n", ""
            )
        if command[:2] == ["docker", "logs"]:
            logs = self.ready_token + "\nserver started\n"
            return subprocess.CompletedProcess(command, 0, logs, "")
        if command[:2] == ["docker", "inspect"]:
            state = {"Status": "running", "Running": True, "ExitCode": 0}
            return subprocess.CompletedProcess(
                command, 0, json.dumps(state), ""
            )
        if command[:3] == ["docker", "rm", "--force"]:
            return subprocess.CompletedProcess(
                command, 0, command[-1] + "\n", ""
            )
        raise AssertionError(f"Unexpected command: {command}")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    return tmp_path


def test_rejects_paths_outside_workspace(project: Path) -> None:
    validator = DockerPythonValidator(project)

    with pytest.raises(PythonValidationError, match="relativ"):
        validator.validate(str(project), "main.py")
    with pytest.raises(PythonValidationError, match=r"\.\."):
        validator.validate("../other", "main.py")


def test_builds_starts_and_removes_container(project: Path) -> None:
    docker = FakeDocker()
    settings = ValidatorSettings(startup_grace_seconds=0)
    validator = DockerPythonValidator(
        project,
        settings,
        command_runner=docker,
        sleep=lambda _: None,
    )

    result = validator.validate(".", "main.py")

    assert result["success"] is True
    assert result["container_removed"] is True
    assert [step["name"] for step in result["steps"]] == [
        "docker_available",
        "container_created",
        "project_built",
        "process_started",
        "container_removed",
    ]

    run_command = next(
        command
        for command in docker.commands
        if command[:2] == ["docker", "run"]
    )
    assert "--cap-drop" in run_command
    assert "no-new-privileges:true" in run_command
    mounts = [
        run_command[index + 1]
        for index, value in enumerate(run_command)
        if value == "--mount"
    ]
    assert any("dst=/source,readonly" in mount for mount in mounts)
    assert all("docker.sock" not in mount for mount in mounts)
    assert not any(
        value in {"sh", "bash", "cmd.exe"} for value in run_command
    )


def test_clean_exit_is_only_accepted_for_short_lived_program(
    project: Path,
) -> None:
    class ExitedDocker(FakeDocker):
        def __call__(
            self,
            command: list[str],
            **kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            if command[:2] == ["docker", "inspect"]:
                self.commands.append(command)
                state = {
                    "Status": "exited",
                    "Running": False,
                    "ExitCode": 0,
                }
                return subprocess.CompletedProcess(
                    command, 0, json.dumps(state), ""
                )
            return super().__call__(command, **kwargs)

    settings = ValidatorSettings(startup_grace_seconds=0)

    long_running_result = DockerPythonValidator(
        project,
        settings,
        command_runner=ExitedDocker(),
        sleep=lambda _: None,
    ).validate(".", "main.py", expect_long_running=True)
    short_lived_result = DockerPythonValidator(
        project,
        settings,
        command_runner=ExitedDocker(),
        sleep=lambda _: None,
    ).validate(".", "main.py", expect_long_running=False)

    assert long_running_result["success"] is False
    assert short_lived_result["success"] is True
