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
