from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cli_agent import cli as cli_module
from cli_agent.admin_config import AdminConfig, McpPolicy
from cli_agent.cli import (
    _approval_arguments,
    apply_mcp_cli_overrides,
    apply_model_cli_override,
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
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--with-os-read", "--with-os-write"])


def test_with_os_read_adds_read_only_server() -> None:
    config = apply_mcp_cli_overrides(AppConfig(), os_access="read")
    server = config.mcp_servers[0]
    assert server.name == "os"
    assert server.command == "{python}"
    assert server.allow_write_files() is False
    assert server.args[-2:] == ("--access", "read")


def test_with_os_write_replaces_configured_os_server() -> None:
    original = AppConfig(
        mcp_servers=(
            McpServerConfig(name="external", command="external-server"),
            McpServerConfig(name="os", command="legacy-os-server"),
        )
    )
    config = apply_mcp_cli_overrides(original, os_access="write")
    assert [server.name for server in config.mcp_servers] == ["external", "os"]
    assert config.mcp_servers[1].allow_write_files() is True


def test_without_os_cli_flag_keeps_config_unchanged() -> None:
    original = AppConfig(mcp_servers=(McpServerConfig(name="external", command="external-server"),))
    assert apply_mcp_cli_overrides(original, os_access=None) is original


def test_os_server_explicit_access_overrides_config() -> None:
    configured = (McpServerConfig(name="os", command="unused", config={"allow_write_files": True}),)
    assert resolve_mcp_config(configured, access="read").allow_write_files() is False
    assert resolve_mcp_config((), access="write").allow_write_files() is True


def test_model_cli_argument_is_parsed() -> None:
    assert build_parser().parse_args(["--model", "gwen100"]).model == "gwen100"


def test_validator_cli_option_is_no_longer_available() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--with-python-validator"])


def test_run_uses_admin_policy_for_network_and_mcp(tmp_path, monkeypatch) -> None:
    config = AppConfig(
        model=ModelConfig(
            provider="openai",
            model="internal-model",
            base_url="https://llm.internal/v1",
        )
    )
    admin_config = AdminConfig(
        network=NetworkConfig(
            model_allowed_hosts=("llm.internal",),
            mcp_allowed_hosts=("mcp.internal",),
            web_allowed_hosts=("docs.internal",),
        ),
        mcp=McpPolicy(
            allow_untrusted_stdio=True,
            auto_approve_tools=("continuous__search",),
        ),
    )
    captured = {}

    class FakeAgent:
        def __init__(self, _workspace, model_client, *_args, **kwargs):
            captured["model_client"] = model_client
            captured["network"] = kwargs["network"]
            captured["mcp_policy"] = kwargs["mcp_policy"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def ask(self, _prompt):
            return "ok"

    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "load_admin_config", lambda: admin_config)
    monkeypatch.setattr(cli_module, "configure_logging", lambda _config: None)
    monkeypatch.setattr(cli_module, "WebContextCliAgent", FakeAgent)

    args = SimpleNamespace(
        config=None,
        model=None,
        os_access=None,
        debug=False,
        workspace=tmp_path,
        prompt=["hello"],
    )
    asyncio.run(cli_module.run(args))

    assert captured["model_client"].base_url == "https://llm.internal/v1"
    assert captured["network"] is admin_config.network
    assert captured["mcp_policy"] is admin_config.mcp


def test_model_cli_override_replaces_only_model_name() -> None:
    original = AppConfig(
        model=ModelConfig(
            provider="openai",
            model="configured-model",
            base_url="https://model-host.example/v1",
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
