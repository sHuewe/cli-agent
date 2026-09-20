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
        ("version = 1\\nunknown = true", "Unbekannte Flow-Schlüssel"),
        ("version = 2\\nsteps = []", "version = 1"),
        ("version = 1\\nsteps = []", "mindestens einen"),
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
