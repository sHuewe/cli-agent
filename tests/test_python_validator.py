from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cli_agent.python_validator import (
    DockerPythonValidator,
    PythonValidationError,
    ValidatorSettings,
    is_pinned_image,
)


PINNED_IMAGE = "registry.internal/python@sha256:" + "a" * 64


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
            return subprocess.CompletedProcess(command, 0, "container-id\n", "")
        if command[:2] == ["docker", "logs"]:
            logs = self.ready_token + "\nserver started\n"
            return subprocess.CompletedProcess(command, 0, logs, "")
        if command[:2] == ["docker", "inspect"]:
            state = {"Status": "running", "Running": True, "ExitCode": 0}
            return subprocess.CompletedProcess(command, 0, json.dumps(state), "")
        if command[:3] == ["docker", "rm", "--force"]:
            return subprocess.CompletedProcess(command, 0, command[-1] + "\n", "")
        raise AssertionError(f"Unexpected command: {command}")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "main.py").write_text("print('ok')\n", encoding="utf-8")
    return tmp_path


def test_rejects_paths_outside_workspace(project: Path) -> None:
    validator = DockerPythonValidator(
        project, ValidatorSettings(python_image=PINNED_IMAGE)
    )

    with pytest.raises(PythonValidationError, match="relativ"):
        validator.validate(str(project), "main.py")
    with pytest.raises(PythonValidationError, match=r"\.\."):
        validator.validate("../other", "main.py")


def test_pinned_image_policy() -> None:
    digest = "sha256:" + "a" * 64

    assert is_pinned_image("registry.internal/python@" + digest)
    assert not is_pinned_image("registry.internal/python:3.12-slim")
    assert not is_pinned_image("registry.internal/python@sha256:" + "a" * 63)

    with pytest.raises(ValueError, match="sha256-Digest"):
        ValidatorSettings(
            python_image="registry.internal/python:3.12-slim",
            require_pinned_image=True,
        )

    settings = ValidatorSettings(
        python_image="registry.internal/python@" + digest,
        require_pinned_image=True,
    )
    assert settings.require_pinned_image is True


def test_builds_starts_and_removes_container(project: Path) -> None:
    docker = FakeDocker()
    settings = ValidatorSettings(
        python_image=PINNED_IMAGE,
        startup_grace_seconds=0,
    )
    validator = DockerPythonValidator(
        project,
        settings,
        command_runner=docker,
        sleep=lambda _: None,
    )

    result = validator.validate(".", "main.py")

    assert result["success"] is True
    assert result["validator_policy"]["image_pinned"] is True
    assert result["validator_policy"]["network_mode"] == "none"
    assert result["container_removed"] is True
    assert result["start"]["argument_count"] == 0
    assert [step["name"] for step in result["steps"]] == [
        "docker_available",
        "container_created",
        "project_built",
        "process_started",
        "container_removed",
    ]

    run_command = next(
        command for command in docker.commands if command[:2] == ["docker", "run"]
    )
    assert "--cap-drop" in run_command
    assert "no-new-privileges:true" in run_command
    assert run_command[run_command.index("--user") + 1] == "65532:65532"
    assert "--read-only" in run_command
    assert "/tmp:rw,nosuid,nodev" in run_command
    assert run_command[run_command.index("--network") + 1] == "none"
    assert run_command[run_command.index("--pull") + 1] == "never"
    mounts = [
        run_command[index + 1]
        for index, value in enumerate(run_command)
        if value == "--mount"
    ]
    assert any("dst=/source,readonly" in mount for mount in mounts)
    assert all("docker.sock" not in mount for mount in mounts)
    assert not any(value in {"sh", "bash", "cmd.exe"} for value in run_command)


def test_validator_mounts_sanitized_project_copy(project: Path) -> None:
    (project / ".env").write_text("SECRET=must-not-be-mounted\n", encoding="utf-8")
    observed: dict[str, bool] = {}

    class InspectingDocker(FakeDocker):
        def __call__(self, command, **kwargs):
            if command[:2] == ["docker", "run"]:
                mounts = [
                    command[index + 1]
                    for index, value in enumerate(command)
                    if value == "--mount"
                ]
                source_mount = next(mount for mount in mounts if "dst=/source" in mount)
                source = Path(source_mount.split("src=", 1)[1].split(",dst=", 1)[0])
                observed["is_staged"] = source != project.resolve()
                observed["contains_main"] = (source / "main.py").is_file()
                observed["contains_env"] = (source / ".env").exists()
            return super().__call__(command, **kwargs)

    result = DockerPythonValidator(
        project,
        ValidatorSettings(python_image=PINNED_IMAGE, startup_grace_seconds=0),
        command_runner=InspectingDocker(),
        sleep=lambda _: None,
    ).validate(".", "main.py")

    assert result["success"] is True
    assert observed == {
        "is_staged": True,
        "contains_main": True,
        "contains_env": False,
    }


def test_validator_truncates_untrusted_container_logs(project: Path) -> None:
    validator = DockerPythonValidator(
        project,
        ValidatorSettings(python_image=PINNED_IMAGE),
        command_runner=lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, "x" * 250_000, ""
        ),
    )

    logs = validator._logs("container")

    assert len(logs) < 201_000
    assert logs.endswith("[Validator-Logs wegen Größenlimit abgeschnitten]")


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
                return subprocess.CompletedProcess(command, 0, json.dumps(state), "")
            return super().__call__(command, **kwargs)

    settings = ValidatorSettings(
        python_image=PINNED_IMAGE,
        startup_grace_seconds=0,
    )

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
