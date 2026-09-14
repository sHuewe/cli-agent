from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent.config import load_config


@pytest.mark.parametrize(
    "header_name",
    [
        "Host",
        ":authority",
        "Forwarded",
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "X-Original-Host",
        "Proxy-Authorization",
        "Connection",
        "Upgrade",
    ],
)
def test_model_config_rejects_routing_and_proxy_headers(tmp_path: Path, header_name: str) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'''
[model]
provider = "openai"
model = "test-model"
base_url = "https://allowed.example/v1"

[model.headers]
"{header_name}" = "attacker-controlled"
'''.strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="routing|Header"):
        load_config(config_file)


@pytest.mark.parametrize(
    "header_name",
    ["Host", "Forwarded", "X-Forwarded-Host", "X-Original-Host", "Proxy-Connection"],
)
def test_mcp_config_rejects_routing_headers(tmp_path: Path, header_name: str) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'''
[[mcp_servers]]
name = "external"
transport = "streamable_http"
url = "https://allowed.example/mcp"

[mcp_servers.headers]
"{header_name}" = "other-backend.example"
'''.strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="routing|Header"):
        load_config(config_file)


def test_normal_authentication_headers_remain_allowed(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '''
[[mcp_servers]]
name = "external"
transport = "streamable_http"
url = "https://allowed.example/mcp"

[mcp_servers.headers]
Authorization = "Bearer token"
X-Client-Id = "cli-agent"
'''.strip(),
        encoding="utf-8",
    )

    server = load_config(config_file).mcp_servers[0]

    assert server.headers == {
        "Authorization": "Bearer token",
        "X-Client-Id": "cli-agent",
    }
