from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent import cli as cli_module
from cli_agent.admin_config import (
    AdminConfig,
    McpPolicy,
    TrustedMcpServer,
    TrustedMcpToolApproval,
)
from cli_agent.cli import (
    McpToolInspection,
    _inspect_mcp_tool,
    _render_tool_approval_fragment,
    build_admin_parser,
)
from cli_agent.config import McpServerConfig
from cli_agent.mcp_contracts import tool_contract_fingerprint
from cli_agent.network_policy import NetworkConfig

SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


def test_admin_cli_parses_inspect_and_trust_tool_commands() -> None:
    inspect = build_admin_parser().parse_args(
        ["inspect-tool", "fachsoftware", "search", "--config", "project.toml"]
    )
    trust = build_admin_parser().parse_args(
        ["trust-tool", "fachsoftware", "search", "--config", "project.toml", "--update"]
    )

    assert inspect.admin_command == "inspect-tool"
    assert inspect.server == "fachsoftware"
    assert inspect.tool == "search"
    assert trust.admin_command == "trust-tool"
    assert trust.update is True


def test_rendered_fragment_contains_only_copyable_pinned_approval() -> None:
    contract = tool_contract_fingerprint("search", SCHEMA, "Search")
    inspection = McpToolInspection(
        server_name="fachsoftware",
        transport="streamable_http",
        tool_name="search",
        description="Search",
        input_schema=SCHEMA,
        contract_sha256=contract,
        trusted_server_found=True,
        existing_contract_sha256=None,
    )

    fragment = _render_tool_approval_fragment(inspection)

    assert "[[mcp.trusted_servers.auto_approve_tools]]" in fragment
    assert 'name = "search"' in fragment
    assert f'contract_sha256 = "{contract}"' in fragment
    assert "admin_config.toml" not in fragment


def test_inspection_uses_live_tool_metadata_without_model_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = McpServerConfig(
        name="fachsoftware",
        transport="streamable_http",
        url="https://mcp.internal/mcp",
    )
    policy = McpPolicy(
        trusted_servers=(
            TrustedMcpServer(
                name="fachsoftware",
                transport="streamable_http",
                url="https://mcp.internal/mcp",
            ),
        )
    )
    admin = AdminConfig(
        network=NetworkConfig(mcp_allowed_hosts=("mcp.internal",)),
        mcp=policy,
    )

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            self._server_tools = {
                "fachsoftware": [
                    {
                        "function": {
                            "name": "fachsoftware__search",
                            "description": "Search records",
                            "parameters": SCHEMA,
                        }
                    }
                ]
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def _trusted_server_matches(self, _server, trusted):
            return trusted.name == "fachsoftware"

    monkeypatch.setattr(cli_module, "CliAgent", FakeAgent)

    inspection = asyncio.run(
        _inspect_mcp_tool(
            server=server,
            tool_name="fachsoftware__search",
            workspace=tmp_path,
            config_file=tmp_path / "config.toml",
            admin_config=admin,
        )
    )

    assert inspection.tool_name == "search"
    assert inspection.description == "Search records"
    assert inspection.input_schema == SCHEMA
    assert inspection.contract_sha256 == tool_contract_fingerprint("search", SCHEMA, "Search records")
    assert inspection.trusted_server_found is True


def test_inspection_reports_existing_pinned_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    contract = tool_contract_fingerprint("search", SCHEMA, "")
    server = McpServerConfig(name="fachsoftware")
    admin = AdminConfig(
        mcp=McpPolicy(
            trusted_servers=(
                TrustedMcpServer(
                    name="fachsoftware",
                    transport="stdio",
                    command="/trusted/fachsoftware",
                    auto_approve_tools=(TrustedMcpToolApproval("search", contract),),
                ),
            )
        )
    )

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            self._server_tools = {
                "fachsoftware": [
                    {"function": {"name": "fachsoftware__search", "parameters": SCHEMA}}
                ]
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def _trusted_server_matches(self, _server, _trusted):
            return True

    monkeypatch.setattr(cli_module, "CliAgent", FakeAgent)
    inspection = asyncio.run(
        _inspect_mcp_tool(
            server=server,
            tool_name="search",
            workspace=tmp_path,
            config_file=tmp_path / "config.toml",
            admin_config=admin,
        )
    )

    assert inspection.existing_contract_sha256 == contract
