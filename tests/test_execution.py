from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.admin_config import AdminConfig
from cli_agent.config import AppConfig, ModelConfig
from cli_agent.execution import (
    ExecutionDependencies,
    OneShotRunOptions,
    apply_workspace_access_override,
    build_preapproval_callback,
    run_once,
)
from cli_agent.file_context import FileContext
from cli_agent.model import (
    ModelRequestError,
    ModelRetryPolicy,
    RetryingModelClient,
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



def test_run_once_rejects_missing_workspace(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Arbeitsordner existiert nicht"):
        asyncio.run(
            run_once(
                OneShotRunOptions(
                    workspace=tmp_path / "missing",
                    prompt="work",
                )
            )
        )


def test_run_once_rejects_blank_prompt_before_loading_dependencies(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="darf nicht leer sein"):
        asyncio.run(
            run_once(
                OneShotRunOptions(
                    workspace=tmp_path,
                    prompt="   ",
                )
            )
        )


def test_run_once_rejects_mixed_prepared_and_raw_file_options(
    tmp_path: Path,
) -> None:
    context_path = tmp_path / "context.txt"
    context_path.write_text("context", encoding="utf-8")
    prepared = FileContext(
        "context.txt",
        "context",
        context_path,
        len(b"context"),
    )
    dependencies = ExecutionDependencies(
        load_config=lambda _path: _config(),
        load_admin_config=lambda: AdminConfig(),
        configure_logging=lambda _config: None,
        create_model_client=lambda *_args, **_kwargs: object(),
        agent_type=object,
    )

    with pytest.raises(ValueError, match="nicht zusammen"):
        asyncio.run(
            run_once(
                OneShotRunOptions(
                    workspace=tmp_path,
                    prompt="work",
                    context_files=(Path("context.txt"),),
                    prepared_file_contexts=(prepared,),
                ),
                dependencies=dependencies,
            )
        )


def test_workspace_access_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError, match="workspace_access"):
        apply_workspace_access_override(
            _config(),
            workspace_access="admin",
        )

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


def test_build_preapproval_callback_matches_exact_tool_name() -> None:
    fallback_calls = []

    async def fallback(tool_name, arguments):
        fallback_calls.append((tool_name, arguments))
        return False

    callback = build_preapproval_callback(
        ["os__write_file"],
        fallback=fallback,
    )

    assert asyncio.run(callback("os__write_file", {"path": "a.txt"})) is True
    assert asyncio.run(callback("os__write_file_extra", {})) is False
    assert fallback_calls == [("os__write_file_extra", {})]



def test_retrying_model_client_retries_only_retryable_errors(
    monkeypatch,
) -> None:
    class FakeModel:
        model = "m"
        base_url = "http://localhost"
        last_usage = None
        usage_history = []

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls < 3:
                raise ModelRequestError("temporary", retryable=True)
            return {"content": "ok"}

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("cli_agent.model.asyncio.sleep", fake_sleep)

    model = FakeModel()
    retrying = RetryingModelClient(
        model,
        ModelRetryPolicy(
            max_attempts=3,
            initial_delay_seconds=1,
            backoff_multiplier=2,
            max_delay_seconds=10,
        ),
    )

    result = asyncio.run(retrying.chat([], []))

    assert result == {"content": "ok"}
    assert model.calls == 3
    assert sleeps == [1, 2]


def test_retrying_model_client_does_not_retry_permanent_error(
    monkeypatch,
) -> None:
    class FakeModel:
        model = "m"
        base_url = "http://localhost"
        last_usage = None
        usage_history = []

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools):
            self.calls += 1
            raise ModelRequestError("permanent", retryable=False)

    async def fail_sleep(_delay):
        raise AssertionError("sleep must not be called")

    monkeypatch.setattr("cli_agent.model.asyncio.sleep", fail_sleep)

    model = FakeModel()
    retrying = RetryingModelClient(
        model,
        ModelRetryPolicy(max_attempts=5),
    )

    try:
        asyncio.run(retrying.chat([], []))
    except ModelRequestError as exc:
        assert exc.retryable is False
    else:
        raise AssertionError("expected ModelRequestError")

    assert model.calls == 1


def test_run_once_wraps_model_with_retry_policy(
    tmp_path: Path,
) -> None:
    captured = {}

    class BaseModel:
        model = "configured-model"
        base_url = "http://localhost"
        last_usage = None
        usage_history = []

        async def chat(self, *_args, **_kwargs):
            return {"content": "unused"}

    class FakeAgent:
        def __init__(self, _workspace, model_client, _servers, **_kwargs):
            captured["model_client"] = model_client

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
        create_model_client=lambda *_args, **_kwargs: BaseModel(),
        agent_type=FakeAgent,
    )

    policy = ModelRetryPolicy(max_attempts=3)
    asyncio.run(
        run_once(
            OneShotRunOptions(
                workspace=tmp_path,
                prompt="work",
                retry_policy=policy,
            ),
            dependencies=dependencies,
        )
    )

    assert isinstance(captured["model_client"], RetryingModelClient)


def test_retrying_model_client_caps_initial_delay_at_maximum(
    monkeypatch,
) -> None:
    class FakeModel:
        model = "m"
        base_url = "http://localhost"
        last_usage = None
        usage_history = []

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools):
            self.calls += 1
            if self.calls == 1:
                raise ModelRequestError("temporary", retryable=True)
            return {"content": "ok"}

    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("cli_agent.model.asyncio.sleep", fake_sleep)

    retrying = RetryingModelClient(
        FakeModel(),
        ModelRetryPolicy(
            max_attempts=2,
            initial_delay_seconds=60,
            max_delay_seconds=0,
        ),
    )

    result = asyncio.run(retrying.chat([], []))

    assert result == {"content": "ok"}
    assert sleeps == []


def test_workspace_write_passes_mutation_protected_paths_to_os_server(
    tmp_path: Path,
) -> None:
    prompt = (tmp_path / "prompt.md").resolve()
    context = (tmp_path / "context.txt").resolve()

    config = apply_workspace_access_override(
        _config(),
        workspace_access="write",
        mutation_protected_paths=(prompt, context),
    )

    server = config.mcp_servers[0]
    assert server.name == "os"
    assert server.allow_write_files() is True
    assert server.args[-2:] == ("--access", "write")
    assert server.literal_args == (
        "--mutation-protected-path",
        str(prompt),
        "--mutation-protected-path",
        str(context),
    )


def test_run_once_defaults_to_text_response_format(tmp_path: Path) -> None:
    captured = {}

    class FakeAgent:
        def __init__(self, *_args, **kwargs):
            captured["response_format"] = kwargs["response_format"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def ask(self, prompt):
            return "usage" if prompt == "tokens" else "plain text"

    dependencies = ExecutionDependencies(
        load_config=lambda _path: _config(),
        load_admin_config=lambda: AdminConfig(),
        configure_logging=lambda _config: None,
        create_model_client=lambda *_args, **_kwargs: object(),
        agent_type=FakeAgent,
    )

    result = asyncio.run(
        run_once(
            OneShotRunOptions(
                workspace=tmp_path,
                prompt="work",
            ),
            dependencies=dependencies,
        )
    )

    assert result.answer == "plain text"
    assert captured["response_format"] == "text"


def test_run_once_repairs_invalid_json_in_same_agent(tmp_path: Path) -> None:
    prompts = []
    instances = []

    class FakeAgent:
        def __init__(self, *_args, **kwargs):
            instances.append(self)
            assert kwargs["response_format"] == "json"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def ask(self, prompt):
            prompts.append(prompt)
            if prompt == "tokens":
                return "usage"
            non_token_prompts = [value for value in prompts if value != "tokens"]
            if len(non_token_prompts) == 1:
                return "not json"
            return '{"value":1}'

    dependencies = ExecutionDependencies(
        load_config=lambda _path: _config(),
        load_admin_config=lambda: AdminConfig(),
        configure_logging=lambda _config: None,
        create_model_client=lambda *_args, **_kwargs: object(),
        agent_type=FakeAgent,
    )

    result = asyncio.run(
        run_once(
            OneShotRunOptions(
                workspace=tmp_path,
                prompt="work",
                response_format="json",
            ),
            dependencies=dependencies,
        )
    )

    assert result.answer == '{"value":1}'
    assert len(instances) == 1
    assert len(prompts) == 3
    assert "Korrigiere die Antwort jetzt" in prompts[1]
    assert "verfügbaren Tools verwenden" in prompts[1]


def test_run_once_fails_after_two_json_format_repairs(tmp_path: Path) -> None:
    prompts = []

    class FakeAgent:
        def __init__(self, *_args, **kwargs):
            assert kwargs["response_format"] == "json"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def ask(self, prompt):
            prompts.append(prompt)
            return "still not json"

    dependencies = ExecutionDependencies(
        load_config=lambda _path: _config(),
        load_admin_config=lambda: AdminConfig(),
        configure_logging=lambda _config: None,
        create_model_client=lambda *_args, **_kwargs: object(),
        agent_type=FakeAgent,
    )

    with pytest.raises(ValueError, match="nach 2 Korrekturversuchen"):
        asyncio.run(
            run_once(
                OneShotRunOptions(
                    workspace=tmp_path,
                    prompt="work",
                    response_format="json",
                ),
                dependencies=dependencies,
            )
        )

    assert len(prompts) == 3


def test_run_once_rejects_unknown_response_format(tmp_path: Path) -> None:
    dependencies = ExecutionDependencies(
        load_config=lambda _path: _config(),
        load_admin_config=lambda: AdminConfig(),
        configure_logging=lambda _config: None,
        create_model_client=lambda *_args, **_kwargs: object(),
        agent_type=object,
    )

    with pytest.raises(ValueError, match="response_format"):
        asyncio.run(
            run_once(
                OneShotRunOptions(
                    workspace=tmp_path,
                    prompt="work",
                    response_format="yaml",
                ),
                dependencies=dependencies,
            )
        )


@pytest.mark.parametrize("invalid_json", ["NaN", "Infinity", "-Infinity"])
def test_json_response_format_rejects_non_standard_constants(
    tmp_path: Path,
    invalid_json: str,
) -> None:
    answers = [invalid_json, '{"ok":true}']

    class FakeAgent:
        def __init__(self, *_args, **kwargs):
            assert kwargs["response_format"] == "json"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def ask(self, prompt):
            if prompt == "tokens":
                return "usage"
            return answers.pop(0)

    dependencies = ExecutionDependencies(
        load_config=lambda _path: _config(),
        load_admin_config=lambda: AdminConfig(),
        configure_logging=lambda _config: None,
        create_model_client=lambda *_args, **_kwargs: object(),
        agent_type=FakeAgent,
    )

    result = asyncio.run(
        run_once(
            OneShotRunOptions(
                workspace=tmp_path,
                prompt="work",
                response_format="json",
            ),
            dependencies=dependencies,
        )
    )

    assert result.answer == '{"ok":true}'
