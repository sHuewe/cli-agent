from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cli_agent import cli as cli_module
from cli_agent.cli import (
    apply_mcp_cli_overrides,
    apply_model_cli_override,
    _approval_arguments,
    build_parser,
)
from cli_agent.config import AppConfig, McpServerConfig, ModelConfig
from cli_agent.network_policy import NetworkConfig
from cli_agent.os_mcp_server import resolve_mcp_config


def test_approval_summary_hides_sensitive_tool_arguments() -> None:
    summary = _approval_arguments(
        {
            "path": "src/main.py",
            "content": "confidential source",
            "auth-header": "Bearer confidential-token",
            "options": {"api_key": "nested-secret"},
        }
    )

    assert "src/main.py" in summary
    assert "confidential source" not in summary
    assert "confidential-token" not in summary
    assert "nested-secret" not in summary
    assert "verborgen" in summary


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


def test_with_python_validator_requires_pinned_image() -> None:
    with pytest.raises(ValueError, match="gepinntes Image"):
        apply_mcp_cli_overrides(
            AppConfig(),
            os_access=None,
            with_python_validator=True,
        )

    with pytest.raises(ValueError, match="sha256-Digest"):
        apply_mcp_cli_overrides(
            AppConfig(),
            os_access=None,
            with_python_validator=True,
            python_validator_image="registry.internal/python:latest",
        )


def test_with_python_validator_adds_pinned_server() -> None:
    pinned_image = "registry.internal/python@sha256:" + "a" * 64
    config = apply_mcp_cli_overrides(
        AppConfig(),
        os_access=None,
        with_python_validator=True,
        python_validator_image=pinned_image,
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
        pinned_image,
        "--config-file",
        "{config_file}",
        "--network-mode",
        "none",
        "--require-pinned-image",
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
        python_validator_image="registry.internal/python@sha256:" + "a" * 64,
    )

    assert [server.name for server in config.mcp_servers] == [
        "compose",
        "python-validator",
    ]
    validator = config.mcp_servers[1]
    assert validator.command == "{python}"
    assert validator.args[-5:] == (
        "--config-file",
        "{config_file}",
        "--network-mode",
        "none",
        "--require-pinned-image",
    )
    assert validator.args[4:6] == (
        "--python-image",
        "registry.internal/python@sha256:" + "a" * 64,
    )


def test_python_validator_can_require_a_pinned_local_image() -> None:
    digest = "sha256:" + "b" * 64

    config = apply_mcp_cli_overrides(
        AppConfig(),
        os_access=None,
        python_validator_image="registry.internal/python@" + digest,
        require_pinned_validator_image=True,
    )

    args = config.mcp_servers[0].args
    assert "registry.internal/python@" + digest in args
    assert args[-1] == "--require-pinned-image"


def test_os_and_python_validator_cli_overrides_can_be_combined() -> None:
    config = apply_mcp_cli_overrides(
        AppConfig(),
        os_access="read",
        with_python_validator=True,
        python_validator_image="registry.internal/python@sha256:" + "a" * 64,
    )

    assert [server.name for server in config.mcp_servers] == [
        "os",
        "python-validator",
    ]


def test_model_cli_argument_is_parsed() -> None:
    args = build_parser().parse_args(["--model", "gwen100"])

    assert args.model == "gwen100"


def test_run_passes_network_policy_to_model_factory(tmp_path, monkeypatch) -> None:
    config = AppConfig(
        model=ModelConfig(
            provider="openai",
            model="internal-model",
            base_url="https://llm.internal/v1",
        ),
        network=NetworkConfig(model_allowed_hosts=("llm.internal",)),
    )
    captured = {}

    class FakeAgent:
        def __init__(self, _workspace, model_client, *_args, **_kwargs):
            captured["model_client"] = model_client

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def ask(self, _prompt):
            return "ok"

    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "configure_logging", lambda _config: None)
    monkeypatch.setattr(cli_module, "WebContextCliAgent", FakeAgent)
    args = SimpleNamespace(
        config=None,
        model=None,
        os_access=None,
        with_python_validator=False,
        python_validator_image=None,
        require_pinned_validator_image=False,
        debug=False,
        workspace=tmp_path,
        prompt=["hello"],
    )

    asyncio.run(cli_module.run(args))

    assert captured["model_client"].base_url == "https://llm.internal/v1"


def test_python_validator_security_flags_are_parsed() -> None:
    args = build_parser().parse_args(
        [
            "--with-python-validator",
            "--python-validator-image",
            "registry.internal/python@sha256:" + "c" * 64,
            "--require-pinned-validator-image",
        ]
    )

    assert args.with_python_validator is True
    assert args.python_validator_image.endswith("@sha256:" + "c" * 64)
    assert args.require_pinned_validator_image is True


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
