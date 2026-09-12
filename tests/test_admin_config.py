from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent.admin_config import load_admin_config
from cli_agent.config import load_config


def test_admin_config_defaults_are_restrictive(tmp_path: Path) -> None:
    config = load_admin_config(tmp_path / "missing.toml")
    assert "localhost" in config.network.model_allowed_hosts
    assert config.network.web_allowed_hosts == ()
    assert config.mcp.allow_untrusted_stdio is False
    assert config.mcp.auto_approve_tools == ()


def test_admin_config_loads_network_and_mcp_policy(tmp_path: Path) -> None:
    path = tmp_path / "admin.toml"
    path.write_text(
        """
[network]
model_allowed_hosts = ["llm.internal"]
mcp_allowed_hosts = ["mcp.internal"]
web_allowed_hosts = ["docs.internal"]

[mcp]
allow_untrusted_stdio = true

[mcp.approval]
auto_approve_tools = ["continuous__search"]
""".strip(),
        encoding="utf-8",
    )
    config = load_admin_config(path)
    assert config.network.model_allowed_hosts == ("llm.internal",)
    assert config.network.mcp_allowed_hosts == ("mcp.internal",)
    assert config.network.web_allowed_hosts == ("docs.internal",)
    assert config.mcp.allow_untrusted_stdio is True
    assert config.mcp.auto_approve_tools == ("continuous__search",)


def test_user_config_rejects_network_policy(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("[network]\nmodel_allowed_hosts = [\"example.org\"]\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Admin|admin_config|Netzwerk"):
        load_config(path)


def test_user_config_rejects_untrusted_stdio_policy(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        """
[[mcp_servers]]
name = "external"
transport = "stdio"
command = "external-mcp"

[mcp_servers.config]
allow_untrusted_stdio = true
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="admin_config"):
        load_config(path)
