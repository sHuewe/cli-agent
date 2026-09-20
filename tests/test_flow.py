from __future__ import annotations

import subprocess
from pathlib import Path

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


def test_flow_uses_fixed_workspace_and_per_step_configs(
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
output = "work/items.json"
overwrite_output = true

[[steps]]
id = "process"
config = "config-b.toml"
prompt_file = "prompts/process.md"
foreach = "steps.discover.output.items"
output = "work/${item.id}.md"
overwrite_output = true

[steps.vars]
id = "${item.id}"
""".strip(),
        encoding="utf-8",
    )

    calls: list[list[str]] = []

    def fake_run(command, *, cwd, check):
        calls.append(list(command))
        if "discover.md" in " ".join(command):
            output = Path(command[command.index("--output") + 1])
            output.write_text(
                '{"items":[{"id":"one"},{"id":"two"}]}',
                encoding="utf-8",
            )
        else:
            output = Path(command[command.index("--output") + 1])
            output.write_text("done", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(flow_module.subprocess, "run", fake_run)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    run_flow(definition, workspace=tmp_path)

    assert len(calls) == 3
    assert all(
        command[command.index("--workspace") + 1] == str(tmp_path.resolve())
        for command in calls
    )
    assert calls[0][calls[0].index("--config") + 1].endswith("config-a.toml")
    assert calls[1][calls[1].index("--config") + 1].endswith("config-b.toml")
    assert "id=one" in calls[1]
    assert "id=two" in calls[2]
    assert (tmp_path / "work" / "one.md").read_text(encoding="utf-8") == "done"
    assert (tmp_path / "work" / "two.md").read_text(encoding="utf-8") == "done"


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


def test_flow_rejects_llm_controlled_output_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process {{var:path}}", encoding="utf-8")
    (tmp_path / "work").mkdir()
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
config = "config.toml"
prompt_file = "prompt.md"
output = "work/items.json"
overwrite_output = true

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

    def fake_run(command, *, cwd, check):
        output = Path(command[command.index("--output") + 1])
        output.write_text('{"items":[{"path":"../../../escape.txt"}]}', encoding="utf-8")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(flow_module.subprocess, "run", fake_run)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="darf '..' nicht enthalten"):
        run_flow(definition, workspace=tmp_path)


def test_foreach_requires_strict_json_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("discover", encoding="utf-8")
    (tmp_path / "process.md").write_text("process", encoding="utf-8")
    (tmp_path / "work").mkdir()
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "discover"
config = "config.toml"
prompt_file = "prompt.md"
output = "work/items.json"
overwrite_output = true

[[steps]]
id = "process"
config = "config.toml"
prompt_file = "process.md"
foreach = "steps.discover.output.items"
""".strip(),
        encoding="utf-8",
    )

    def fake_run(command, *, cwd, check):
        if "--output" in command:
            Path(command[command.index("--output") + 1]).write_text(
                "not json",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(flow_module.subprocess, "run", fake_run)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="gültiges JSON"):
        run_flow(definition, workspace=tmp_path)


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
output = "later.json"
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
