from __future__ import annotations

import logging
from pathlib import Path

from cli_agent.config import load_config
from cli_agent.logging_setup import configure_logging


def test_load_logging_config(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[logging]
enabled = true
level = "DEBUG"
file = "agent.log"
log_prompts = false
log_model_messages = true
log_tool_results = true
max_bytes = 1234
backup_count = 2
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.logging.level == "DEBUG"
    assert config.logging.file == Path("agent.log")
    assert config.logging.log_prompts is False
    assert config.logging.log_model_messages is True
    assert config.logging.log_tool_results is True
    assert config.logging.max_bytes == 1234
    assert config.logging.backup_count == 2
    assert config.mcp_servers == ()


def test_configure_logging_writes_file(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    log_file = tmp_path / "agent.log"
    config_file.write_text(
        f"""
[logging]
file = "{log_file.as_posix()}"
""".strip(),
        encoding="utf-8",
    )
    config = load_config(config_file)

    configure_logging(config.logging)
    logging.getLogger("cli_agent.test").info("tool_call name=compose_ps")

    assert "tool_call name=compose_ps" in log_file.read_text(encoding="utf-8")


def test_load_mcp_servers(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[[mcp_servers]]
name = "special"
transport = "stdio"
command = "C:/tools/python.exe"
args = ["-m", "special.server"]

[mcp_servers.env]
API_URL = "http://localhost:8080"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert len(config.mcp_servers) == 1
    assert config.mcp_servers[0].name == "special"
    assert config.mcp_servers[0].args == ("-m", "special.server")
    assert config.mcp_servers[0].env["API_URL"] == "http://localhost:8080"


def test_load_streamable_http_server(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[[mcp_servers]]
name = "external"
transport = "streamable_http"
url = "http://127.0.0.1:8001/mcp"

[mcp_servers.headers]
Authorization = "Bearer test"
""".strip(),
        encoding="utf-8",
    )

    server = load_config(config_file).mcp_servers[0]

    assert server.transport == "streamable_http"
    assert server.url == "http://127.0.0.1:8001/mcp"
    assert server.command is None
    assert server.headers == {"Authorization": "Bearer test"}


def test_http_server_rejects_process_options(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[[mcp_servers]]
name = "invalid"
transport = "streamable_http"
url = "http://127.0.0.1:8001/mcp"
command = "python"
""".strip(),
        encoding="utf-8",
    )

    import pytest

    with pytest.raises(ValueError, match="darf command, args und env"):
        load_config(config_file)
