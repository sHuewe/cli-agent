from __future__ import annotations

import logging
from pathlib import Path

import pytest

import cli_agent.config as config_module
from cli_agent.config import load_config
from cli_agent.logging_setup import configure_logging


def test_load_logging_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setattr(config_module, "application_directory", lambda: state_dir)
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[logging]
enabled = true
level = "DEBUG"
file = "logs/agent.log"
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
    assert config.logging.file == (state_dir / "logs" / "agent.log").resolve()
    assert config.logging.log_prompts is False
    assert config.logging.log_tool_calls is False
    assert config.logging.log_model_messages is True
    assert config.logging.log_tool_results is True
    assert config.logging.max_bytes == 1234
    assert config.logging.backup_count == 2
    assert config.mcp_servers == ()


def test_logging_file_rejects_absolute_paths(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[logging]\nfile = "{(tmp_path / "outside.log").as_posix()}"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="relativer Pfad"):
        load_config(config_file)


def test_logging_file_rejects_parent_traversal(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text('[logging]\nfile = "../outside.log"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="\.\."):
        load_config(config_file)


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
    assert load_config(config_file).model.context_length == 262_144


@pytest.mark.parametrize("value", [0, -1, '"invalid"', "true"])
def test_model_context_length_must_be_positive_integer(tmp_path: Path, value: str) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(f"[model]\ncontext_length = {value}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="context_length"):
        load_config(config_file)


def test_load_config_rejects_string_security_flags(tmp_path: Path) -> None:
    logging_config_file = tmp_path / "logging-config.toml"
    logging_config_file.write_text('[logging]\nlog_prompts = "false"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="log_prompts"):
        load_config(logging_config_file)


def test_configure_logging_writes_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_dir = tmp_path / "state"
    monkeypatch.setattr(config_module, "application_directory", lambda: state_dir)
    config_file = tmp_path / "config.toml"
    config_file.write_text('[logging]\nfile = "agent.log"\n', encoding="utf-8")
    configure_logging(load_config(config_file).logging)
    logging.getLogger("cli_agent.test").info("tool_call name=example")
    log_file = state_dir / "agent.log"
    assert "tool_call name=example" in log_file.read_text(encoding="utf-8")


def test_load_stdio_server_reference_is_name_only(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[[mcp_servers]]
name = "special"
""".strip(),
        encoding="utf-8",
    )
    server = load_config(config_file).mcp_servers[0]
    assert server.name == "special"
    assert server.transport == "stdio"
    assert server.command is None
    assert server.args == ()
    assert server.env == {}


@pytest.mark.parametrize(
    "extra",
    [
        'transport = "stdio"',
        'command = "python"',
        'args = ["-m", "special.server"]',
        'compress_result = true',
    ],
)
def test_stdio_user_config_rejects_launch_or_runtime_settings(tmp_path: Path, extra: str) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        f'[[mcp_servers]]\nname = "special"\n{extra}\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="nur.*Namen|nur.*referenziert|Admin-Policy"):
        load_config(config_file)


def test_load_streamable_http_server_without_authentication(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[[mcp_servers]]
name = "external"
transport = "streamable_http"
url = "http://127.0.0.1:8001/mcp"

[mcp_servers.headers]
X-Client-Id = "cli-agent"
""".strip(),
        encoding="utf-8",
    )
    server = load_config(config_file).mcp_servers[0]
    assert server.transport == "streamable_http"
    assert server.url == "http://127.0.0.1:8001/mcp"
    assert server.command is None
    assert server.headers == {"X-Client-Id": "cli-agent"}


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


def test_duplicate_mcp_server_names_are_rejected(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        """
[[mcp_servers]]
name = "same"

[[mcp_servers]]
name = "same"
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="eindeutig"):
        load_config(config_file)


def test_default_config_is_created_from_safe_packaged_template(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_file = tmp_path / "state" / "config.toml"
    monkeypatch.setattr(config_module, "default_config_file", lambda: config_file)
    config = load_config()
    assert config_file.is_file()
    assert config.mcp_servers == ()
    assert config.okf is None
    content = config_file.read_text(encoding="utf-8")
    active_lines = {
        line.strip()
        for line in content.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    assert "[network]" not in active_lines
    assert not any(line.startswith("allow_untrusted_stdio") for line in active_lines)
    assert "# [[mcp_servers]]" in content
    assert "# [okf]" in content


def test_existing_default_config_is_not_overwritten(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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
