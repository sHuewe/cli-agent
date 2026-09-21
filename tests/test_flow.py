from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli_agent.flow as flow_module
from cli_agent.flow import load_flow, run_flow, validate_flow


def _write_config(path: Path) -> None:
    path.write_text(
        "[model]\n"
        'provider = "ollama"\n'
        'model = "test"\n'
        'base_url = "http://localhost:11434"\n',
        encoding="utf-8",
    )



def test_flow_cli_validate_command(tmp_path: Path, monkeypatch, capsys) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        flow_module.sys,
        "argv",
        [
            "cli-agent-flow",
            "validate",
            "flow.toml",
            "--workspace",
            str(tmp_path),
        ],
    )

    flow_module.main()

    output = capsys.readouterr().out
    assert "Flow gültig:" in output
    assert "1 Schritte" in output


def test_flow_cli_run_command_invokes_runner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )
    captured = {}

    async def fake_run_flow(flow, *, workspace, approval_callback):
        captured["flow"] = flow
        captured["workspace"] = workspace
        captured["approval_callback"] = approval_callback

    monkeypatch.setattr(flow_module, "run_flow", fake_run_flow)
    monkeypatch.setattr(
        flow_module.sys,
        "argv",
        [
            "cli-agent-flow",
            "run",
            "flow.toml",
            "--workspace",
            str(tmp_path),
        ],
    )

    flow_module.main()

    assert captured["flow"].steps[0].step_id == "one"
    assert captured["workspace"] == tmp_path.resolve()
    assert captured["approval_callback"] is flow_module.approve_tool_call


def test_flow_cli_reports_validation_error(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    monkeypatch.setattr(
        flow_module.sys,
        "argv",
        [
            "cli-agent-flow",
            "validate",
            "missing.toml",
            "--workspace",
            str(tmp_path),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        flow_module.main()

    assert exc_info.value.code == 1
    assert "Fehler: ValueError:" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("flow_text", "message"),
    [
        ("version = 1\nunknown = true", "Unbekannte Flow-Schlüssel"),
        ("version = 2\nsteps = []", "version = 1"),
        ("version = 1\nsteps = []", "mindestens einen"),
        (
            """
version = 1
[[steps]]
id = "one"
prompt_file = "prompt.md"
[[steps]]
id = "one"
prompt_file = "prompt.md"
""".strip(),
            "Doppelte Step-ID",
        ),
        (
            """
version = 1
[[steps]]
id = "one"
prompt_file = "prompt.md"
add_file_context = []
""".strip(),
            "nichtleerer String",
        ),
        (
            """
version = 1
[[steps]]
id = "one"
prompt_file = "prompt.md"
add_web_context = ["https://docs.example/a", "https://docs.example/a"]
""".strip(),
            "doppelten URLs",
        ),
        (
            """
version = 1
[[steps]]
id = "one"
prompt_file = "prompt.md"
retry = "invalid"
""".strip(),
            "retry muss eine Tabelle",
        ),
        (
            """
version = 1
[[steps]]
id = "one"
prompt_file = "prompt.md"
overwrite_output = "yes"
""".strip(),
            "overwrite_output",
        ),
    ],
)
def test_flow_rejects_invalid_definition_shapes(
    tmp_path: Path,
    flow_text: str,
    message: str,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(flow_text, encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)

def test_flow_uses_execution_core_fixed_workspace_configs_and_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "work").mkdir()
    (tmp_path / "prompts" / "discover.md").write_text(
        "Discover items",
        encoding="utf-8",
    )
    (tmp_path / "prompts" / "process.md").write_text(
        "Process {{var:id}}",
        encoding="utf-8",
    )
    _write_config(tmp_path / "config-a.toml")
    _write_config(tmp_path / "config-b.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
config = "config-a.toml"
prompt_file = "prompts/discover.md"
workspace_access = "read"
output = "work/items.json"
overwrite_output = true

[[steps]]
id = "process"
config = "config-b.toml"
prompt_file = "prompts/process.md"
workspace_access = "write"
foreach = "steps.discover.output.items"
output = "work/${item.id}.md"
overwrite_output = true

[steps.vars]
id = "${item.id}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "Discover items":
            answer = '{"items":[{"id":"one"},{"id":"two"}]}'
        else:
            answer = "done"
        if options.output is not None:
            options.output.write_text(answer, encoding="utf-8")
        return SimpleNamespace(answer=answer)

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(
        tmp_path / "flow.toml",
        workspace=tmp_path,
    )
    asyncio.run(
        run_flow(
            definition,
            workspace=tmp_path,
        )
    )

    assert len(calls) == 3
    assert all(call.workspace == tmp_path.resolve() for call in calls)
    assert calls[0].config_file.name == "config-a.toml"
    assert calls[1].config_file.name == "config-b.toml"
    assert calls[0].workspace_access == "read"
    assert calls[1].workspace_access == "write"
    assert calls[2].workspace_access == "write"
    assert calls[1].prompt == "Process one"
    assert calls[2].prompt == "Process two"
    assert (tmp_path / "work" / "one.md").read_text(encoding="utf-8") == "done"
    assert (tmp_path / "work" / "two.md").read_text(encoding="utf-8") == "done"


def test_flow_has_no_subprocess_execution_dependency() -> None:
    assert not hasattr(flow_module, "subprocess")





def test_flow_parses_step_file_and_web_contexts(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "context.txt").write_text("reference", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
add_file_context = "context.txt"
add_web_context = [
    "https://docs.example/a",
    "https://docs.example/b",
]
""".strip(),
        encoding="utf-8",
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    step = definition.steps[0]

    assert step.context_files == (Path("context.txt"),)
    assert step.add_web_context == (
        "https://docs.example/a",
        "https://docs.example/b",
    )



def test_flow_parses_multiple_file_contexts(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "b.txt").write_text("b", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
add_file_context = ["a.txt", "b.txt"]
""".strip(),
        encoding="utf-8",
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)

    assert definition.steps[0].context_files == (
        Path("a.txt"),
        Path("b.txt"),
    )


def test_flow_rejects_duplicate_file_contexts(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
add_file_context = ["a.txt", "a.txt"]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="doppelten Dateien"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)


def test_flow_passes_step_contexts_to_execution_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "context.txt").write_text("reference", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
add_file_context = "context.txt"
add_web_context = ["https://docs.example/reference"]
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer="done")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert len(calls) == 1
    assert calls[0].context_files == ((tmp_path / "context.txt").resolve(),)
    assert calls[0].add_web_context == ("https://docs.example/reference",)


def test_flow_rejects_dynamic_context_configuration(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")

    cases = (
        ("add_file_context", "\"${item.path}\"", "add_file_context"),
        ("add_web_context", "[\"https://docs.example/${item.id}\"]", "add_web_context"),
    )
    for key, value, message in cases:
        (tmp_path / "flow.toml").write_text(
            f"""
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
{key} = {value}
""".strip(),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=message):
            load_flow(tmp_path / "flow.toml", workspace=tmp_path)


def test_flow_rejects_context_file_alias_conflict(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "context.txt").write_text("reference", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
context_file = "context.txt"
add_file_context = "context.txt"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="nicht gleichzeitig"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)

def test_flow_parses_model_and_retry_policy(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
model = "small-model"
prompt_file = "prompt.md"

[steps.retry]
max_attempts = 3
initial_delay_seconds = 0.5
backoff_multiplier = 2
max_delay_seconds = 4
""".strip(),
        encoding="utf-8",
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    step = definition.steps[0]

    assert step.model == "small-model"
    assert step.retry_policy is not None
    assert step.retry_policy.max_attempts == 3
    assert step.retry_policy.initial_delay_seconds == 0.5
    assert step.retry_policy.backoff_multiplier == 2
    assert step.retry_policy.max_delay_seconds == 4


@pytest.mark.parametrize(
    ("retry_toml", "message"),
    [
        ("max_attempts = 0", "max_attempts"),
        ("max_attempts = 2.5", "max_attempts"),
        ("initial_delay_seconds = -1", "initial_delay_seconds"),
        ("backoff_multiplier = 0", "backoff_multiplier"),
        ("max_delay_seconds = 301", "max_delay_seconds"),
        ("unknown = 1", "unbekannte Schlüssel"),
    ],
)
def test_flow_rejects_invalid_retry_policy(
    tmp_path: Path,
    retry_toml: str,
    message: str,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        f"""
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"

[steps.retry]
{retry_toml}
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=message):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)


def test_flow_passes_model_and_retry_policy_to_execution_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
model = "fast-model"
prompt_file = "prompt.md"

[steps.retry]
max_attempts = 4
initial_delay_seconds = 0
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer="done")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert len(calls) == 1
    assert calls[0].model == "fast-model"
    assert calls[0].retry_policy is not None
    assert calls[0].retry_policy.max_attempts == 4
    assert calls[0].retry_policy.initial_delay_seconds == 0

def test_flow_parses_per_step_approve_tools(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
workspace_access = "write"
approve_tools = ["os__write_file", "os__make_directory"]
""".strip(),
        encoding="utf-8",
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)

    assert definition.steps[0].approve_tools == (
        "os__write_file",
        "os__make_directory",
    )


def test_flow_rejects_dynamic_or_duplicate_approve_tools(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")

    for value, message in (
        ('["os__write_file", "os__write_file"]', "doppelten"),
        ('["${item.tool}"]', "parametrisiert"),
    ):
        (tmp_path / "flow.toml").write_text(
            f"""
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
approve_tools = {value}
""".strip(),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match=message):
            load_flow(tmp_path / "flow.toml", workspace=tmp_path)


def test_flow_step_preapproval_is_exact_and_falls_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
workspace_access = "write"
approve_tools = ["os__write_file"]
""".strip(),
        encoding="utf-8",
    )

    captured = {}
    fallback_calls = []

    async def fallback(tool_name, arguments):
        fallback_calls.append((tool_name, arguments))
        return False

    async def fake_run_once(options, *, dependencies=None):
        captured["callback"] = options.approval_callback
        return SimpleNamespace(answer="done")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(
        run_flow(
            definition,
            workspace=tmp_path,
            approval_callback=fallback,
        )
    )

    callback = captured["callback"]
    assert asyncio.run(callback("os__write_file", {"path": "a.txt"})) is True
    assert asyncio.run(callback("os__delete_file", {"path": "a.txt"})) is False
    assert fallback_calls == [("os__delete_file", {"path": "a.txt"})]

def test_flow_rejects_workspace_override_in_step(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "bad"
config = "config.toml"
prompt_file = "prompt.md"
workspace = "../other"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unbekannte Schlüssel"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)


@pytest.mark.parametrize("access", ["none", "read", "write"])
def test_flow_accepts_explicit_workspace_access(
    tmp_path: Path,
    access: str,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        f"""
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
workspace_access = "{access}"
""".strip(),
        encoding="utf-8",
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    assert definition.steps[0].workspace_access == access


def test_flow_defaults_workspace_access_to_none(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    assert definition.steps[0].workspace_access == "none"


def test_flow_rejects_invalid_workspace_access(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
workspace_access = "admin"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="workspace_access"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)


def test_flow_rejects_llm_controlled_output_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text(
        "process {{var:path}}",
        encoding="utf-8",
    )
    (tmp_path / "work").mkdir()
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
config = "config.toml"
prompt_file = "prompt.md"

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.discover.output.items"
output = "work/${item.path}"
overwrite_output = true

[steps.vars]
path = "${item.path}"
""".strip(),
        encoding="utf-8",
    )

    async def fake_run_once(options, *, dependencies=None):
        return SimpleNamespace(
            answer='{"items":[{"path":"../../../escape.txt"}]}'
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(
        tmp_path / "flow.toml",
        workspace=tmp_path,
    )
    with pytest.raises(ValueError, match="darf '..' nicht enthalten"):
        asyncio.run(
            run_flow(
                definition,
                workspace=tmp_path,
            )
        )


def test_foreach_requires_strict_json_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
config = "config.toml"
prompt_file = "prompt.md"

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.discover.output.items"
""".strip(),
        encoding="utf-8",
    )

    async def fake_run_once(options, *, dependencies=None):
        return SimpleNamespace(answer="not json")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(
        tmp_path / "flow.toml",
        workspace=tmp_path,
    )
    with pytest.raises(ValueError, match="gültiges JSON"):
        asyncio.run(
            run_flow(
                definition,
                workspace=tmp_path,
            )
        )


def test_plain_final_step_does_not_require_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("final", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "final"
config = "config.toml"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )

    async def fake_run_once(options, *, dependencies=None):
        return SimpleNamespace(answer="ordinary prose")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(
        tmp_path / "flow.toml",
        workspace=tmp_path,
    )
    asyncio.run(
        run_flow(
            definition,
            workspace=tmp_path,
        )
    )


def test_validate_rejects_future_foreach_reference(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "first"
config = "config.toml"
prompt_file = "prompt.md"
foreach = "steps.later.output.items"

[[steps]]
id = "later"
config = "config.toml"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="vorherigen Schritt"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)


def test_validate_flow_checks_static_files(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    validate_flow(definition, workspace=tmp_path)


def test_flow_renders_null_foreach_item_as_empty_string(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text(
        "value={{var:value}}",
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
config = "config.toml"
prompt_file = "discover.md"

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.discover.output.items"
output = "result-${item}.txt"

[steps.vars]
value = "${item}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[null]}',
                web_context_statuses=(),
            )
        return SimpleNamespace(answer="done", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls[1].prompt == "value="
    assert calls[1].output == (tmp_path / "result-.txt").resolve()


def test_flow_detects_equivalent_resolved_output_paths_before_running_iterations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process", encoding="utf-8")
    (tmp_path / "work").mkdir()
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
config = "config.toml"
prompt_file = "discover.md"

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.discover.output.items"
output = "work/${item.path}"
overwrite_output = true
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[{"path":"a.md"},{"path":"./a.md"}]}',
                web_context_statuses=(),
            )
        return SimpleNamespace(answer="done", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="nicht eindeutige Output-Pfade"):
        asyncio.run(run_flow(definition, workspace=tmp_path))

    assert len(calls) == 1


def test_flow_prints_answer_when_step_has_no_output_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
) -> None:
    (tmp_path / "prompt.md").write_text("final", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "final"
config = "config.toml"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )

    async def fake_run_once(options, *, dependencies=None):
        return SimpleNamespace(
            answer="visible answer",
            web_context_statuses=("context loaded",),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    output = capsys.readouterr().out
    assert "context loaded" in output
    assert "visible answer" in output


@pytest.mark.parametrize(
    ("target_name", "later_field"),
    [
        ("later.md", ""),
        ("context.txt", "add_file_context = \"context.txt\""),
        ("later.toml", "config = \"later.toml\""),
    ],
)
def test_flow_rejects_output_collision_with_later_reserved_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    later_field: str,
) -> None:
    (tmp_path / "first.md").write_text("first", encoding="utf-8")
    (tmp_path / "later.md").write_text("later", encoding="utf-8")
    (tmp_path / "context.txt").write_text("context", encoding="utf-8")
    _write_config(tmp_path / "first.toml")
    _write_config(tmp_path / "later.toml")
    (tmp_path / "flow.toml").write_text(
        f"""
version = 1

[[steps]]
id = "first"
config = "first.toml"
prompt_file = "first.md"
output = "{target_name}"
overwrite_output = true

[[steps]]
id = "later"
prompt_file = "later.md"
{later_field}
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(
            answer="model output",
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)

    with pytest.raises(ValueError, match="reservierten Flow-Eingabe"):
        asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls == []


def test_filesystem_path_key_collapses_case_when_filesystem_is_case_insensitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        flow_module,
        "_filesystem_is_case_insensitive",
        lambda _path: True,
    )

    upper = flow_module._filesystem_path_key(tmp_path / "Result" / "A.txt")
    lower = flow_module._filesystem_path_key(tmp_path / "result" / "a.TXT")

    assert upper == lower


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_foreach_rejects_non_standard_json_constants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    constant: str,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
config = "config.toml"
prompt_file = "discover.md"

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.discover.output.items"
""".strip(),
        encoding="utf-8",
    )

    async def fake_run_once(options, *, dependencies=None):
        return SimpleNamespace(
            answer=f'{{"items":[{constant}]}}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="gültiges JSON"):
        asyncio.run(run_flow(definition, workspace=tmp_path))


def test_validate_flow_parses_explicit_step_config(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "bad.toml").write_text(
        "[model\n",
        encoding="utf-8",
    )
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "bad.toml"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="Konfiguration.*ungültig"):
        validate_flow(definition, workspace=tmp_path)


def test_validate_flow_rejects_static_output_escape(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
output = "../escape.txt"
""".strip(),
        encoding="utf-8",
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)

    with pytest.raises(ValueError, match="darf '..' nicht enthalten"):
        validate_flow(definition, workspace=tmp_path)


def test_run_flow_uses_injected_config_loader_for_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )

    loaded = []

    def fake_load_config(path):
        loaded.append(path)
        return flow_module.load_config(path)

    dependencies = flow_module.ExecutionDependencies(
        load_config=fake_load_config,
    )

    async def fake_run_once(options, *, dependencies=None):
        return SimpleNamespace(
            answer="ok",
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(
        run_flow(
            definition,
            workspace=tmp_path,
            dependencies=dependencies,
        )
    )

    assert loaded == [(tmp_path / "config.toml").resolve()]


def test_flow_passes_all_reserved_inputs_as_mutation_protection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "first.md").write_text("first", encoding="utf-8")
    (tmp_path / "second.md").write_text("second", encoding="utf-8")
    (tmp_path / "context.txt").write_text("context", encoding="utf-8")
    _write_config(tmp_path / "first.toml")
    _write_config(tmp_path / "second.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "first"
config = "first.toml"
prompt_file = "first.md"
workspace_access = "write"

[[steps]]
id = "second"
config = "second.toml"
prompt_file = "second.md"
add_file_context = "context.txt"
""".strip(),
        encoding="utf-8",
    )

    seen = []

    async def fake_run_once(options, *, dependencies=None):
        seen.append(options)
        return SimpleNamespace(answer="ok", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    expected = {
        (tmp_path / "flow.toml").resolve(),
        (tmp_path / "first.md").resolve(),
        (tmp_path / "second.md").resolve(),
        (tmp_path / "context.txt").resolve(),
        (tmp_path / "first.toml").resolve(),
        (tmp_path / "second.toml").resolve(),
    }
    assert len(seen) == 2
    assert set(seen[0].mutation_protected_paths) == expected
    assert set(seen[1].mutation_protected_paths) == expected

def test_validate_flow_rejects_duplicate_static_output_without_overwrite(
    tmp_path: Path,
) -> None:
    (tmp_path / "one.md").write_text("one", encoding="utf-8")
    (tmp_path / "two.md").write_text("two", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "one.md"
output = "result.txt"

[[steps]]
id = "two"
config = "config.toml"
prompt_file = "two.md"
output = "./result.txt"
""".strip(),
        encoding="utf-8",
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)

    with pytest.raises(ValueError, match="geplanten Output"):
        validate_flow(definition, workspace=tmp_path)


@pytest.mark.parametrize(
    ("items_json", "expected_message"),
    [
        (
            '{"items":[{"path":"ok.md"},{"path":"process.md"}]}',
            "reservierten Flow-Eingabe",
        ),
        (
            '{"items":[{"path":"ok.md"},{"path":"existing.md"}]}',
            "existiert bereits",
        ),
        (
            '{"items":[{"path":"ok.md"},{"path":"missing/out.md"}]}',
            "Output-Ordner existiert nicht",
        ),
    ],
)
def test_foreach_output_batch_preflight_blocks_before_first_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    items_json: str,
    expected_message: str,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process", encoding="utf-8")
    (tmp_path / "existing.md").write_text("existing", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
config = "config.toml"
prompt_file = "discover.md"

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.discover.output.items"
output = "${item.path}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer=items_json,
                web_context_statuses=(),
            )
        return SimpleNamespace(answer="processed", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match=expected_message):
        asyncio.run(run_flow(definition, workspace=tmp_path))

    assert len(calls) == 1
    assert calls[0].prompt == "discover"


def test_flow_response_format_defaults_to_text(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)

    assert definition.steps[0].response_format == "text"


def test_flow_parses_json_response_format(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
prompt_file = "prompt.md"
response_format = "json"
""".strip(),
        encoding="utf-8",
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)

    assert definition.steps[0].response_format == "json"


def test_flow_rejects_unknown_response_format(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
prompt_file = "prompt.md"
response_format = "yaml"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="response_format"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)


def test_flow_passes_response_format_to_execution_core(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
prompt_file = "prompt.md"
response_format = "json"
""".strip(),
        encoding="utf-8",
    )
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer='{"ok":true}', web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls[0].response_format == "json"


def test_flow_assigns_dump_prefix_per_step_and_foreach_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text(
        "process {{var:name}}",
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "extract"
config = "config.toml"
prompt_file = "discover.md"

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.extract.output.items"

[steps.vars]
name = "${item.name}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[{"name":"one"},{"name":"two"}]}',
                web_context_statuses=(),
            )
        return SimpleNamespace(
            answer="done",
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.dump_file_prefix for call in calls] == [
        "extract",
        "process.foreach-1",
        "process.foreach-2",
    ]


def test_flow_dump_prefixes_cannot_collide_with_step_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "process_1"
config = "config.toml"
prompt_file = "discover.md"

[[steps]]
id = "discover"
config = "config.toml"
prompt_file = "discover.md"

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.discover.output.items"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[1]}',
                web_context_statuses=(),
            )
        return SimpleNamespace(answer="ok", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.dump_file_prefix for call in calls] == [
        "process_1",
        "discover",
        "process.foreach-1",
    ]
    assert len({call.dump_file_prefix for call in calls}) == 3


def test_flow_accepts_long_step_id_without_oversized_dump_filename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    long_id = "step_" + ("a" * 400)
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        f"""
version = 1

[[steps]]
id = "{long_id}"
config = "config.toml"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer="ok", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    prefix = calls[0].dump_file_prefix
    assert prefix == long_id

    agent = flow_module.ExecutionDependencies().agent_type
    assert agent is not None


def test_flow_disambiguates_case_colliding_step_ids_on_case_insensitive_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "Build"
config = "config.toml"
prompt_file = "prompt.md"

[[steps]]
id = "build"
config = "config.toml"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer="ok", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    monkeypatch.setattr(
        flow_module,
        "_filesystem_is_case_insensitive",
        lambda _path: True,
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    prefixes = [call.dump_file_prefix for call in calls]
    assert prefixes[0].startswith("Build.case-")
    assert prefixes[1].startswith("build.case-")
    assert prefixes[0].casefold() != prefixes[1].casefold()


def test_flow_keeps_case_distinct_prefixes_readable_on_case_sensitive_filesystem(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "Build"
config = "config.toml"
prompt_file = "prompt.md"

[[steps]]
id = "build"
config = "config.toml"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer="ok", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    monkeypatch.setattr(
        flow_module,
        "_filesystem_is_case_insensitive",
        lambda _path: False,
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.dump_file_prefix for call in calls] == ["Build", "build"]


def test_flow_data_paths_are_relative_to_workspace_not_flow_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "flow").mkdir()
    (tmp_path / "okf").mkdir()
    (tmp_path / "handbuch.txt").write_text("Handbuch", encoding="utf-8")
    (tmp_path / "anweisungen.txt").write_text("Anweisungen", encoding="utf-8")
    (tmp_path / "flow" / "prompt.md").write_text(
        "Erstelle das OKF.",
        encoding="utf-8",
    )
    _write_config(tmp_path / "flow" / "config.toml")
    (tmp_path / "flow" / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "create_okf"
config = "config.toml"
prompt_file = "flow/prompt.md"
add_file_context = ["handbuch.txt", "anweisungen.txt"]
output = "okf/result.md"
overwrite_output = true
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(
            answer="done",
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(
        tmp_path / "flow" / "flow.toml",
        workspace=tmp_path,
    )
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert len(calls) == 1
    options = calls[0]
    assert options.prompt == "Erstelle das OKF."
    assert options.context_files == (
        (tmp_path / "handbuch.txt").resolve(),
        (tmp_path / "anweisungen.txt").resolve(),
    )
    assert options.output == (tmp_path / "okf" / "result.md").resolve()
    # Config bleibt bewusst relativ zur flow.toml.
    assert options.config_file == (tmp_path / "flow" / "config.toml")


def test_flow_relative_prompt_path_is_no_longer_resolved_from_flow_directory(
    tmp_path: Path,
) -> None:
    (tmp_path / "flow").mkdir()
    (tmp_path / "flow" / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow" / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )

    definition = load_flow(
        tmp_path / "flow" / "flow.toml",
        workspace=tmp_path,
    )

    with pytest.raises(ValueError, match="Prompt-Datei"):
        validate_flow(definition, workspace=tmp_path)
