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


def test_sdk_credential_dataclass_tuple_is_accepted():
    from cli_agent.settings.admin_config import ModelCredentialRule
    policy = (
        AgentBuilder()
        .with_admin_config(
            model_allowed_hosts=["localhost", "api.example.com"],
            model_credentials=[
                ModelCredentialRule(
                    provider="openai",
                    host="api.example.com",
                    allowed_api_key_envs=("OPENAI_API_KEY",),
                )
            ],
        )
        .build()
        ._policy
    )
    assert policy.model_credentials[0].allowed_api_key_envs == ("OPENAI_API_KEY",)


def test_sdk_builtin_mcp_uses_configless_mode_and_effective_pdf_limit(tmp_path):
    from cli_agent.platform.execution import (
        ExecutionDependencies, OneShotRunOptions, prepare_agent,
    )
    from cli_agent.settings.config import AppConfig
    captured = {}

    class FakeAgent:
        def __init__(self, _workspace, _model_client, servers, **kwargs):
            captured["servers"] = servers

    deps = ExecutionDependencies(
        load_config=lambda _path: (_ for _ in ()).throw(
            AssertionError("No project file should be loaded")
        ),
        load_admin_config=lambda: (_ for _ in ()).throw(
            AssertionError("No system policy should be loaded")
        ),
        configure_logging=lambda _config: None,
        create_model_client=lambda *_args, **_kwargs: object(),
        agent_type=FakeAgent,
    )
    prepared = prepare_agent(
        OneShotRunOptions(
            workspace=tmp_path,
            prompt="test",
            workspace_access="read",
            with_data=True,
        ),
        dependencies=deps,
        project_config=__import__("dataclasses").replace(
            AppConfig(), max_pdf_pages=217,
        ),
        admin_config=AdminConfig(),
    )
    servers = {s.name: s for s in prepared.config.mcp_servers}
    assert set(servers) == {"os", "data"}
    for server in servers.values():
        assert "--sdk-configless" in server.args
        assert "--config-file" not in server.args
    os_args = servers["os"].args
    assert os_args[os_args.index("--max-pdf-pages") + 1] == "217"


def test_sdk_okf_flag_on_runtime_and_effective_config(tmp_path):
    from cli_agent.platform.execution import ExecutionDependencies, OneShotRunOptions, prepare_agent
    from cli_agent.settings.config import AppConfig, OkfConfig
    from dataclasses import replace
    captured = {}
    class FakeAgent:
        def __init__(self, _workspace, _model_client, _servers, **kwargs):
            captured.update(kwargs)

    deps = ExecutionDependencies(
        load_config=lambda _: (_ for _ in ()).throw(AssertionError("unexpected file load")),
        load_admin_config=lambda: (_ for _ in ()).throw(AssertionError("unexpected admin load")),
        configure_logging=lambda *_: None,
        create_model_client=lambda *_args, **_kwargs: object(),
        agent_type=FakeAgent,
    )
    repo = tmp_path / "knowledge"
    repo.mkdir()
    config = replace(AppConfig(), okf=OkfConfig(repository=str(repo)))
    prepare_agent(
        OneShotRunOptions(workspace=tmp_path, prompt="test"),
        dependencies=deps, project_config=config, admin_config=AdminConfig(),
    )
    assert captured["sdk_configless"] is True
    assert captured["okf"] == config.okf


def test_sdk_project_file_remains_protected_in_both_builtin_servers(tmp_path):
    from cli_agent.platform.execution import ExecutionDependencies, OneShotRunOptions, prepare_agent
    from cli_agent.settings.config import AppConfig
    captured = {}
    class FakeAgent:
        def __init__(self, _workspace, _model_client, servers, **kwargs):
            captured["servers"] = servers

    deps = ExecutionDependencies(
        load_config=lambda _: (_ for _ in ()).throw(AssertionError("unexpected file load")),
        load_admin_config=lambda: (_ for _ in ()).throw(AssertionError("unexpected admin load")),
        configure_logging=lambda *_: None,
        create_model_client=lambda *_args, **_kwargs: object(),
        agent_type=FakeAgent,
    )
    path = tmp_path / "project.toml"
    path.write_text("[model]\\nmodel = 'demo'\\n")
    prepare_agent(
        OneShotRunOptions(
            workspace=tmp_path, prompt="test", config_file=path,
            workspace_access="write", with_data=True,
        ),
        dependencies=deps, project_config=AppConfig(), admin_config=AdminConfig(),
    )
    servers = {s.name: s for s in captured["servers"]}
    for name in ("os", "data"):
        server = servers[name]
        assert "--config-file" not in server.args
        assert "--sdk-configless" in server.args
        args = list(server.literal_args)
        assert args[args.index("--protected-path") + 1] == str(path.resolve())


def test_sdk_cancel_active_ask_stops_runtime_and_keeps_session(monkeypatch, tmp_path):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    calls = []
    class Runtime:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def ask(self, prompt):
            calls.append(prompt)
            if prompt == "slow":
                started.set()
                try:
                    await asyncio.sleep(30)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return "ok"
    monkeypatch.setattr("cli_agent.sdk.agent.prepare_agent",
                        lambda *_args, **_kwargs: SimpleNamespace(agent=Runtime()))

    async def scenario():
        async with AgentBuilder().with_project_config(workspace=tmp_path).build() as agent:
            task = asyncio.create_task(agent.ask("slow"))
            await started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await asyncio.wait_for(cancelled.wait(), timeout=2)
            result = await asyncio.wait_for(agent.ask("next"), timeout=2)
            assert result.answer == "ok"
    asyncio.run(scenario())
    assert calls == ["slow", "next"]


def test_sdk_cancelling_queued_request_does_not_interrupt_active(monkeypatch, tmp_path):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []
    class Runtime:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def ask(self, prompt):
            calls.append(prompt)
            if prompt == "first":
                entered.set()
                await release.wait()
            return "ok"
    monkeypatch.setattr("cli_agent.sdk.agent.prepare_agent",
                        lambda *_args, **_kwargs: SimpleNamespace(agent=Runtime()))
    async def scenario():
        async with AgentBuilder().with_project_config(workspace=tmp_path).build() as agent:
            first = asyncio.create_task(agent.ask("first"))
            await entered.wait()
            queued = asyncio.create_task(agent.ask("second"))
            await asyncio.sleep(0)
            queued.cancel()
            with pytest.raises(asyncio.CancelledError):
                await queued
            release.set()
            assert (await asyncio.wait_for(first, 2)).answer == "ok"
            assert (await asyncio.wait_for(agent.ask("third"), 2)).answer == "ok"
    asyncio.run(scenario())
    assert calls == ["first", "third"]


def test_sdk_close_interrupts_active_request(monkeypatch, tmp_path):
    started = asyncio.Event()
    cancelled = asyncio.Event()
    class Runtime:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def ask(self, _prompt):
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
    monkeypatch.setattr("cli_agent.sdk.agent.prepare_agent",
                        lambda *_args, **_kwargs: SimpleNamespace(agent=Runtime()))
    async def scenario():
        agent = AgentBuilder().with_project_config(workspace=tmp_path).build()
        await agent.start()
        task = asyncio.create_task(agent.ask("slow"))
        await started.wait()
        await asyncio.wait_for(agent.close(), 2)
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
    asyncio.run(scenario())


def test_sdk_start_cancellation_interrupts_initializing_worker(monkeypatch, tmp_path):
    entered = asyncio.Event()
    interrupted = asyncio.Event()
    exited = asyncio.Event()

    class Runtime:
        async def __aenter__(self):
            entered.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                interrupted.set()
                raise
        async def __aexit__(self, *_):
            exited.set()

    monkeypatch.setattr(
        "cli_agent.sdk.agent.prepare_agent",
        lambda *_args, **_kwargs: SimpleNamespace(agent=Runtime()),
    )

    async def scenario():
        agent = AgentBuilder().with_project_config(workspace=tmp_path).build()
        startup = asyncio.create_task(agent.start())
        await asyncio.wait_for(entered.wait(), 2)
        startup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(startup, 2)
        assert interrupted.is_set()
        assert agent._worker_task is None
        with pytest.raises(RuntimeError):
            await agent.start()

    asyncio.run(scenario())


def test_sdk_close_during_startup_interrupts_worker(monkeypatch, tmp_path):
    entered = asyncio.Event()
    interrupted = asyncio.Event()

    class Runtime:
        async def __aenter__(self):
            entered.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                interrupted.set()
                raise
        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(
        "cli_agent.sdk.agent.prepare_agent",
        lambda *_args, **_kwargs: SimpleNamespace(agent=Runtime()),
    )

    async def scenario():
        agent = AgentBuilder().with_project_config(workspace=tmp_path).build()
        startup = asyncio.create_task(agent.start())
        await asyncio.wait_for(entered.wait(), 2)
        await asyncio.wait_for(agent.close(), 2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(startup, 2)
        assert interrupted.is_set()

    asyncio.run(scenario())


def test_sdk_close_before_worker_first_runs_settles_startup(monkeypatch, tmp_path):
    prepared = []

    class Runtime:
        async def __aenter__(self):
            prepared.append(True)
            return self

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(
        "cli_agent.sdk.agent.prepare_agent",
        lambda *_args, **_kwargs: SimpleNamespace(agent=Runtime()),
    )

    async def scenario():
        agent = AgentBuilder().with_project_config(workspace=tmp_path).build()
        real_worker = agent._worker
        wrapper_entered = asyncio.Event()
        release_worker = asyncio.Event()

        async def suspended_worker():
            # Deterministically keep _worker's body from running, including
            # its try/finally. asyncio.sleep(0) cannot guarantee this ordering.
            wrapper_entered.set()
            await release_worker.wait()
            await real_worker()

        agent._worker = suspended_worker
        startup = asyncio.create_task(agent.start())
        await asyncio.wait_for(wrapper_entered.wait(), timeout=2)
        assert agent._ready is not None and not agent._ready.done()

        await asyncio.wait_for(agent.close(), timeout=2)
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(startup, timeout=2)
        assert agent._worker_task is None
        assert agent._ready.done()
        assert agent._closed

    asyncio.run(scenario())
    assert prepared == []


def test_sdk_tool_trace_and_events_are_per_run(monkeypatch, tmp_path):
    class Runtime:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            pass
        async def ask(self, prompt):
            await self.event_callback("model_started", {"call_id": "m", "phase": "main"})
            await self.event_callback("model_finished", {"call_id": "m", "phase": "main", "status": "success"})
            await self.event_callback("tool_started", {"call_id": "t", "tool": "data__aggregate_data", "arguments": {"secret": "NEVER"}})
            await self.event_callback("tool_finished", {"call_id": "t", "tool": "data__aggregate_data", "status": "success", "result": "NEVER"})
            return prompt
    monkeypatch.setattr("cli_agent.sdk.agent.prepare_agent", lambda *_a, **_k: SimpleNamespace(agent=Runtime()))
    seen = []
    async def on_event(event):
        seen.append(event)
    async def scenario():
        async with AgentBuilder().with_project_config(workspace=tmp_path).with_event_callback(on_event).build() as agent:
            a = await agent.ask("first")
            b = await agent.ask("second")
            assert a.answer == "first" and b.answer == "second"
            assert len(a.tool_calls) == len(b.tool_calls) == 1
            assert a.tool_calls[0].name == "data__aggregate_data"
            assert a.tool_calls[0].status == "success"
            assert a.tool_calls[0].duration_ms >= 0
            assert a.events[0].type == "run_started"
            assert a.events[-1].type == "run_finished"
            assert a.events[0].run_id != b.events[0].run_id
            assert all("NEVER" not in repr(e) for e in a.events)
            await asyncio.sleep(0)
        assert seen
    asyncio.run(scenario())


def test_sdk_default_callback_prints_metadata_only(monkeypatch, tmp_path, capsys):
    from cli_agent.sdk.events import AgentEvent
    from cli_agent.sdk import default_event_callback
    default_event_callback(AgentEvent("tool_finished", "run", "os__read_file", "a", "success", duration_ms=25))
    output = capsys.readouterr().out
    assert "os__read_file" in output
    assert "success" in output
    assert "0.03 s" in output
    agent = AgentBuilder().with_default_event_callback(verbose=True).build()
    assert callable(agent._event_callback)


def test_sdk_approval_callback_is_separate_from_events():
    async def approve(tool, args):
        return tool == "os__write_file" and args.get("path") == "ok.txt"
    a = AgentBuilder().with_approval_callback(approve).build()
    assert a._options["approval_callback"] is approve


def test_runtime_rejects_non_boolean_approval_responses():
    from cli_agent.runtime.agent import CliAgent
    from cli_agent.settings.config import McpServerConfig
    class Model:
        pass
    async def scenario():
        agent = CliAgent(Path.cwd(), Model(), ())
        for value in ("no", "yes", "false", 1, None, {}, []):
            async def approval(_tool, _args, return_value=value):
                return return_value
            agent.approval_callback = approval
            assert await agent._approve_tool_call("os__write_file", {"path": "x"}) is False
        async def approved(_tool, _args):
            return True
        agent.approval_callback = approved
        assert await agent._approve_tool_call("os__write_file", {}) is True
        async def session(_tool, _args):
            return "session"
        agent.approval_callback = session
        assert await agent._approve_tool_call("os__write_file", {}) is True
        assert "os__write_file" in agent._session_approved_tools
    asyncio.run(scenario())
