"""Workspace project override, private prompt, and MCP-path regression tests."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from cli_agent.settings.config import load_config
from cli_agent.settings.project_config import (
    merge_project_overrides,
    read_project_overrides,
    read_project_system_prompt,
)
from cli_agent.platform.os_operations import AllowedPath, Workspace, WorkspaceError
from cli_agent.settings.config import McpServerConfig


def _project(tmp_path: Path, text: str) -> Path:
    folder = tmp_path / ".cli-agent"
    folder.mkdir(exist_ok=True)
    (folder / "project.toml").write_text(text, encoding="utf-8")
    return folder


def test_project_config_overrides_only_explicit_nested_fields(tmp_path: Path):
    global_config = tmp_path / "global.toml"
    global_config.write_text(
        '[agent]\ntimezone = "Europe/Berlin"\n'
        '[model]\nprovider = "openai"\nmodel = "base"\n'
        'base_url = "http://localhost:11434/v1"\ntimeout = 75\n'
        '[pdf]\nmax_pdf_pages = 200\n',
        encoding="utf-8",
    )
    folder = _project(
        tmp_path, '[model]\nmodel = "project-model"\n'
        '[agent]\nsystem_prompt = "system.md"\n',
    )
    (folder / "system.md").write_text("You are a time-series analyst.", encoding="utf-8")

    base = load_config(global_config)
    effective = load_config(global_config, workspace=tmp_path)
    assert base.model.model == "base"
    assert effective.model.model == "project-model"
    assert effective.model.provider == "openai"
    assert effective.model.timeout == 75
    assert effective.model.base_url == "http://localhost:11434/v1"
    assert effective.agent.timezone == "Europe/Berlin"
    assert effective.agent.system_prompt == "system.md"
    assert effective.max_pdf_pages == 200
    assert read_project_system_prompt(tmp_path, effective.agent.system_prompt) == (
        "You are a time-series analyst."
    )


def test_absent_project_config_preserves_global_config(tmp_path: Path):
    global_config = tmp_path / "config.toml"
    global_config.write_text("[model]\nmodel = 'unchanged'\n", encoding="utf-8")
    assert read_project_overrides(tmp_path) == {}
    assert load_config(global_config, workspace=tmp_path) == load_config(global_config)
    assert read_project_system_prompt(tmp_path, None) is None


def test_nested_tables_are_merged_but_arrays_are_replaced():
    base = {"model": {"provider": "openai", "model": "base"},
            "mcp_servers": [{"name": "old"}]}
    overlay = {"model": {"model": "new"}, "mcp_servers": [{"name": "new"}]}
    assert merge_project_overrides(base, overlay) == {
        "model": {"provider": "openai", "model": "new"},
        "mcp_servers": [{"name": "new"}],
    }
    assert base["model"]["model"] == "base"


@pytest.mark.parametrize("value", [
    "../system.md", "/tmp/system.md", "folder/system.md",
    r"folder\\system.md", "C:system.md", "", ".", "..", " system.md ",
])
def test_system_prompt_rejects_unsafe_filenames(tmp_path: Path, value: str):
    with pytest.raises(ValueError, match="filename directly"):
        read_project_system_prompt(tmp_path, value)


def test_system_prompt_missing_empty_or_oversized_fails_closed(tmp_path: Path):
    folder = _project(tmp_path, '[agent]\nsystem_prompt = "system.md"\n')
    with pytest.raises(FileNotFoundError):
        read_project_system_prompt(tmp_path, "system.md")
    (folder / "system.md").write_text("  ", encoding="utf-8")
    with pytest.raises(ValueError, match="must not be empty"):
        read_project_system_prompt(tmp_path, "system.md")
    (folder / "system.md").write_bytes(b"a" * (256 * 1024 + 1))
    with pytest.raises(ValueError, match="exceeds"):
        read_project_system_prompt(tmp_path, "system.md")


def test_project_toml_invalid_utf8_or_unexpected_agent_field_fails(tmp_path: Path):
    folder = _project(tmp_path, '[agent]\nunknown = true\n')
    global_config = tmp_path / "global.toml"
    global_config.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown options"):
        load_config(global_config, workspace=tmp_path)
    (folder / "project.toml").write_bytes(bytes([255]))
    with pytest.raises(ValueError, match="UTF-8"):
        load_config(global_config, workspace=tmp_path)


def test_symlinked_private_prompt_is_rejected(tmp_path: Path):
    folder = _project(tmp_path, '[agent]\nsystem_prompt = "system.md"\n')
    target = tmp_path / "other.md"
    target.write_text("other", encoding="utf-8")
    link = folder / "system.md"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation not available")
    with pytest.raises(ValueError, match="symlink"):
        read_project_system_prompt(tmp_path, "system.md")


def test_private_root_files_remain_hidden_with_allowed_data_checkpoints(tmp_path: Path):
    folder = _project(tmp_path, '[agent]\nsystem_prompt = "system.md"\n')
    (folder / "system.md").write_text("private prompt", encoding="utf-8")
    data = folder / "mcp-data-123"
    data.mkdir()
    (data / "checkpoint.json").write_text('{"value": 7}', encoding="utf-8")
    workspace = Workspace.from_directory(
        tmp_path, McpServerConfig(name="data", config={"allow_write_files": True}),
        allowed_paths=(AllowedPath(data, "write"),),
    )
    assert '"value": 7' in workspace.read_file(".cli-agent/mcp-data-123/checkpoint.json")
    workspace.write_file(".cli-agent/mcp-data-123/checkpoint.json", '{"value": 8}')
    assert '"value": 8' in workspace.read_file(".cli-agent/mcp-data-123/checkpoint.json")
    with pytest.raises(WorkspaceError):
        workspace.read_file(".cli-agent/project.toml")
    with pytest.raises(WorkspaceError):
        workspace.read_file(".cli-agent/system.md")


def test_direct_private_files_cannot_be_exposed_even_when_parent_allowed(tmp_path: Path):
    folder = _project(tmp_path, '[agent]\nsystem_prompt = "system.md"\n')
    (folder / "system.md").write_text("secret", encoding="utf-8")
    data = folder / "mcp-data-123"
    data.mkdir()
    (data / "rows.json").write_text('{"x": 1}', encoding="utf-8")
    workspace = Workspace.from_directory(
        tmp_path, McpServerConfig(name="os", config={"allow_write_files": True}),
        allowed_paths=(AllowedPath(folder, "write"), AllowedPath(data, "write")),
    )
    with pytest.raises(WorkspaceError):
        workspace.read_file(".cli-agent/system.md")
    with pytest.raises(WorkspaceError):
        workspace.read_file(".cli-agent/project.toml")
    assert '"x": 1' in workspace.read_file(".cli-agent/mcp-data-123/rows.json")

def test_one_shot_preparation_receives_merged_config_and_prompt(tmp_path: Path):
    from cli_agent.platform.execution import (
        ExecutionDependencies, OneShotRunOptions, prepare_agent,
    )
    from cli_agent.settings.admin_config import AdminConfig

    global_config = tmp_path / "global.toml"
    global_config.write_text(
        '[model]\nprovider = "openai"\nmodel = "base"\n'
        'base_url = "http://localhost:11434/v1"\n', encoding="utf-8",
    )
    folder = _project(
        tmp_path,
        '[model]\nmodel = "project-model"\n'
        '[agent]\nsystem_prompt = "system.md"\n',
    )
    (folder / "system.md").write_text("You are a project analyst.", encoding="utf-8")
    captured = {}

    class Agent:
        def __init__(self, workspace, model_client, servers, **kwargs):
            captured.update(kwargs)

    deps = ExecutionDependencies(
        load_config=lambda path: load_config(path),
        load_admin_config=AdminConfig,
        configure_logging=lambda config: None,
        create_model_client=lambda *args, **kwargs: object(),
        agent_type=Agent,
    )
    prepared = prepare_agent(
        OneShotRunOptions(workspace=tmp_path, prompt="work", config_file=global_config, model="cli-model"),
        dependencies=deps,
    )
    assert prepared.config.model.model == "cli-model"  # CLI > project > global.
    assert load_config(global_config, workspace=tmp_path).model.model == "project-model"
    assert prepared.config.model.provider == "openai"
    assert captured["system_prompt"] == "You are a project analyst."


def test_main_and_sub_run_inherit_project_prompt_without_removing_internal_rules(
    tmp_path: Path, monkeypatch,
):
    import asyncio
    import json
    from cli_agent.runtime.agent import CliAgent
    from cli_agent.models.ollama import OllamaClient
    from cli_agent.state.sub_runs import execute_sub_run
    import cli_agent.runtime.agent_loop as agent_loop

    agent = CliAgent(
        tmp_path, OllamaClient(base_url="http://localhost:11434", model="test"),
        (), system_prompt="You are a project analyst.",
    )
    prompt = agent._build_system_prompt()
    assert "You are a project analyst." in prompt
    assert "You are a local CLI assistant." not in prompt
    assert "Current local time:" in prompt
    seen = []

    async def fake_run_model_loop(agent, *, messages, **kwargs):
        seen.append(messages[0]["content"])
        return json.dumps({
            "status": "partial", "summary": "No verified findings.",
            "evidence_ids": [], "confidence": "low", "uncertainties": [],
        })

    monkeypatch.setattr(agent_loop, "run_model_loop", fake_run_model_loop)
    asyncio.run(execute_sub_run(agent, {
        "task": "Inspect only one small time-series dataset and report its coverage.",
    }))
    assert len(seen) == 1
    assert "You are a project analyst." in seen[0]
    assert "You are a local CLI assistant." not in seen[0]
    assert "Sub-runs have NO local plan." in seen[0]


def test_os_mcp_subprocess_reads_effective_project_pdf_limit(tmp_path: Path, monkeypatch):
    """The spawned OS MCP process must not fall back to global PDF settings."""
    from types import SimpleNamespace
    from cli_agent.platform import os_mcp_server

    global_config = tmp_path / "global.toml"
    global_config.write_text("[pdf]\nmax_pdf_pages = 100\n", encoding="utf-8")
    _project(tmp_path, "[pdf]\nmax_pdf_pages = 200\n")
    captured = {}

    monkeypatch.setattr(os_mcp_server, "parse_args", lambda: SimpleNamespace(
        project_directory=tmp_path, config_file=global_config, access="read",
        protected_path=[], mutation_protected_path=[], path_policy_file=None,
        max_pdf_pages=None, sdk_configless=False,
    ))
    monkeypatch.setattr(os_mcp_server, "configure_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(os_mcp_server, "vision_config_from_environment", lambda: None)
    monkeypatch.setattr(
        os_mcp_server.Workspace, "from_directory",
        lambda directory, mcp_config, **_kwargs:
            captured.setdefault("mcp_config", mcp_config) or object(),
    )
    monkeypatch.setattr(
        os_mcp_server, "create_server",
        lambda *_a, **_k: SimpleNamespace(run=lambda **_options: None),
    )
    os_mcp_server.main()
    assert captured["mcp_config"].config["max_pdf_pages"] == 200


def test_data_mcp_subprocess_reads_effective_project_logging(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace
    from cli_agent.data import data_mcp_server

    global_config = tmp_path / "global.toml"
    global_config.write_text("[logging]\nenabled = true\n", encoding="utf-8")
    _project(tmp_path, "[logging]\nenabled = false\n")
    observed = []
    monkeypatch.setattr(data_mcp_server, "parse_args", lambda: SimpleNamespace(
        project_directory=tmp_path, config_file=global_config, access="none",
        protected_path=[], mutation_protected_path=[], path_policy_file=None,
        sdk_configless=False,
    ))
    monkeypatch.setattr(
        data_mcp_server, "configure_logging",
        lambda logging, **_kwargs: observed.append(logging.enabled),
    )
    monkeypatch.setattr(data_mcp_server, "DataOperations", lambda workspace: object())
    monkeypatch.setattr(
        data_mcp_server, "create_server",
        lambda *_a, **_k: SimpleNamespace(run=lambda **_options: None),
    )
    data_mcp_server.main()
    assert observed == [False]


def test_sdk_configless_os_subprocess_ignores_project_overlay(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace
    from cli_agent.platform import os_mcp_server

    _project(tmp_path, "[pdf]\nmax_pdf_pages = 200\n")
    captured = {}
    monkeypatch.setattr(os_mcp_server, "parse_args", lambda: SimpleNamespace(
        project_directory=tmp_path, config_file=None, access="read",
        protected_path=[], mutation_protected_path=[], path_policy_file=None,
        max_pdf_pages=350, sdk_configless=True,
    ))
    monkeypatch.setattr(os_mcp_server, "configure_logging", lambda *_a, **_k: None)
    monkeypatch.setattr(os_mcp_server, "vision_config_from_environment", lambda: None)
    monkeypatch.setattr(
        os_mcp_server.Workspace, "from_directory",
        lambda _directory, mcp_config, **_kwargs:
            captured.setdefault("mcp_config", mcp_config) or object(),
    )
    monkeypatch.setattr(
        os_mcp_server, "create_server",
        lambda *_a, **_k: SimpleNamespace(run=lambda **_options: None),
    )
    os_mcp_server.main()
    assert captured["mcp_config"].config["max_pdf_pages"] == 350
