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


def test_global_vars_are_available_and_may_be_unused(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "one.md").write_text(
        "root={{var:knowledgePath}}",
        encoding="utf-8",
    )
    (tmp_path / "two.md").write_text("plain", encoding="utf-8")
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[vars]
knowledgePath = "knowledge_neu2"
unused = "allowed"

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "one.md"

[[steps]]
id = "two"
config = "config.toml"
prompt_file = "two.md"
""".strip(),
        encoding="utf-8",
    )

    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer="ok", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    validate_flow(definition, workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert [call.prompt for call in calls] == [
        "root=knowledge_neu2",
        "plain",
    ]


def test_step_vars_override_global_vars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text(
        "lang={{var:language}}",
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[vars]
language = "de"

[[steps]]
id = "one"
config = "config.toml"
prompt_file = "prompt.md"

[steps.vars]
language = "en"
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

    assert calls[0].prompt == "lang=en"


def test_global_var_is_available_in_conversation_final_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "turn.md").write_text(
        "dir={{var:directory}}",
        encoding="utf-8",
    )
    (tmp_path / "final.md").write_text(
        "root={{var:knowledgePath}}",
        encoding="utf-8",
    )
    _write_config(tmp_path / "config.toml")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[vars]
knowledgePath = "knowledge_neu2"

[[steps]]
id = "links"
config = "config.toml"
prompt_file = "turn.md"
conversation_items = ["operations"]
conversation_final_prompt_file = "final.md"

[steps.vars]
directory = "${conversation.item}"
""".strip(),
        encoding="utf-8",
    )

    conversations = []

    async def fake_run_conversation(options, *, dependencies=None):
        conversations.append(options)
        return SimpleNamespace(answer="ok", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_conversation", fake_run_conversation)

    definition = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    validate_flow(definition, workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert conversations[0].prompts == (
        "dir=operations",
        "root=knowledge_neu2",
    )


@pytest.mark.parametrize(
    "value",
    [
        "${item.id}",
        "${conversation.item}",
        "${previous_output}",
        "${iteration.id}",
        "${steps.first.output}",
    ],
)
def test_global_vars_reject_dynamic_flow_placeholders(
    tmp_path: Path,
    value: str,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        f"""
version = 1

[vars]
value = "{value}"

[[steps]]
id = "one"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="dynamischen Flow-Platzhalter"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)


def test_global_vars_must_be_string_table(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")

    (tmp_path / "flow.toml").write_text(
        """
version = 1
vars = "invalid"

[[steps]]
id = "one"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="vars muss eine Tabelle"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)

    (tmp_path / "flow.toml").write_text(
        """
version = 1

[vars]
count = 1

[[steps]]
id = "one"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="String-zu-String"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)


def test_global_vars_reject_invalid_variable_name(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[vars]
"bad.name" = "value"

[[steps]]
id = "one"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="Ungültiger globaler Variablenname"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)
