from __future__ import annotations

import logging
from pathlib import Path

import pytest

import cli_agent.config as config_module
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
    assert config.logging.log_tool_calls is False
    assert config.logging.log_model_messages is True
    assert config.logging.log_tool_results is True
    assert config.logging.max_bytes == 1234
    assert config.logging.backup_count == 2
    assert config.mcp_servers == ()


def test_load_model_context_length(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[model]
provider = "openai"
model = "test-model"
base_url = "http://localhost:8000/v1"
context_length = 262144
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.model.context_length == 262_144


@pytest.mark.parametrize("value", [0, -1, '"invalid"', "true"])
def test_model_context_length_must_be_positive_integer(
    tmp_path: Path,
    value: str,
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f"""
[model]
context_length = {value}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="context_length"):
        load_config(config_file)


def test_load_network_allowlists(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[network]
model_allowed_hosts = ["llm.internal"]
mcp_allowed_hosts = ["mcp.internal"]
web_allowed_hosts = ["docs.internal"]
""".strip(),
        encoding="utf-8",
    )

    network = load_config(config_file).network

    assert network.model_allowed_hosts == ("llm.internal",)
    assert network.mcp_allowed_hosts == ("mcp.internal",)
    assert network.web_allowed_hosts == ("docs.internal",)


def test_load_config_rejects_string_security_flags(tmp_path: Path) -> None:
    logging_config_file = tmp_path / "logging-config.toml"
    logging_config_file.write_text(
        """
[logging]
log_prompts = "false"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="log_prompts"):
        load_config(logging_config_file)

    mcp_config_file = tmp_path / "mcp-config.toml"
    mcp_config_file.write_text(
        """
[[mcp_servers]]
name = "os"
transport = "stdio"
command = "python"

[mcp_servers.config]
allow_write_files = "false"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="allow_write_files"):
        load_config(mcp_config_file)


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

    with pytest.raises(ValueError, match="darf command, args und env"):
        load_config(config_file)


def test_default_config_is_created_from_safe_packaged_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "state" / "config.toml"
    monkeypatch.setattr(config_module, "default_config_file", lambda: config_file)

    config = load_config()

    assert config_file.is_file()
    assert config.mcp_servers == ()
    assert config.okf is None
    content = config_file.read_text(encoding="utf-8")
    assert "# [[mcp_servers]]" in content
    assert "# [okf]" in content


def test_existing_default_config_is_not_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_file = tmp_path / "config.toml"
    original = """
[model]
provider = "ollama"
model = "custom-model"
base_url = "http://localhost:11434"
""".strip()
    config_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr(config_module, "default_config_file", lambda: config_file)

    config = load_config()

    assert config.model.model == "custom-model"
    assert config_file.read_text(encoding="utf-8") == original


def test_missing_explicit_config_is_not_created(tmp_path: Path) -> None:
    config_file = tmp_path / "custom" / "config.toml"

    config = load_config(config_file)

    assert config.mcp_servers == ()
    assert not config_file.exists()
