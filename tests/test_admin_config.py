from __future__ import annotations

from pathlib import Path, PureWindowsPath

import pytest

import cli_agent.admin_config as admin_config_module
from cli_agent.admin_config import default_admin_config_file, load_admin_config
from cli_agent.config import load_config


def test_admin_config_defaults_are_restrictive(tmp_path: Path) -> None:
    config = load_admin_config(tmp_path / "missing.toml")

    assert config.network.model_allowed_hosts == ("localhost", "127.0.0.1", "::1")
    assert config.network.mcp_allowed_hosts == config.network.model_allowed_hosts
    assert config.network.web_allowed_hosts == ()
    assert config.mcp.allow_untrusted_stdio is False
    assert config.mcp.trusted_servers == ()


def test_windows_admin_config_path_ignores_programdata_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(admin_config_module.platform, "system", lambda: "Windows")
    monkeypatch.setenv("PROGRAMDATA", r"C:\Users\attacker\policy")

    assert PureWindowsPath(default_admin_config_file()) == PureWindowsPath(
        r"C:\ProgramData\cli-agent\admin_config.toml"
    )


def test_admin_config_loads_network_and_mcp_policy(tmp_path: Path) -> None:
    path = tmp_path / "admin.toml"
    path.write_text(
        """
[network]
model_allowed_hosts = ["LLM.INTERNAL."]
mcp_allowed_hosts = ["mcp.internal"]
web_allowed_hosts = ["docs.internal"]

[mcp]
allow_untrusted_stdio = true

[[mcp.trusted_servers]]
name = "continuous"
transport = "streamable_http"
url = "https://MCP.INTERNAL./mcp"
auto_approve_tools = ["search"]
""".strip(),
        encoding="utf-8",
    )

    config = load_admin_config(path)

    assert config.network.model_allowed_hosts == ("llm.internal",)
    assert config.network.mcp_allowed_hosts == ("mcp.internal",)
    assert config.network.web_allowed_hosts == ("docs.internal",)
    assert config.mcp.allow_untrusted_stdio is True
    assert len(config.mcp.trusted_servers) == 1
    trusted = config.mcp.trusted_servers[0]
    assert trusted.name == "continuous"
    assert trusted.transport == "streamable_http"
    assert trusted.url == "https://mcp.internal/mcp"
    assert trusted.auto_approve_tools == ("search",)


def test_admin_config_accepts_empty_optional_lists(tmp_path: Path) -> None:
    path = tmp_path / "admin.toml"
    path.write_text(
        """
[network]
model_allowed_hosts = ["localhost", "127.0.0.1", "::1", "openrouter.ai"]
mcp_allowed_hosts = ["localhost", "127.0.0.1", "::1"]
web_allowed_hosts = []

[mcp]
allow_untrusted_stdio = false
""".strip(),
        encoding="utf-8",
    )

    config = load_admin_config(path)

    assert "openrouter.ai" in config.network.model_allowed_hosts
    assert config.network.web_allowed_hosts == ()
    assert config.mcp.trusted_servers == ()


def test_admin_config_rejects_legacy_name_only_auto_approval(tmp_path: Path) -> None:
    path = tmp_path / "admin.toml"
    path.write_text(
        """
[mcp.approval]
auto_approve_tools = ["continuous__search"]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="trusted_servers"):
        load_admin_config(path)


def test_admin_config_rejects_duplicate_trusted_server_names(tmp_path: Path) -> None:
    path = tmp_path / "admin.toml"
    path.write_text(
        """
[[mcp.trusted_servers]]
name = "continuous"
transport = "streamable_http"
url = "https://mcp.internal/a"

[[mcp.trusted_servers]]
name = "continuous"
transport = "streamable_http"
url = "https://mcp.internal/b"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="doppelt"):
        load_admin_config(path)


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("[network]\nmodel_allowed_hosts = \"llm.internal\"\n", "model_allowed_hosts"),
        ("[mcp]\nallow_untrusted_stdio = \"true\"\n", "allow_untrusted_stdio"),
        (
            "[network]\nmodel_allowed_hosts = [\"LLM.INTERNAL\", \"llm.internal.\"]\n",
            "doppelten Hosts",
        ),
        (
            "[[mcp.trusted_servers]]\nname = \"x\"\ntransport = \"streamable_http\"\nurl = \"ftp://example.org/mcp\"\n",
            "http",
        ),
    ],
)
def test_admin_config_rejects_invalid_security_policy(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    path = tmp_path / "admin.toml"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_admin_config(path)


def test_user_config_rejects_network_policy(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[network]\nmodel_allowed_hosts = [\"example.org\"]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="admin_config|Netzwerk"):
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
