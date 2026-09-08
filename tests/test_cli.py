from __future__ import annotations

import pytest

from cli_agent.cli import apply_mcp_cli_overrides, apply_model_cli_override, build_parser
from cli_agent.config import AppConfig, McpServerConfig, ModelConfig
from cli_agent.os_mcp_server import resolve_mcp_config


def test_os_cli_flags_are_mutually_exclusive() -> None:
    parser = build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--with-os-read", "--with-os-write"])


def test_with_os_read_adds_read_only_server() -> None:
    config = apply_mcp_cli_overrides(AppConfig(), os_access="read")

    assert len(config.mcp_servers) == 1
    server = config.mcp_servers[0]
    assert server.name == "os"
    assert server.command == "{python}"
    assert server.allow_write_files() is False
    assert server.args[-2:] == ("--access", "read")


def test_with_os_write_replaces_configured_os_server() -> None:
    original = AppConfig(
        mcp_servers=(
            McpServerConfig(name="compose", command="compose-server"),
            McpServerConfig(
                name="os",
                command="legacy-os-server",
                config={"allow_write_files": False},
            ),
        )
    )

    config = apply_mcp_cli_overrides(original, os_access="write")

    assert [server.name for server in config.mcp_servers] == ["compose", "os"]
    os_server = config.mcp_servers[1]
    assert os_server.command == "{python}"
    assert os_server.allow_write_files() is True
    assert os_server.args[-2:] == ("--access", "write")


def test_without_os_cli_flag_keeps_config_unchanged() -> None:
    original = AppConfig(
        mcp_servers=(McpServerConfig(name="compose", command="compose-server"),)
    )

    assert apply_mcp_cli_overrides(original, os_access=None) is original


def test_os_server_explicit_access_overrides_config() -> None:
    configured = (
        McpServerConfig(
            name="os",
            command="unused",
            config={"allow_write_files": True},
        ),
    )

    read_config = resolve_mcp_config(configured, access="read")
    write_config = resolve_mcp_config((), access="write")

    assert read_config.allow_write_files() is False
    assert write_config.allow_write_files() is True


def test_with_python_validator_adds_default_server() -> None:
    config = apply_mcp_cli_overrides(
        AppConfig(),
        os_access=None,
        with_python_validator=True,
    )

    assert len(config.mcp_servers) == 1
    server = config.mcp_servers[0]
    assert server.name == "python-validator"
    assert server.transport == "stdio"
    assert server.command == "{python}"
    assert server.args == (
        "-m",
        "cli_agent.python_validator_mcp",
        "--project-directory",
        "{workspace_directory}",
        "--python-image",
        "python:3.12-slim",
        "--config-file",
        "{config_file}",
    )


def test_with_python_validator_replaces_configured_server() -> None:
    original = AppConfig(
        mcp_servers=(
            McpServerConfig(name="compose", command="compose-server"),
            McpServerConfig(
                name="python-validator",
                command="custom-validator",
                args=("--custom",),
            ),
        )
    )

    config = apply_mcp_cli_overrides(
        original,
        os_access=None,
        with_python_validator=True,
    )

    assert [server.name for server in config.mcp_servers] == [
        "compose",
        "python-validator",
    ]
    validator = config.mcp_servers[1]
    assert validator.command == "{python}"
    assert validator.args[-2:] == ("--config-file", "{config_file}")
    assert validator.args[-4:-2] == ("--python-image", "python:3.12-slim")


def test_os_and_python_validator_cli_overrides_can_be_combined() -> None:
    config = apply_mcp_cli_overrides(
        AppConfig(),
        os_access="read",
        with_python_validator=True,
    )

    assert [server.name for server in config.mcp_servers] == [
        "os",
        "python-validator",
    ]


def test_model_cli_argument_is_parsed() -> None:
    args = build_parser().parse_args(["--model", "gwen100"])

    assert args.model == "gwen100"


def test_model_cli_override_replaces_only_model_name() -> None:
    original = AppConfig(
        model=ModelConfig(
            provider="openai-compatible",
            model="configured-model",
            base_url="http://model-host:8000/v1",
            api_key_env="MODEL_API_KEY",
            timeout=42.0,
            headers={"X-Test": "value"},
        )
    )

    config = apply_model_cli_override(original, model="gwen100")

    assert config.model.model == "gwen100"
    assert config.model.provider == original.model.provider
    assert config.model.base_url == original.model.base_url
    assert config.model.api_key_env == original.model.api_key_env
    assert config.model.timeout == original.model.timeout
    assert config.model.headers == original.model.headers


def test_without_model_cli_argument_keeps_config_unchanged() -> None:
    original = AppConfig(model=ModelConfig(model="configured-model"))

    assert apply_model_cli_override(original, model=None) is original
