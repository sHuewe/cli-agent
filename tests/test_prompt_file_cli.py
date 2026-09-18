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
        context_file=None,
        prompt_file=None,
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
    args = cli_module.build_parser().parse_args(["--prompt-file", "review-prompt.md"])
    assert args.prompt_file == Path("review-prompt.md")


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
                context_file=Path("repository.txt"),
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
