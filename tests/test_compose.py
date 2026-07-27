from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from cli_agent.compose import ComposeError, ComposeProject, find_compose_file


def test_find_compose_file_prefers_compose_yaml(tmp_path: Path) -> None:
    (tmp_path / "docker-compose.yml").write_text("services: {}", encoding="utf-8")
    expected = tmp_path / "compose.yaml"
    expected.write_text("services: {}", encoding="utf-8")

    assert find_compose_file(tmp_path) == expected


def test_find_compose_file_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ComposeError, match="Keine Compose-Datei"):
        find_compose_file(tmp_path)


def test_command_fixes_directory_and_file(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}", encoding="utf-8")
    project = ComposeProject.from_directory(tmp_path)

    assert project.command("ps") == [
        "docker",
        "compose",
        "-f",
        str(compose_file.resolve()),
        "ps",
    ]


def test_logs_validates_service_and_uses_tail_20(tmp_path: Path) -> None:
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text("services: {}", encoding="utf-8")
    project = ComposeProject.from_directory(tmp_path)

    responses = [
        Mock(returncode=0, stdout="web\nworker\n", stderr=""),
        Mock(returncode=0, stdout="last line\n", stderr=""),
    ]
    with patch("cli_agent.compose.subprocess.run", side_effect=responses) as run:
        assert project.logs("web") == "last line"

    assert run.call_args_list[1].args[0][-5:] == [
        "logs",
        "--tail",
        "20",
        "--no-color",
        "web",
    ]


def test_logs_rejects_unknown_service(tmp_path: Path) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}", encoding="utf-8")
    project = ComposeProject.from_directory(tmp_path)
    response = Mock(returncode=0, stdout="web\n", stderr="")

    with patch("cli_agent.compose.subprocess.run", return_value=response):
        with pytest.raises(ComposeError, match="Unbekannter Service"):
            project.logs("database")


@pytest.mark.parametrize(
    ("method", "expected"),
    [
        ("start", ["up", "-d", "web"]),
        ("stop", ["stop", "web"]),
        ("restart", ["restart", "web"]),
    ],
)
def test_service_actions_validate_and_execute(
    tmp_path: Path,
    method: str,
    expected: list[str],
) -> None:
    (tmp_path / "compose.yaml").write_text("services: {}", encoding="utf-8")
    project = ComposeProject.from_directory(tmp_path)
    responses = [
        Mock(returncode=0, stdout="web\n", stderr=""),
        Mock(returncode=0, stdout="done\n", stderr=""),
    ]

    with patch("cli_agent.compose.subprocess.run", side_effect=responses) as run:
        assert getattr(project, method)("web") == "done"

    assert run.call_args_list[1].args[0][-len(expected):] == expected
