from __future__ import annotations

import logging
from pathlib import Path

import pytest

import cli_agent.config as config_module
from cli_agent.config import load_config
from cli_agent.logging_setup import (
    _FileLock,
    _cleanup_process_log_files,
    configure_logging,
    process_log_file,
)


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
    log_file = process_log_file(state_dir / "agent.log")
    assert "tool_call name=example" in log_file.read_text(encoding="utf-8")


def test_process_log_file_includes_pid(tmp_path: Path) -> None:
    assert process_log_file(tmp_path / "agent.log", pid=12345) == tmp_path / "agent-12345.log"
    assert process_log_file(tmp_path / "agent", pid=12345) == tmp_path / "agent-12345"


def test_cleanup_process_log_files_bounds_inactive_families(tmp_path: Path) -> None:
    base_file = tmp_path / "agent.log"
    old_log = process_log_file(base_file, pid=100)
    middle_log = process_log_file(base_file, pid=200)
    newest_log = process_log_file(base_file, pid=300)
    for index, path in enumerate((old_log, middle_log, newest_log), start=1):
        path.write_text(str(index), encoding="utf-8")
        path.touch()
        path.stat()
    old_log.touch()
    middle_log.touch()
    newest_log.touch()

    # Establish deterministic modification ordering without sleeping.
    import os

    os.utime(old_log, (100, 100))
    os.utime(middle_log, (200, 200))
    os.utime(newest_log, (300, 300))

    _cleanup_process_log_files(base_file, current_pid=999, keep_inactive=2)

    assert not old_log.exists()
    assert middle_log.exists()
    assert newest_log.exists()


def test_cleanup_process_log_files_preserves_active_family(tmp_path: Path) -> None:
    base_file = tmp_path / "agent.log"
    active_log = process_log_file(base_file, pid=123)
    inactive_log = process_log_file(base_file, pid=456)
    active_log.write_text("active", encoding="utf-8")
    inactive_log.write_text("inactive", encoding="utf-8")

    active_lock = _FileLock(active_log.with_name(f"{active_log.name}.lock"))
    assert active_lock.acquire()
    try:
        _cleanup_process_log_files(base_file, current_pid=999, keep_inactive=0)
        assert active_log.exists()
        assert not inactive_log.exists()
    finally:
        active_lock.release()


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


def test_missing_explicit_config_fails_closed_and_is_not_created(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "custom" / "config.toml"

    with pytest.raises(FileNotFoundError, match="Explizit angegebene"):
        load_config(config_file)

    assert not config_file.exists()
