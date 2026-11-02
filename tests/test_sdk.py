"""SDK contract and task-bound lifecycle tests."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.sdk import AgentBuilder
from cli_agent.settings.admin_config import AdminConfig
from cli_agent.security.network_policy import LOCAL_HOSTS


def test_sdk_defaults_ignore_system_admin_config(monkeypatch):
    def unexpected():
        raise AssertionError("SDK must not load system admin policy")
    monkeypatch.setattr("cli_agent.sdk.builder.load_admin_config", unexpected)
    agent = AgentBuilder().with_project_config(model="test").build()
    assert agent._policy == AdminConfig()
    assert agent._config.model.model == "test"


def test_sdk_python_admin_policy_does_not_read_system(monkeypatch):
    def unexpected():
        raise AssertionError("SDK must not read system admin policy")
    monkeypatch.setattr("cli_agent.sdk.builder.load_admin_config", unexpected)
    agent = (AgentBuilder()
             .with_admin_config(model_allowed_hosts=["localhost", "api.example.com"])
             .with_project_config(model="gpt")
             .build())
    assert agent._policy.network.model_allowed_hosts == ("localhost", "api.example.com")
    assert agent._policy.network.mcp_allowed_hosts == LOCAL_HOSTS


def test_sdk_explicit_global_admin_policy(monkeypatch):
    monkeypatch.setattr("cli_agent.sdk.builder.load_admin_config",
                        lambda: AdminConfig())
    agent = AgentBuilder().with_admin_config().build()
    assert agent._policy == AdminConfig()


def test_sdk_explicit_admin_path_requires_existing_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        AgentBuilder().with_admin_config(path=tmp_path / "missing.toml")


def test_sdk_rejects_unknown_or_malformed_policy_options():
    with pytest.raises(TypeError):
        AgentBuilder().with_admin_config(not_an_option=True)
    with pytest.raises(ValueError):
        AgentBuilder().with_admin_config(mcp_allowed_hosts=["https://bad.example"])
    with pytest.raises(ValueError):
        AgentBuilder().with_admin_config(mcp_allowed_hosts=["example.com", "example.com"])


def test_builder_agents_are_independent():
    builder = AgentBuilder().with_project_config(model="a")
    first = builder.build()
    second = builder.with_project_config(model="b").build()
    assert first._config.model.model == "a"
    assert second._config.model.model == "b"


def test_sdk_rejects_invalid_prompts():
    async def scenario():
        agent = AgentBuilder().build()
        with pytest.raises(RuntimeError):
            await agent.ask("hello")
        with pytest.raises(ValueError):
            await agent.ask("  ")
    asyncio.run(scenario())


def test_sdk_persistent_worker_preserves_agent_state(monkeypatch, tmp_path):
    created = []
    class FakeRuntime:
        def __init__(self):
            self.prompts = []
        async def __aenter__(self):
            created.append(self)
            return self
        async def __aexit__(self, *_):
            pass
        async def ask(self, prompt):
            self.prompts.append(prompt)
            return str(len(self.prompts))

    def fake_prepare(opts, **kwargs):
        assert opts.workspace == tmp_path
        assert kwargs["admin_config"] == AdminConfig()
        return SimpleNamespace(agent=FakeRuntime())

    monkeypatch.setattr("cli_agent.sdk.agent.prepare_agent", fake_prepare)

    async def scenario():
        async with AgentBuilder().with_project_config(workspace=tmp_path).build() as agent:
            assert (await agent.ask("first")).answer == "1"
            assert (await agent.ask("second")).answer == "2"
        with pytest.raises(RuntimeError):
            await agent.ask("third")

    asyncio.run(scenario())
    assert len(created) == 1
    assert created[0].prompts == ["first", "second"]
