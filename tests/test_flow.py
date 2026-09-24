from __future__ import annotations

import asyncio
import json
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


def test_flow_rejects_oversized_static_conversation_items(
    tmp_path: Path,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    items = ", ".join('"x"' for _ in range(flow_module.MAX_FOREACH_ITEMS + 1))
    (tmp_path / "flow.toml").write_text(
        (
            "version = 1\n\n"
            "[[steps]]\n"
            'id = "one"\n'
            'prompt_file = "prompt.md"\n'
            f"conversation_items = [{items}]\n"
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="conversation_items.*mehr als"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)


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



def test_flow_rejects_dynamic_output_collision_with_default_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process", encoding="utf-8")
    _write_config(tmp_path / "discover.toml")

    default_config = tmp_path / "state" / "cli-agent" / "config.toml"
    default_config.parent.mkdir(parents=True)
    _write_config(default_config)
    original_config = default_config.read_text(encoding="utf-8")

    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
config = "discover.toml"
prompt_file = "discover.md"

[[steps]]
id = "process"
prompt_file = "process.md"
foreach = "steps.discover.output.items"
output = "state/cli-agent/${item.path}"
overwrite_output = true
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        flow_module,
        "default_config_file",
        lambda: default_config,
    )

    def fake_load_config(path):
        if path is None:
            return flow_module.AppConfig()
        return flow_module.load_config(path)

    dependencies = flow_module.ExecutionDependencies(
        load_config=fake_load_config,
    )
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(
            answer='{"items":[{"path":"config.toml"}]}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)

    with pytest.raises(ValueError, match="reservierten Flow-Eingabe"):
        asyncio.run(
            run_flow(
                definition,
                workspace=tmp_path,
                dependencies=dependencies,
            )
        )

    assert len(calls) == 1
    assert default_config.read_text(encoding="utf-8") == original_config


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


def test_flow_merges_global_and_step_excluded_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        """
version = 1
exclude_paths = ["flow", "shared"]

[[steps]]
id = "read"
prompt_file = "prompt.md"
workspace_access = "read"
exclude_paths = ["private", "shared"]
""".strip(),
        encoding="utf-8",
    )
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer="ok", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    assert definition.excluded_paths == (Path("flow"), Path("shared"))
    assert definition.steps[0].excluded_paths == (
        Path("private"),
        Path("shared"),
    )

    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls[0].excluded_paths == (
        Path("flow"),
        Path("shared"),
        Path("private"),
    )


def test_flow_global_exclusions_are_ignored_for_step_without_os_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        """
version = 1
exclude_paths = ["flow"]

[[steps]]
id = "plain"
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

    assert calls[0].excluded_paths == ()


def test_flow_rejects_step_exclusions_without_os_access(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "plain"
prompt_file = "prompt.md"
exclude_paths = ["flow"]
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="exclude_paths benötigen workspace_access"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)


def test_iteration_id_requires_foreach(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
prompt_file = "prompt.md"
iteration_id = "one"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="iteration_id ist nur zusammen mit foreach"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)


def test_existing_json_output_resumes_static_step_and_drives_foreach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "plan.md").write_text("plan", encoding="utf-8")
    (tmp_path / "process.md").write_text(
        "process {{var:id}}",
        encoding="utf-8",
    )
    (tmp_path / "plan.json").write_text(
        '{"concepts":[{"id":"one"},{"id":"two"},{"id":"manual"}]}',
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "plan"
config = "config.toml"
prompt_file = "plan.md"
response_format = "json"
output = "plan.json"
overwrite_output = false

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.plan.output.concepts"

[steps.vars]
id = "${item.id}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer="done", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == [
        "process one",
        "process two",
        "process manual",
    ]


def test_invalid_existing_json_checkpoint_fails_without_model_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "checkpoint.json").write_text(
        "{invalid",
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
response_format = "json"
output = "checkpoint.json"
overwrite_output = false
""".strip(),
        encoding="utf-8",
    )
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer='{"ok":true}', web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="gültiges JSON"):
        asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls == []


def test_overwrite_true_executes_json_step_even_when_output_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "checkpoint.json").write_text(
        '{"old":true}',
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
response_format = "json"
output = "checkpoint.json"
overwrite_output = true
""".strip(),
        encoding="utf-8",
    )
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer='{"new":true}', web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert len(calls) == 1
    assert calls[0].overwrite_output is True


def test_foreach_iteration_ids_suffix_collisions_and_resume_individually(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text(
        "process {{var:iteration}}",
        encoding="utf-8",
    )
    (tmp_path / "status").mkdir()
    (tmp_path / "status" / "card.json").write_text(
        '{"status":"success","files_changed":["existing.md"]}',
        encoding="utf-8",
    )
    (tmp_path / "plan.json").write_text(
        '{"items":[{"name":"card"},{"name":"card"},{"name":"Card"}]}',
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
response_format = "json"
output = "plan.json"
overwrite_output = false

[[steps]]
id = "cards"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.discover.output.items"
iteration_id = "${item.name}"
response_format = "json"
output = "status/${iteration.id}.json"
overwrite_output = false

[steps.vars]
iteration = "${iteration.id}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(
            answer='{"status":"success","files_changed":[]}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == [
        "process card-2",
        "process Card-3",
    ]
    assert [call.output.name for call in calls] == [
        "card-2.json",
        "Card-3.json",
    ]
    assert [call.dump_file_prefix for call in calls] == [
        "cards.card-2",
        "cards.Card-3",
    ]


def test_foreach_invalid_later_checkpoint_blocks_all_iterations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process", encoding="utf-8")
    (tmp_path / "status").mkdir()
    (tmp_path / "status" / "one.json").write_text(
        '{"status":"success"}',
        encoding="utf-8",
    )
    (tmp_path / "status" / "two.json").write_text(
        "invalid",
        encoding="utf-8",
    )
    (tmp_path / "plan-invalid.json").write_text(
        '{"items":[{"id":"one"},{"id":"two"},{"id":"three"}]}',
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
response_format = "json"
output = "plan-invalid.json"
overwrite_output = false

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.discover.output.items"
iteration_id = "${item.id}"
response_format = "json"
output = "status/${iteration.id}.json"
overwrite_output = false
""".strip(),
        encoding="utf-8",
    )
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(
            answer='{"status":"success"}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="gültiges JSON"):
        asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls == []


def test_existing_text_output_still_requires_overwrite(
    tmp_path: Path,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "result.txt").write_text("existing", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
response_format = "text"
output = "result.txt"
overwrite_output = false
""".strip(),
        encoding="utf-8",
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)

    with pytest.raises(ValueError, match="existiert bereits"):
        validate_flow(definition, workspace=tmp_path)


def test_item_values_do_not_expand_iteration_id_placeholder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text(
        "value={{var:value}} iteration={{var:iteration}}",
        encoding="utf-8",
    )
    (tmp_path / "status").mkdir()
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
iteration_id = "${item.id}"
response_format = "json"
output = "status/${item.filename}"
overwrite_output = true

[steps.vars]
value = "${item.text}"
iteration = "${iteration.id}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer=(
                    '{"items":[{"id":"card","text":'
                    '"Keep ${iteration.id} literal",'
                    '"filename":"keep-${iteration.id}.json"}]}'
                ),
                web_context_statuses=(),
            )
        return SimpleNamespace(
            answer='{"status":"success"}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert len(calls) == 2
    assert calls[1].prompt == (
        "value=Keep ${iteration.id} literal iteration=card"
    )
    assert calls[1].output == (
        tmp_path / "status" / "keep-${iteration.id}.json"
    ).resolve()


def test_later_step_cannot_resume_output_claimed_by_earlier_foreach_in_same_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "create.md").write_text("create", encoding="utf-8")
    (tmp_path / "verify.md").write_text("verify", encoding="utf-8")
    (tmp_path / "shared").mkdir()
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
config = "config.toml"
prompt_file = "discover.md"

[[steps]]
id = "create"
config = "config.toml"
prompt_file = "create.md"
foreach = "steps.discover.output.items"
response_format = "json"
output = "shared/${item.id}.json"
overwrite_output = false

[[steps]]
id = "verify"
config = "config.toml"
prompt_file = "verify.md"
response_format = "json"
output = "shared/foo.json"
overwrite_output = false
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[{"id":"foo"}]}',
                web_context_statuses=(),
            )
        answer = '{"status":"success"}'
        if options.output is not None:
            options.output.write_text(answer, encoding="utf-8")
        return SimpleNamespace(answer=answer, web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="bereits von.*create"):
        asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == ["discover", "create"]
    assert (tmp_path / "shared" / "foo.json").exists()


def test_two_steps_cannot_claim_same_preexisting_json_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "one.md").write_text("one", encoding="utf-8")
    (tmp_path / "two.md").write_text("two", encoding="utf-8")
    (tmp_path / "checkpoint.json").write_text('{"ok":true}', encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "one.md"
response_format = "json"
output = "checkpoint.json"
overwrite_output = false

[[steps]]
id = "two"
config = "config.toml"
prompt_file = "two.md"
response_format = "json"
output = "checkpoint.json"
overwrite_output = false
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer='{"unexpected":true}', web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="geplanten Output|bereits von"):
        asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls == []


def test_later_overwrite_step_may_replace_output_claimed_in_same_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "first.md").write_text("first", encoding="utf-8")
    (tmp_path / "second.md").write_text("second", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "first"
config = "config.toml"
prompt_file = "first.md"
response_format = "json"
output = "result.json"
overwrite_output = true

[[steps]]
id = "second"
config = "config.toml"
prompt_file = "second.md"
response_format = "json"
output = "result.json"
overwrite_output = true
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        answer = '{"step":"' + options.prompt + '"}'
        if options.output is not None:
            options.output.write_text(answer, encoding="utf-8")
        return SimpleNamespace(answer=answer, web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == ["first", "second"]
    assert (tmp_path / "result.json").read_text(encoding="utf-8") == '{"step":"second"}'


def test_later_foreach_with_overwrite_may_replace_claimed_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "first.md").write_text("first", encoding="utf-8")
    (tmp_path / "second.md").write_text("second", encoding="utf-8")
    (tmp_path / "out").mkdir()
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
config = "config.toml"
prompt_file = "discover.md"

[[steps]]
id = "first"
config = "config.toml"
prompt_file = "first.md"
foreach = "steps.discover.output.items"
response_format = "json"
output = "out/${item.id}.json"
overwrite_output = true

[[steps]]
id = "second"
config = "config.toml"
prompt_file = "second.md"
foreach = "steps.discover.output.items"
response_format = "json"
output = "out/${item.id}.json"
overwrite_output = true
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[{"id":"foo"}]}',
                web_context_statuses=(),
            )
        answer = '{"step":"' + options.prompt + '"}'
        if options.output is not None:
            options.output.write_text(answer, encoding="utf-8")
        return SimpleNamespace(answer=answer, web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == ["discover", "first", "second"]
    assert (tmp_path / "out" / "foo.json").read_text(encoding="utf-8") == '{"step":"second"}'


def test_iteration_id_suffix_does_not_steal_natural_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process {{var:iteration}}", encoding="utf-8")
    (tmp_path / "status").mkdir()
    (tmp_path / "status" / "card.json").write_text('{"status":"success"}', encoding="utf-8")
    (tmp_path / "status" / "card-2.json").write_text('{"status":"success"}', encoding="utf-8")
    (tmp_path / "plan-natural.json").write_text(
        '{"items":[{"id":"card"},{"id":"card"},{"id":"card-2"}]}',
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
response_format = "json"
output = "plan-natural.json"
overwrite_output = false

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.discover.output.items"
iteration_id = "${item.id}"
response_format = "json"
output = "status/${iteration.id}.json"
overwrite_output = false

[steps.vars]
iteration = "${iteration.id}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(
            answer='{"status":"success"}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == ["process card-3"]
    assert calls[0].output == (tmp_path / "status" / "card-3.json").resolve()
    assert calls[0].dump_file_prefix == "process.card-3"


def test_foreach_preflight_does_not_retain_all_checkpoint_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process", encoding="utf-8")
    (tmp_path / "status").mkdir()
    (tmp_path / "status" / "one.json").write_text('{"status":"one"}', encoding="utf-8")
    (tmp_path / "status" / "two.json").write_text('{"status":"two"}', encoding="utf-8")
    (tmp_path / "plan-memory.json").write_text(
        '{"items":[{"id":"one"},{"id":"two"}]}',
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
response_format = "json"
output = "plan-memory.json"
overwrite_output = false

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.discover.output.items"
iteration_id = "${item.id}"
response_format = "json"
output = "status/${iteration.id}.json"
overwrite_output = false
""".strip(),
        encoding="utf-8",
    )

    reads = []
    real_reader = flow_module.read_existing_output_text

    def tracking_reader(workspace, path, *, max_bytes):
        reads.append(path.name)
        return real_reader(workspace, path, max_bytes=max_bytes)

    monkeypatch.setattr(flow_module, "read_existing_output_text", tracking_reader)

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        raise AssertionError("checkpointed foreach iteration must not execute")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls == []
    assert reads.count("one.json") == 3
    assert reads.count("two.json") == 3


def test_foreach_detects_checkpoint_modified_after_preflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process {{var:id}}", encoding="utf-8")
    (tmp_path / "status").mkdir()
    (tmp_path / "status" / "one.json").write_text('{"status":"one"}', encoding="utf-8")
    (tmp_path / "status" / "two.json").write_text('{"status":"two"}', encoding="utf-8")
    (tmp_path / "plan-integrity.json").write_text(
        '{"items":[{"id":"one"},{"id":"two"}]}',
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
response_format = "json"
output = "plan-integrity.json"
overwrite_output = false

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.discover.output.items"
iteration_id = "${item.id}"
response_format = "json"
output = "status/${iteration.id}.json"
overwrite_output = false

[steps.vars]
id = "${item.id}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        raise AssertionError("checkpointed iteration must not execute")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    original = flow_module._verified_preflight_checkpoint
    verification_calls = 0

    def tampering_verifier(step, *, workspace, output, expected_fingerprint):
        nonlocal verification_calls
        verification_calls += 1
        if verification_calls == 1:
            (tmp_path / "status" / "two.json").write_text(
                '{"status":"changed"}',
                encoding="utf-8",
            )
        return original(
            step,
            workspace=workspace,
            output=output,
            expected_fingerprint=expected_fingerprint,
        )

    monkeypatch.setattr(
        flow_module,
        "_verified_preflight_checkpoint",
        tampering_verifier,
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="nach dem Preflight verändert"):
        asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls == []


def test_foreach_checkpoint_paths_are_mutation_protected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process {{var:id}}", encoding="utf-8")
    (tmp_path / "status").mkdir()
    (tmp_path / "status" / "one.json").write_text('{"status":"one"}', encoding="utf-8")
    (tmp_path / "plan-protect.json").write_text(
        '{"items":[{"id":"one"},{"id":"two"}]}',
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
response_format = "json"
output = "plan-protect.json"
overwrite_output = false

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
workspace_access = "write"
foreach = "steps.discover.output.items"
iteration_id = "${item.id}"
response_format = "json"
output = "status/${iteration.id}.json"
overwrite_output = false

[steps.vars]
id = "${item.id}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(
            answer='{"status":"success"}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    process_call = next(call for call in calls if call.prompt == "process two")
    assert (tmp_path / "status" / "one.json").resolve() in (
        process_call.mutation_protected_paths
    )


def test_file_created_by_earlier_step_is_not_a_resume_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "create.md").write_text("create", encoding="utf-8")
    (tmp_path / "later.md").write_text("later", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "create"
config = "config.toml"
prompt_file = "create.md"
workspace_access = "write"

[[steps]]
id = "later"
config = "config.toml"
prompt_file = "later.md"
response_format = "json"
output = "later.json"
overwrite_output = false
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "create":
            (tmp_path / "later.json").write_text(
                '{"created":"during-run"}',
                encoding="utf-8",
            )
            return SimpleNamespace(answer="created", web_context_statuses=())
        raise AssertionError("later step must not resume or execute")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="existiert bereits"):
        asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == ["create"]


def test_preexisting_static_json_checkpoint_still_resumes_from_run_start_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "checkpoint.json").write_text('{"ok":true}', encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"
response_format = "json"
output = "checkpoint.json"
overwrite_output = false
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        raise AssertionError("pre-existing checkpoint must skip the model")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls == []


def test_foreach_checkpoint_is_only_resumable_when_known_at_run_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process", encoding="utf-8")
    (tmp_path / "status").mkdir()
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
iteration_id = "${item.id}"
response_format = "json"
output = "status/${iteration.id}.json"
overwrite_output = false
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            (tmp_path / "status" / "one.json").write_text(
                '{"created":"during-run"}',
                encoding="utf-8",
            )
            return SimpleNamespace(
                answer='{"items":[{"id":"one"}]}',
                web_context_statuses=(),
            )
        raise AssertionError("process iteration must not resume or execute")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="existiert bereits"):
        asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == ["discover"]


def test_iteration_id_allows_leading_hyphen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process {{var:id}}", encoding="utf-8")
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
iteration_id = "${item.id}"

[steps.vars]
id = "${iteration.id}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[{"id":"-card"}]}',
                web_context_statuses=(),
            )
        return SimpleNamespace(answer="done", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == ["discover", "process -card"]
    assert calls[1].dump_file_prefix == "process.-card"


def test_overwrite_step_does_not_inherit_previous_steps_checkpoint_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "resume.md").write_text("resume", encoding="utf-8")
    (tmp_path / "replace.md").write_text("replace", encoding="utf-8")
    (tmp_path / "result.json").write_text('{"old":true}', encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "resume"
config = "config.toml"
prompt_file = "resume.md"
response_format = "json"
output = "result.json"
overwrite_output = false

[[steps]]
id = "replace"
config = "config.toml"
prompt_file = "replace.md"
response_format = "json"
output = "result.json"
overwrite_output = true
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        assert options.prompt == "replace"
        answer = '{"new":true}'
        if options.output is not None:
            options.output.write_text(answer, encoding="utf-8")
        return SimpleNamespace(answer=answer, web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == ["replace"]
    assert (tmp_path / "result.json").read_text(encoding="utf-8") == '{"new":true}'


def test_later_step_can_use_previous_json_output_in_variables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "first.md").write_text("first", encoding="utf-8")
    (tmp_path / "second.md").write_text(
        "summary={{var:summary}} nested={{var:nested}} complete={{var:complete}}",
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "first"
config = "config.toml"
prompt_file = "first.md"
response_format = "json"

[[steps]]
id = "second"
config = "config.toml"
prompt_file = "second.md"

[steps.vars]
summary = "${steps.first.output.summary}"
nested = "${steps.first.output.details.value}"
complete = "${steps.first.output}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "first":
            return SimpleNamespace(
                answer='{"summary":"done","details":{"value":7}}',
                web_context_statuses=(),
            )
        return SimpleNamespace(answer="ok", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    validate_flow(definition, workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == [
        "first",
        'summary=done nested=7 complete={"summary":"done","details":{"value":7}}',
    ]


def test_step_output_variable_serializes_structured_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "first.md").write_text("first", encoding="utf-8")
    (tmp_path / "second.md").write_text("items={{var:items}}", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "first"
config = "config.toml"
prompt_file = "first.md"
response_format = "json"

[[steps]]
id = "second"
config = "config.toml"
prompt_file = "second.md"

[steps.vars]
items = "${steps.first.output.items}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "first":
            return SimpleNamespace(
                answer='{"items":[{"id":1},{"id":2}]}',
                web_context_statuses=(),
            )
        return SimpleNamespace(answer="ok", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls[1].prompt == 'items=[{"id":1},{"id":2}]'


@pytest.mark.parametrize(
    "reference",
    [
        "${steps.current.output.value}",
        "${steps.future.output.value}",
    ],
)
def test_step_output_variable_must_reference_previous_step(
    tmp_path: Path,
    reference: str,
) -> None:
    (tmp_path / "prompt.md").write_text("value={{var:value}}", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        f"""
version = 1

[[steps]]
id = "current"
config = "config.toml"
prompt_file = "prompt.md"

[steps.vars]
value = "{reference}"

[[steps]]
id = "future"
config = "config.toml"
prompt_file = "prompt.md"

[steps.vars]
value = "static"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="vorherigen Schritt"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)


def test_step_output_variable_rejects_missing_json_field(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "first.md").write_text("first", encoding="utf-8")
    (tmp_path / "second.md").write_text("value={{var:value}}", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "first"
config = "config.toml"
prompt_file = "first.md"
response_format = "json"

[[steps]]
id = "second"
config = "config.toml"
prompt_file = "second.md"

[steps.vars]
value = "${steps.first.output.missing}"
""".strip(),
        encoding="utf-8",
    )

    async def fake_run_once(options, *, dependencies=None):
        if options.prompt == "first":
            return SimpleNamespace(
                answer='{"summary":"done"}',
                web_context_statuses=(),
            )
        raise AssertionError("second step must fail before model execution")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="enthält kein Feld 'missing'"):
        asyncio.run(run_flow(definition, workspace=tmp_path))


def test_foreach_publishes_aggregated_json_output_for_later_foreach(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "analyze.md").write_text("analyze {{var:id}}", encoding="utf-8")
    (tmp_path / "again.md").write_text(
        "again {{var:id}}={{var:value}}",
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
id = "analyze"
config = "config.toml"
prompt_file = "analyze.md"
foreach = "steps.discover.output.items"
iteration_id = "${item.id}"
response_format = "json"

[steps.vars]
id = "${item.id}"

[[steps]]
id = "again"
config = "config.toml"
prompt_file = "again.md"
foreach = "steps.analyze.output.iterations"
iteration_id = "${item.id}"

[steps.vars]
id = "${item.id}"
value = "${item.output.value}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[{"id":"one"},{"id":"two"}]}',
                web_context_statuses=(),
            )
        if options.prompt == "analyze one":
            return SimpleNamespace(
                answer='{"value":1}',
                web_context_statuses=(),
            )
        if options.prompt == "analyze two":
            return SimpleNamespace(
                answer='{"value":2}',
                web_context_statuses=(),
            )
        return SimpleNamespace(answer="done", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    validate_flow(definition, workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == [
        "discover",
        "analyze one",
        "analyze two",
        "again one=1",
        "again two=2",
    ]


def test_foreach_publishes_text_outputs_as_strings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "analyze.md").write_text("analyze {{var:id}}", encoding="utf-8")
    (tmp_path / "again.md").write_text("again {{var:value}}", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
config = "config.toml"
prompt_file = "discover.md"

[[steps]]
id = "analyze"
config = "config.toml"
prompt_file = "analyze.md"
foreach = "steps.discover.output.items"

[steps.vars]
id = "${item.id}"

[[steps]]
id = "again"
config = "config.toml"
prompt_file = "again.md"
foreach = "steps.analyze.output.iterations"

[steps.vars]
value = "${item.output}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[{"id":"one"}]}',
                web_context_statuses=(),
            )
        if options.prompt == "analyze one":
            return SimpleNamespace(
                answer="plain text",
                web_context_statuses=(),
            )
        return SimpleNamespace(answer="done", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == [
        "discover",
        "analyze one",
        "again plain text",
    ]


def test_foreach_aggregation_uses_positional_ids_without_explicit_iteration_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process", encoding="utf-8")
    (tmp_path / "again.md").write_text("id={{var:id}}", encoding="utf-8")
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

[[steps]]
id = "again"
config = "config.toml"
prompt_file = "again.md"
foreach = "steps.process.output.iterations"

[steps.vars]
id = "${item.id}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":["a","b"]}',
                web_context_statuses=(),
            )
        return SimpleNamespace(answer="ok", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == [
        "discover",
        "process",
        "process",
        "id=1",
        "id=2",
    ]


def test_foreach_aggregation_includes_resumed_checkpoint_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "plan.md").write_text("plan", encoding="utf-8")
    (tmp_path / "process.md").write_text("process {{var:id}}", encoding="utf-8")
    (tmp_path / "again.md").write_text("again {{var:status}}", encoding="utf-8")
    (tmp_path / "status").mkdir()
    (tmp_path / "plan.json").write_text(
        '{"items":[{"id":"one"},{"id":"two"}]}',
        encoding="utf-8",
    )
    (tmp_path / "status" / "one.json").write_text(
        '{"status":"existing"}',
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "plan"
config = "config.toml"
prompt_file = "plan.md"
response_format = "json"
output = "plan.json"
overwrite_output = false

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.plan.output.items"
iteration_id = "${item.id}"
response_format = "json"
output = "status/${iteration.id}.json"
overwrite_output = false

[steps.vars]
id = "${item.id}"

[[steps]]
id = "again"
config = "config.toml"
prompt_file = "again.md"
foreach = "steps.process.output.iterations"

[steps.vars]
status = "${item.output.status}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "process two":
            return SimpleNamespace(
                answer='{"status":"new"}',
                web_context_statuses=(),
            )
        return SimpleNamespace(answer="done", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == [
        "process two",
        "again existing",
        "again new",
    ]


@pytest.mark.parametrize(
    ("number", "expected"),
    [
        ("1e400", "1e400"),
        ("1e-400", "1e-400"),
        ("1.2300e+5", "1.2300e+5"),
    ],
)
def test_foreach_aggregation_preserves_json_number_lexemes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    number: str,
    expected: str,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "analyze.md").write_text("analyze", encoding="utf-8")
    (tmp_path / "again.md").write_text("value={{var:value}}", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
config = "config.toml"
prompt_file = "discover.md"

[[steps]]
id = "analyze"
config = "config.toml"
prompt_file = "analyze.md"
foreach = "steps.discover.output.items"
response_format = "json"

[[steps]]
id = "again"
config = "config.toml"
prompt_file = "again.md"
foreach = "steps.analyze.output.iterations"

[steps.vars]
value = "${item.output.value}"
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
        if options.prompt == "analyze":
            return SimpleNamespace(
                answer=f'{{"value":{number}}}',
                web_context_statuses=(),
            )
        return SimpleNamespace(answer="done", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls[-1].prompt == f"value={expected}"


def test_parse_structured_output_rejects_nonstandard_constants_with_lossless_numbers() -> None:
    with pytest.raises(ValueError, match="nicht standardkonstante JSON-Zahl"):
        flow_module._parse_structured_output(
            '{"x":Infinity}',
            step_id="test",
        )


def test_foreach_aggregation_escapes_lone_surrogate_string_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "analyze.md").write_text("analyze", encoding="utf-8")
    (tmp_path / "again.md").write_text("value={{var:value}}", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
config = "config.toml"
prompt_file = "discover.md"

[[steps]]
id = "analyze"
config = "config.toml"
prompt_file = "analyze.md"
foreach = "steps.discover.output.items"
response_format = "json"

[[steps]]
id = "again"
config = "config.toml"
prompt_file = "again.md"
foreach = "steps.analyze.output.iterations"

[steps.vars]
value = "${item.output.value}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            return SimpleNamespace(answer='{"items":[1]}', web_context_statuses=())
        if options.prompt == "analyze":
            return SimpleNamespace(answer='{"value":"\\ud800"}', web_context_statuses=())
        return SimpleNamespace(answer="done", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls[-1].prompt == "value=\ud800"


def test_json_dump_string_escapes_surrogates_but_keeps_normal_unicode() -> None:
    value = "ä🙂" + chr(0xD800)

    dumped = flow_module._json_dump_string(value)

    assert "ä" in dumped
    assert "🙂" in dumped
    assert "\\ud800" in dumped
    dumped.encode("utf-8")


def test_json_dump_string_escapes_surrogate_object_keys() -> None:
    value = {"key" + chr(0xDFFF): "ok"}

    dumped = flow_module._json_dumps_preserving_numbers(value)

    assert "\\udfff" in dumped
    dumped.encode("utf-8")


def test_foreach_aggregate_limit_is_enforced_while_collecting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process {{var:id}}", encoding="utf-8")
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

[steps.vars]
id = "${item.id}"
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(flow_module, "MAX_STRUCTURED_OUTPUT_BYTES", 90)
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[{"id":"one"},{"id":"two"},{"id":"three"}]}',
                web_context_statuses=(),
            )
        return SimpleNamespace(
            answer="x" * 40,
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="Aggregierter Output.*JSON-Limit"):
        asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == [
        "discover",
        "process one",
        "process two",
    ]


def test_foreach_aggregate_limit_counts_resumed_checkpoint_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "plan.md").write_text("plan", encoding="utf-8")
    (tmp_path / "process.md").write_text("process {{var:id}}", encoding="utf-8")
    (tmp_path / "status").mkdir()
    (tmp_path / "plan.json").write_text(
        '{"items":[{"id":"one"},{"id":"two"}]}',
        encoding="utf-8",
    )
    (tmp_path / "status" / "one.json").write_text(
        '{"value":"' + ("x" * 50) + '"}',
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "plan"
config = "config.toml"
prompt_file = "plan.md"
response_format = "json"
output = "plan.json"
overwrite_output = false

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.plan.output.items"
iteration_id = "${item.id}"
response_format = "json"
output = "status/${iteration.id}.json"
overwrite_output = false

[steps.vars]
id = "${item.id}"
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(flow_module, "MAX_STRUCTURED_OUTPUT_BYTES", 80)
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer='{"value":"new"}', web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="Aggregierter Output.*JSON-Limit"):
        asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls == []



def test_flow_static_conversation_items_run_in_one_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text(
        "dir={{var:directory}}",
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "update"
config = "config.toml"
prompt_file = "prompt.md"
conversation_items = ["operations", "application", "rules"]
response_format = "json"

[steps.vars]
directory = "${conversation.item}"
""".strip(),
        encoding="utf-8",
    )

    conversations = []

    async def fail_run_once(*_args, **_kwargs):
        raise AssertionError("conversation step must not use run_once")

    async def fake_run_conversation(options, *, dependencies=None):
        conversations.append(options)
        return SimpleNamespace(
            answer='{"processed":["operations","application","rules"]}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fail_run_once)
    monkeypatch.setattr(
        flow_module,
        "run_conversation",
        fake_run_conversation,
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert len(conversations) == 1
    assert conversations[0].prompts == (
        "dir=operations",
        "dir=application",
        "dir=rules",
    )
    assert conversations[0].response_format == "json"


def test_flow_static_conversation_items_serialize_toml_floats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text(
        "item={{var:item}}",
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "update"
config = "config.toml"
prompt_file = "prompt.md"
conversation_items = [{ score = 1.5 }]

[steps.vars]
item = "${conversation.item}"
""".strip(),
        encoding="utf-8",
    )

    conversations = []

    async def fake_run_conversation(options, *, dependencies=None):
        conversations.append(options)
        return SimpleNamespace(answer="done", web_context_statuses=())

    monkeypatch.setattr(
        flow_module,
        "run_conversation",
        fake_run_conversation,
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert conversations[0].prompts == ('item={"score":1.5}',)


def test_flow_conversation_items_from_foreach_item(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text(
        "concept={{var:concept}} dir={{var:directory}}",
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
iteration_id = "${item.id}"
conversation_items = "${item.dirs}"
response_format = "json"

[steps.vars]
concept = "${item.id}"
directory = "${conversation.item}"
""".strip(),
        encoding="utf-8",
    )

    conversations = []

    async def fake_run_once(options, *, dependencies=None):
        assert options.prompt == "discover"
        return SimpleNamespace(
            answer=(
                '{"items":['
                '{"id":"auth","dirs":["operations","rules"]},'
                '{"id":"logging","dirs":["application"]}'
                ']}'
            ),
            web_context_statuses=(),
        )

    async def fake_run_conversation(options, *, dependencies=None):
        conversations.append(options)
        return SimpleNamespace(
            answer='{"status":"success"}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    monkeypatch.setattr(
        flow_module,
        "run_conversation",
        fake_run_conversation,
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompts for call in conversations] == [
        (
            "concept=auth dir=operations",
            "concept=auth dir=rules",
        ),
        ("concept=logging dir=application",),
    ]
    assert [call.dump_file_prefix for call in conversations] == [
        "process.auth",
        "process.logging",
    ]


def test_flow_conversation_items_from_step_output_with_optional_final_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "plan.md").write_text("plan", encoding="utf-8")
    (tmp_path / "turn.md").write_text(
        "dir={{var:directory}}",
        encoding="utf-8",
    )
    (tmp_path / "final.md").write_text(
        "final {{var:concept}}",
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "plan"
config = "config.toml"
prompt_file = "plan.md"

[[steps]]
id = "update"
config = "config.toml"
prompt_file = "turn.md"
conversation_items = "steps.plan.output.dirs"
conversation_final_prompt_file = "final.md"
response_format = "json"

[steps.vars]
directory = "${conversation.item}"
concept = "cross-links"
""".strip(),
        encoding="utf-8",
    )

    conversations = []

    async def fake_run_once(options, *, dependencies=None):
        return SimpleNamespace(
            answer='{"dirs":["operations","application"]}',
            web_context_statuses=(),
        )

    async def fake_run_conversation(options, *, dependencies=None):
        conversations.append(options)
        return SimpleNamespace(
            answer='{"status":"success"}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    monkeypatch.setattr(
        flow_module,
        "run_conversation",
        fake_run_conversation,
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    validate_flow(definition, workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert conversations[0].prompts == (
        "dir=operations",
        "dir=application",
        "final cross-links",
    )


def test_flow_empty_conversation_items_require_final_prompt_at_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "plan.md").write_text("plan", encoding="utf-8")
    (tmp_path / "turn.md").write_text(
        "dir={{var:directory}}",
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "plan"
config = "config.toml"
prompt_file = "plan.md"

[[steps]]
id = "update"
config = "config.toml"
prompt_file = "turn.md"
conversation_items = "steps.plan.output.dirs"

[steps.vars]
directory = "${conversation.item}"
""".strip(),
        encoding="utf-8",
    )

    async def fake_run_once(options, *, dependencies=None):
        return SimpleNamespace(
            answer='{"dirs":[]}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="ist leer"):
        asyncio.run(run_flow(definition, workspace=tmp_path))


def test_flow_empty_conversation_items_can_run_final_prompt_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "turn.md").write_text(
        "dir={{var:directory}}",
        encoding="utf-8",
    )
    (tmp_path / "final.md").write_text("finish", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "update"
config = "config.toml"
prompt_file = "turn.md"
conversation_items = []
conversation_final_prompt_file = "final.md"
""".strip(),
        encoding="utf-8",
    )

    conversations = []

    async def fake_run_conversation(options, *, dependencies=None):
        conversations.append(options)
        return SimpleNamespace(answer="done", web_context_statuses=())

    monkeypatch.setattr(
        flow_module,
        "run_conversation",
        fake_run_conversation,
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    validate_flow(definition, workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert conversations[0].prompts == ("finish",)


def test_flow_rejects_conversation_placeholder_without_conversation_items(
    tmp_path: Path,
) -> None:
    (tmp_path / "prompt.md").write_text(
        "dir={{var:directory}}",
        encoding="utf-8",
    )
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "update"
prompt_file = "prompt.md"

[steps.vars]
directory = "${conversation.item}"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="ohne conversation_items"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)



def test_overwrite_json_step_can_use_previous_output_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text(
        "old={{var:old}} count={{var:count}}",
        encoding="utf-8",
    )
    (tmp_path / "result.json").write_text(
        '{"count":2,"items":["a"]}',
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "refine"
config = "config.toml"
prompt_file = "prompt.md"
response_format = "json"
output = "result.json"
overwrite_output = true

[steps.vars]
old = "${previous_output}"
count = "${previous_output.count}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        answer = '{"count":3}'
        assert options.output is not None
        options.output.write_text(answer)
        return SimpleNamespace(answer=answer, web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == [
        'old={"count":2,"items":["a"]} count=2'
    ]
    assert (tmp_path / "result.json").read_text(encoding="utf-8") == '{"count":3}'


def test_previous_output_renders_null_when_output_did_not_exist_at_run_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text(
        "old={{var:old}}",
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "create"
config = "config.toml"
prompt_file = "prompt.md"
response_format = "json"
output = "result.json"
overwrite_output = true

[steps.vars]
old = "${previous_output}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer='{"created":true}', web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls[0].prompt == "old=null"


def test_foreach_overwrite_uses_previous_output_per_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "plan.md").write_text("plan", encoding="utf-8")
    (tmp_path / "update.md").write_text(
        "id={{var:id}} old={{var:old}}",
        encoding="utf-8",
    )
    (tmp_path / "status").mkdir()
    (tmp_path / "plan.json").write_text(
        '{"items":[{"id":"one"},{"id":"two"}]}',
        encoding="utf-8",
    )
    (tmp_path / "status" / "one.json").write_text(
        '{"value":"existing"}',
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "plan"
config = "config.toml"
prompt_file = "plan.md"
response_format = "json"
output = "plan.json"
overwrite_output = false

[[steps]]
id = "update"
config = "config.toml"
prompt_file = "update.md"
foreach = "steps.plan.output.items"
iteration_id = "${item.id}"
response_format = "json"
output = "status/${iteration.id}.json"
overwrite_output = true

[steps.vars]
id = "${item.id}"
old = "${previous_output}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer='{"value":"new"}', web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == [
        'id=one old={"value":"existing"}',
        "id=two old=null",
    ]


def test_chained_foreach_iterations_resume_from_run_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "plan.md").write_text("plan", encoding="utf-8")
    (tmp_path / "analyze.md").write_text("analyze {{var:id}}", encoding="utf-8")
    (tmp_path / "again.md").write_text("again {{var:id}}", encoding="utf-8")
    (tmp_path / "analyze").mkdir()
    (tmp_path / "again").mkdir()
    (tmp_path / "plan.json").write_text(
        '{"items":[{"id":"one"},{"id":"two"}]}',
        encoding="utf-8",
    )
    (tmp_path / "analyze" / "one.json").write_text(
        '{"value":1}',
        encoding="utf-8",
    )
    (tmp_path / "analyze" / "two.json").write_text(
        '{"value":2}',
        encoding="utf-8",
    )
    (tmp_path / "again" / "one.json").write_text(
        '{"done":1}',
        encoding="utf-8",
    )
    (tmp_path / "again" / "two.json").write_text(
        '{"done":2}',
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "plan"
config = "config.toml"
prompt_file = "plan.md"
response_format = "json"
output = "plan.json"
overwrite_output = false

[[steps]]
id = "analyze"
config = "config.toml"
prompt_file = "analyze.md"
foreach = "steps.plan.output.items"
iteration_id = "${item.id}"
response_format = "json"
output = "analyze/${iteration.id}.json"
overwrite_output = false

[steps.vars]
id = "${item.id}"

[[steps]]
id = "again"
config = "config.toml"
prompt_file = "again.md"
foreach = "steps.analyze.output.iterations"
iteration_id = "${item.id}"
response_format = "json"
output = "again/${iteration.id}.json"
overwrite_output = false

[steps.vars]
id = "${item.id}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        raise AssertionError("all steps should resume from run-start checkpoints")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls == []


def test_incomplete_foreach_checkpoint_chain_does_not_promote_downstream_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "plan.md").write_text("plan", encoding="utf-8")
    (tmp_path / "analyze.md").write_text("analyze {{var:id}}", encoding="utf-8")
    (tmp_path / "again.md").write_text("again {{var:id}}", encoding="utf-8")
    (tmp_path / "analyze").mkdir()
    (tmp_path / "again").mkdir()
    (tmp_path / "plan.json").write_text(
        '{"items":[{"id":"one"},{"id":"two"}]}',
        encoding="utf-8",
    )
    (tmp_path / "analyze" / "one.json").write_text(
        '{"value":1}',
        encoding="utf-8",
    )
    (tmp_path / "again" / "one.json").write_text(
        '{"done":1}',
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "plan"
config = "config.toml"
prompt_file = "plan.md"
response_format = "json"
output = "plan.json"
overwrite_output = false

[[steps]]
id = "analyze"
config = "config.toml"
prompt_file = "analyze.md"
foreach = "steps.plan.output.items"
iteration_id = "${item.id}"
response_format = "json"
output = "analyze/${iteration.id}.json"
overwrite_output = false

[steps.vars]
id = "${item.id}"

[[steps]]
id = "again"
config = "config.toml"
prompt_file = "again.md"
foreach = "steps.analyze.output.iterations"
iteration_id = "${item.id}"
response_format = "json"
output = "again/${iteration.id}.json"
overwrite_output = false

[steps.vars]
id = "${item.id}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "analyze two":
            return SimpleNamespace(answer='{"value":2}', web_context_statuses=())
        raise AssertionError("downstream existing file must not become a checkpoint")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="existiert bereits"):
        asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == ["analyze two"]


def test_previous_output_requires_json_overwrite_output_and_output_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "prompt.md").write_text("old={{var:old}}", encoding="utf-8")
    _write_config(tmp_path / "config.toml")

    cases = (
        ('response_format = "text"\noutput = "x.json"\noverwrite_output = true',),
        ('response_format = "json"\noutput = "x.json"\noverwrite_output = false',),
        ('response_format = "json"\noverwrite_output = true',),
    )
    for (configuration,) in cases:
        (tmp_path / "flow.toml").write_text(
            f"""
version = 1

[[steps]]
id = "bad"
config = "config.toml"
prompt_file = "prompt.md"
{configuration}

[steps.vars]
old = "${{previous_output}}"
""".strip(),
            encoding="utf-8",
        )
        with pytest.raises(ValueError, match="previous_output"):
            load_flow(tmp_path / "flow.toml", workspace=tmp_path)



def test_overwrite_step_exposes_existing_previous_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text(
        "previous={{var:previous}} status={{var:status}}",
        encoding="utf-8",
    )
    (tmp_path / "state.json").write_text(
        '{"count":2,"nested":{"status":"old"}}',
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "update"
config = "config.toml"
prompt_file = "prompt.md"
response_format = "json"
output = "state.json"
overwrite_output = true

[steps.vars]
previous = "${previous_output}"
status = "${previous_output.nested.status}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(
            answer='{"count":3}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == [
        'previous={"count":2,"nested":{"status":"old"}} status=old'
    ]


def test_previous_output_is_null_when_output_did_not_exist_at_run_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text(
        "previous={{var:previous}}",
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "update"
config = "config.toml"
prompt_file = "prompt.md"
response_format = "json"
output = "state.json"
overwrite_output = true

[steps.vars]
previous = "${previous_output}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(
            answer='{"created":true}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert calls[0].prompt == "previous=null"


def test_previous_output_ignores_file_created_later_in_same_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "create.md").write_text("create", encoding="utf-8")
    (tmp_path / "update.md").write_text(
        "previous={{var:previous}}",
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "create"
config = "config.toml"
prompt_file = "create.md"

[[steps]]
id = "update"
config = "config.toml"
prompt_file = "update.md"
response_format = "json"
output = "state.json"
overwrite_output = true

[steps.vars]
previous = "${previous_output}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "create":
            (tmp_path / "state.json").write_text(
                '{"created":"during-run"}',
                encoding="utf-8",
            )
            return SimpleNamespace(
                answer="created",
                web_context_statuses=(),
            )
        return SimpleNamespace(
            answer='{"updated":true}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == [
        "create",
        "previous=null",
    ]


def test_foreach_previous_output_is_scoped_per_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "plan.md").write_text("plan", encoding="utf-8")
    (tmp_path / "update.md").write_text(
        "id={{var:id}} previous={{var:previous}}",
        encoding="utf-8",
    )
    (tmp_path / "status").mkdir()
    (tmp_path / "plan.json").write_text(
        '{"items":[{"id":"one"},{"id":"two"}]}',
        encoding="utf-8",
    )
    (tmp_path / "status" / "one.json").write_text(
        '{"count":1}',
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "plan"
config = "config.toml"
prompt_file = "plan.md"
response_format = "json"
output = "plan.json"
overwrite_output = false

[[steps]]
id = "update"
config = "config.toml"
prompt_file = "update.md"
foreach = "steps.plan.output.items"
iteration_id = "${item.id}"
response_format = "json"
output = "status/${iteration.id}.json"
overwrite_output = true

[steps.vars]
id = "${item.id}"
previous = "${previous_output}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(
            answer='{"count":2}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == [
        'id=one previous={"count":1}',
        "id=two previous=null",
    ]


def test_conversation_turns_share_same_previous_output_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text(
        "dir={{var:directory}} previous={{var:previous}}",
        encoding="utf-8",
    )
    (tmp_path / "state.json").write_text(
        '{"count":4}',
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "update"
config = "config.toml"
prompt_file = "prompt.md"
conversation_items = ["operations", "rules"]
response_format = "json"
output = "state.json"
overwrite_output = true

[steps.vars]
directory = "${conversation.item}"
previous = "${previous_output}"
""".strip(),
        encoding="utf-8",
    )

    conversations = []

    async def fake_run_conversation(options, *, dependencies=None):
        conversations.append(options)
        return SimpleNamespace(
            answer='{"count":6}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(
        flow_module,
        "run_conversation",
        fake_run_conversation,
    )

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert conversations[0].prompts == (
        'dir=operations previous={"count":4}',
        'dir=rules previous={"count":4}',
    )


@pytest.mark.parametrize(
    "step_config",
    [
        'response_format = "text"\noutput = "state.json"\noverwrite_output = true',
        'response_format = "json"\noverwrite_output = true',
        'response_format = "json"\noutput = "state.json"\noverwrite_output = false',
    ],
)
def test_previous_output_requires_json_overwrite_output(
    tmp_path: Path,
    step_config: str,
) -> None:
    (tmp_path / "prompt.md").write_text(
        "previous={{var:previous}}",
        encoding="utf-8",
    )
    (tmp_path / "flow.toml").write_text(
        f"""
version = 1

[[steps]]
id = "update"
prompt_file = "prompt.md"
{step_config}

[steps.vars]
previous = "${{previous_output}}"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="previous_output"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)


def test_chained_foreach_resume_uses_aggregated_iterations_at_run_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "plan.md").write_text("plan", encoding="utf-8")
    (tmp_path / "analyze.md").write_text(
        "analyze {{var:id}}",
        encoding="utf-8",
    )
    (tmp_path / "again.md").write_text(
        "again {{var:id}}={{var:value}}",
        encoding="utf-8",
    )
    (tmp_path / "analyze").mkdir()
    (tmp_path / "again").mkdir()
    (tmp_path / "plan.json").write_text(
        '{"items":[{"id":"one"},{"id":"two"}]}',
        encoding="utf-8",
    )
    (tmp_path / "analyze" / "one.json").write_text(
        '{"value":1}',
        encoding="utf-8",
    )
    (tmp_path / "analyze" / "two.json").write_text(
        '{"value":2}',
        encoding="utf-8",
    )
    (tmp_path / "again" / "one.json").write_text(
        '{"status":"existing"}',
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "plan"
config = "config.toml"
prompt_file = "plan.md"
response_format = "json"
output = "plan.json"
overwrite_output = false

[[steps]]
id = "analyze"
config = "config.toml"
prompt_file = "analyze.md"
foreach = "steps.plan.output.items"
iteration_id = "${item.id}"
response_format = "json"
output = "analyze/${iteration.id}.json"
overwrite_output = false

[steps.vars]
id = "${item.id}"

[[steps]]
id = "again"
config = "config.toml"
prompt_file = "again.md"
foreach = "steps.analyze.output.iterations"
iteration_id = "${item.id}"
response_format = "json"
output = "again/${iteration.id}.json"
overwrite_output = false

[steps.vars]
id = "${item.id}"
value = "${item.output.value}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        assert options.prompt == "again two=2"
        return SimpleNamespace(
            answer='{"status":"new"}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == ["again two=2"]


def test_previous_output_foreach_requires_run_start_resolvable_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "update.md").write_text(
        "previous={{var:previous}}",
        encoding="utf-8",
    )
    (tmp_path / "status").mkdir()
    (tmp_path / "status" / "one.json").write_text(
        '{"old":true}',
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
id = "update"
config = "config.toml"
prompt_file = "update.md"
foreach = "steps.discover.output.items"
iteration_id = "${item.id}"
response_format = "json"
output = "status/${iteration.id}.json"
overwrite_output = true

[steps.vars]
previous = "${previous_output}"
""".strip(),
        encoding="utf-8",
    )

    async def fake_run_once(options, *, dependencies=None):
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[{"id":"one"}]}',
                web_context_statuses=(),
            )
        raise AssertionError("update must fail before model execution")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="Run-Start"):
        asyncio.run(run_flow(definition, workspace=tmp_path))



def test_previous_output_rejects_change_after_run_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "mutate.md").write_text("mutate", encoding="utf-8")
    (tmp_path / "update.md").write_text(
        "previous={{var:previous}}",
        encoding="utf-8",
    )
    (tmp_path / "state.json").write_text(
        '{"value":"old"}',
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "mutate"
config = "config.toml"
prompt_file = "mutate.md"

[[steps]]
id = "update"
config = "config.toml"
prompt_file = "update.md"
response_format = "json"
output = "state.json"
overwrite_output = true

[steps.vars]
previous = "${previous_output}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "mutate":
            (tmp_path / "state.json").write_text(
                '{"value":"changed"}',
                encoding="utf-8",
            )
            return SimpleNamespace(
                answer="done",
                web_context_statuses=(),
            )
        raise AssertionError("update must fail before model execution")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="nach dem Run-Start verändert"):
        asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == ["mutate"]


@pytest.mark.parametrize(
    "command",
    [
        "tokens",
        "add_web_context https://docs.example/reference",
        "clear_web_context",
        "enable os",
        "disable os",
    ],
)
def test_flow_rejects_rendered_local_agent_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("{{var:task}}", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
prompt_file = "discover.md"
response_format = "json"

[[steps]]
id = "process"
prompt_file = "process.md"

[steps.vars]
task = "${steps.discover.output.task}"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer=json.dumps({"task": command}),
                web_context_statuses=(),
            )
        return SimpleNamespace(answer="unexpected", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="darf kein lokaler Agent-Befehl sein"):
        asyncio.run(run_flow(definition, workspace=tmp_path))

    assert len(calls) == 1
    assert calls[0].prompt == "discover"


def test_flow_allows_prompt_that_only_mentions_local_agent_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text(
        "Erkläre den Befehl add_web_context https://docs.example/reference",
        encoding="utf-8",
    )
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
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

    assert len(calls) == 1
    assert calls[0].prompt.startswith("Erkläre den Befehl add_web_context")
