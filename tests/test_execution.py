from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from cli_agent.admin_config import AdminConfig
from cli_agent.config import AppConfig, ModelConfig
from cli_agent.execution import (
    ExecutionDependencies,
    OneShotRunOptions,
    apply_workspace_access_override,
    run_once,
)


def _config() -> AppConfig:
    return AppConfig(
        model=ModelConfig(
            provider="openai",
            model="test-model",
            base_url="http://localhost:11434/v1",
        )
    )


def test_workspace_access_read_and_write_are_explicit() -> None:
    read = apply_workspace_access_override(
        _config(),
        workspace_access="read",
    )
    write = apply_workspace_access_override(
        _config(),
        workspace_access="write",
    )

    assert read.mcp_servers[0].name == "os"
    assert read.mcp_servers[0].allow_write_files() is False
    assert write.mcp_servers[0].name == "os"
    assert write.mcp_servers[0].allow_write_files() is True


def test_workspace_access_none_removes_os_server() -> None:
    writable = apply_workspace_access_override(
        _config(),
        workspace_access="write",
    )

    disabled = apply_workspace_access_override(
        writable,
        workspace_access="none",
    )

    assert disabled.mcp_servers == ()


def test_run_once_uses_core_modules_without_cli_process(
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}

    class FakeAgent:
        instances = 0

        def __init__(self, workspace, model_client, servers, **kwargs):
            type(self).instances += 1
            captured["workspace"] = workspace
            captured["servers"] = servers
            captured["kwargs"] = kwargs
            captured["prompts"] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def ask(self, prompt):
            captured["prompts"].append(prompt)
            if prompt == "tokens":
                return "usage"
            return "answer"

    dependencies = ExecutionDependencies(
        load_config=lambda _path: _config(),
        load_admin_config=lambda: AdminConfig(),
        configure_logging=lambda _config: None,
        create_model_client=lambda *_args, **_kwargs: SimpleNamespace(
            model="test-model",
            base_url="http://localhost:11434/v1",
        ),
        agent_type=FakeAgent,
    )

    result = asyncio.run(
        run_once(
            OneShotRunOptions(
                workspace=tmp_path,
                prompt="do work",
                workspace_access="read",
            ),
            dependencies=dependencies,
        )
    )

    assert result.answer == "answer"
    assert result.usage == "usage"
    assert captured["workspace"] == tmp_path.resolve()
    assert captured["servers"][0].allow_write_files() is False
    assert captured["prompts"] == ["do work", "tokens"]
    assert FakeAgent.instances == 1


def test_each_run_once_creates_fresh_agent_instance(
    tmp_path: Path,
) -> None:
    instances = []

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def ask(self, prompt):
            return "usage" if prompt == "tokens" else "answer"

    dependencies = ExecutionDependencies(
        load_config=lambda _path: _config(),
        load_admin_config=lambda: AdminConfig(),
        configure_logging=lambda _config: None,
        create_model_client=lambda *_args, **_kwargs: object(),
        agent_type=FakeAgent,
    )

    async def execute() -> None:
        await run_once(
            OneShotRunOptions(
                workspace=tmp_path,
                prompt="one",
            ),
            dependencies=dependencies,
        )
        await run_once(
            OneShotRunOptions(
                workspace=tmp_path,
                prompt="two",
            ),
            dependencies=dependencies,
        )

    asyncio.run(execute())

    assert len(instances) == 2
    assert instances[0] is not instances[1]
