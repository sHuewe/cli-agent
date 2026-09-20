from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent import cli as cli_module
from cli_agent.admin_config import AdminConfig
from cli_agent.config import AppConfig, ModelConfig


def _base_args(tmp_path: Path, **overrides):
    values = dict(
        config=None,
        model=None,
        os_access=None,
        context_files=[],
        prompt_file=None,
        var=[],
        output=None,
        overwrite_output=False,
        approve_tool=[],
        add_web_context=[],
        debug=False,
        workspace=tmp_path,
        prompt=[],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_run_dependencies(monkeypatch, captured):
    config = AppConfig(
        model=ModelConfig(
            provider="openai",
            model="test-model",
            base_url="http://localhost:11434/v1",
        )
    )

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            captured["constructed"] = True

        async def __aenter__(self):
            captured["entered"] = True
            return self

        async def __aexit__(self, *_args):
            return None

        async def ask(self, prompt):
            captured.setdefault("prompts", []).append(prompt)
            if prompt == "tokens":
                return "token-usage"
            return "review-result"

    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "load_admin_config", lambda: AdminConfig())
    monkeypatch.setattr(cli_module, "configure_logging", lambda _config: None)
    monkeypatch.setattr(cli_module, "WebContextCliAgent", FakeAgent)
    monkeypatch.setattr(
        cli_module,
        "create_model_client",
        lambda *_args, **_kwargs: SimpleNamespace(
            model="test-model",
            base_url="http://localhost:11434/v1",
        ),
    )


def test_prompt_file_option_is_parsed() -> None:
    args = cli_module.build_parser().parse_args(
        ["--prompt-file", "review-prompt.md", "--var", "name=Stephan"]
    )
    assert args.prompt_file == Path("review-prompt.md")
    assert args.var == ["name=Stephan"]


def test_prompt_file_runs_one_model_prompt_then_prints_token_usage(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    prompt_file = tmp_path / "review-prompt.md"
    prompt_file.write_text("Perform the complete review.\n", encoding="utf-8")
    captured = {}
    _patch_run_dependencies(monkeypatch, captured)

    asyncio.run(
        cli_module.run(
            _base_args(tmp_path, prompt_file=Path("review-prompt.md"))
        )
    )

    assert captured["entered"] is True
    assert captured["prompts"] == ["Perform the complete review.\n", "tokens"]
    stdout = capsys.readouterr().out
    assert "review-result" in stdout
    assert "token-usage" in stdout


def test_prompt_file_and_positional_prompt_are_rejected_before_agent_start(
    tmp_path,
    monkeypatch,
) -> None:
    prompt_file = tmp_path / "review-prompt.md"
    prompt_file.write_text("review", encoding="utf-8")
    captured = {}
    _patch_run_dependencies(monkeypatch, captured)

    with pytest.raises(ValueError, match="positional Prompt"):
        asyncio.run(
            cli_module.run(
                _base_args(
                    tmp_path,
                    prompt_file=Path("review-prompt.md"),
                    prompt=["other", "prompt"],
                )
            )
        )

    assert "constructed" not in captured


def test_invalid_prompt_file_is_rejected_before_agent_start(tmp_path, monkeypatch) -> None:
    captured = {}
    _patch_run_dependencies(monkeypatch, captured)

    with pytest.raises(ValueError, match="Prompt-Datei"):
        asyncio.run(
            cli_module.run(
                _base_args(tmp_path, prompt_file=Path("missing.md"))
            )
        )

    assert "constructed" not in captured


def test_sensitive_output_is_rejected_before_agent_start(
    tmp_path,
    monkeypatch,
) -> None:
    captured = {}
    _patch_run_dependencies(monkeypatch, captured)

    with pytest.raises(ValueError, match="Output-Datei.*geschützt"):
        asyncio.run(
            cli_module.run(
                _base_args(
                    tmp_path,
                    output=Path(".env"),
                    prompt=["generate", "output"],
                )
            )
        )

    assert "constructed" not in captured


def test_prompt_file_can_be_combined_with_context_and_output(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    (tmp_path / "repository.txt").write_text("repository snapshot", encoding="utf-8")
    (tmp_path / "review-prompt.md").write_text("Review it.", encoding="utf-8")
    captured = {}
    _patch_run_dependencies(monkeypatch, captured)

    asyncio.run(
        cli_module.run(
            _base_args(
                tmp_path,
                context_files=[Path("repository.txt")],
                prompt_file=Path("review-prompt.md"),
                output=Path("review.md"),
            )
        )
    )

    assert captured["prompts"] == ["Review it.", "tokens"]
    assert (tmp_path / "review.md").read_text(encoding="utf-8") == "review-result"
    stdout = capsys.readouterr().out
    assert "review-result" in stdout
    assert "token-usage" in stdout


def test_prompt_template_uses_cli_variables_without_prompting(
    tmp_path,
    monkeypatch,
) -> None:
    prompt_file = tmp_path / "review-prompt.md"
    prompt_file.write_text(
        "Review {{var:project}} for {{var:name}}. Query={{var:query}}",
        encoding="utf-8",
    )
    captured = {}
    _patch_run_dependencies(monkeypatch, captured)
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: (_ for _ in ()).throw(AssertionError("unexpected input")),
    )

    asyncio.run(
        cli_module.run(
            _base_args(
                tmp_path,
                prompt_file=Path("review-prompt.md"),
                var=["project=cli-agent", "name=Stephan", "query=a=b"],
            )
        )
    )

    assert captured["prompts"][0] == "Review cli-agent for Stephan. Query=a=b"


def test_prompt_template_prompts_for_missing_variables_in_occurrence_order(
    tmp_path,
    monkeypatch,
) -> None:
    prompt_file = tmp_path / "review-prompt.md"
    prompt_file.write_text(
        "{{var:first}} / {{var:second}} / {{var:first}}",
        encoding="utf-8",
    )
    captured = {}
    _patch_run_dependencies(monkeypatch, captured)
    monkeypatch.setattr(
        cli_module.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )
    prompts = []
    answers = iter(["ONE", "TWO"])

    def fake_input(prompt):
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr("builtins.input", fake_input)

    asyncio.run(
        cli_module.run(
            _base_args(tmp_path, prompt_file=Path("review-prompt.md"))
        )
    )

    assert prompts == [
        "Wert für Prompt-Variable first: ",
        "Wert für Prompt-Variable second: ",
    ]
    assert captured["prompts"][0] == "ONE / TWO / ONE"


def test_prompt_template_combines_cli_and_interactive_variables(
    tmp_path,
    monkeypatch,
) -> None:
    prompt_file = tmp_path / "review-prompt.md"
    prompt_file.write_text(
        "{{var:name}} / {{var:task}}",
        encoding="utf-8",
    )
    captured = {}
    _patch_run_dependencies(monkeypatch, captured)
    monkeypatch.setattr(
        cli_module.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "Review")

    asyncio.run(
        cli_module.run(
            _base_args(
                tmp_path,
                prompt_file=Path("review-prompt.md"),
                var=["name=Stephan"],
            )
        )
    )

    assert captured["prompts"][0] == "Stephan / Review"


def test_prompt_template_missing_variable_without_tty_fails_before_agent_start(
    tmp_path,
    monkeypatch,
) -> None:
    prompt_file = tmp_path / "review-prompt.md"
    prompt_file.write_text("Hello {{var:name}}", encoding="utf-8")
    captured = {}
    _patch_run_dependencies(monkeypatch, captured)
    monkeypatch.setattr(
        cli_module.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: False),
    )

    with pytest.raises(ValueError, match="stdin.*kein TTY.*name"):
        asyncio.run(
            cli_module.run(
                _base_args(tmp_path, prompt_file=Path("review-prompt.md"))
            )
        )

    assert "constructed" not in captured


def test_prompt_template_rejects_unknown_cli_variable_before_agent_start(
    tmp_path,
    monkeypatch,
) -> None:
    prompt_file = tmp_path / "review-prompt.md"
    prompt_file.write_text("Hello {{var:name}}", encoding="utf-8")
    captured = {}
    _patch_run_dependencies(monkeypatch, captured)

    with pytest.raises(ValueError, match="kommen im Prompt-Template nicht vor"):
        asyncio.run(
            cli_module.run(
                _base_args(
                    tmp_path,
                    prompt_file=Path("review-prompt.md"),
                    var=["typo=value"],
                )
            )
        )

    assert "constructed" not in captured


def test_var_without_prompt_file_is_rejected_before_agent_start(
    tmp_path,
    monkeypatch,
) -> None:
    captured = {}
    _patch_run_dependencies(monkeypatch, captured)

    with pytest.raises(ValueError, match="nur zusammen mit --prompt-file"):
        asyncio.run(
            cli_module.run(
                _base_args(tmp_path, prompt=["hello"], var=["name=Stephan"])
            )
        )

    assert "constructed" not in captured


def test_prompt_template_rejects_blank_rendered_prompt_from_cli_value(
    tmp_path,
    monkeypatch,
) -> None:
    prompt_file = tmp_path / "review-prompt.md"
    prompt_file.write_text("{{var:task}}", encoding="utf-8")
    captured = {}
    _patch_run_dependencies(monkeypatch, captured)

    with pytest.raises(ValueError, match="Gerenderter Prompt darf nicht leer"):
        asyncio.run(
            cli_module.run(
                _base_args(
                    tmp_path,
                    prompt_file=Path("review-prompt.md"),
                    var=["task="],
                )
            )
        )

    assert "constructed" not in captured


def test_prompt_template_rejects_blank_rendered_prompt_from_interactive_value(
    tmp_path,
    monkeypatch,
) -> None:
    prompt_file = tmp_path / "review-prompt.md"
    prompt_file.write_text(" {{var:task}}\t", encoding="utf-8")
    captured = {}
    _patch_run_dependencies(monkeypatch, captured)
    monkeypatch.setattr(
        cli_module.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "   ")

    with pytest.raises(ValueError, match="Gerenderter Prompt darf nicht leer"):
        asyncio.run(
            cli_module.run(
                _base_args(tmp_path, prompt_file=Path("review-prompt.md"))
            )
        )

    assert "constructed" not in captured
