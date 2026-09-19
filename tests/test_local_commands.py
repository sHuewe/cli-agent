from __future__ import annotations

from cli_agent.local_commands import classify_local_command


def test_exact_local_command_is_parsed() -> None:
    result = classify_local_command("add_web_context https://example.org/docs")

    assert result.is_local is True
    assert result.command == "add_web_context"
    assert result.arguments == ("https://example.org/docs",)
    assert result.error is None


def test_exact_local_command_with_wrong_arity_fails_locally() -> None:
    result = classify_local_command("add_web_context")

    assert result.is_local is True
    assert result.command == "add_web_context"
    assert result.error is not None
    assert "Verwendung: add_web_context <URL>" in result.error


def test_distinctive_command_typo_gets_suggestion() -> None:
    result = classify_local_command("add_web_contex https://example.org/docs")

    assert result.is_local is True
    assert result.command is None
    assert result.error is not None
    assert "add_web_context <URL>" in result.error


def test_transposed_command_typo_gets_suggestion() -> None:
    result = classify_local_command("add_web_conetxt https://example.org/docs")

    assert result.is_local is True
    assert result.error is not None
    assert "add_web_context <URL>" in result.error


def test_clear_web_context_typo_gets_suggestion() -> None:
    result = classify_local_command("clear_web_contex")

    assert result.is_local is True
    assert result.error is not None
    assert "clear_web_context" in result.error


def test_normal_prompt_is_not_treated_as_local_command() -> None:
    result = classify_local_command("Erkläre mir Web Context in Python")

    assert result.is_local is False


def test_short_ordinary_word_is_not_fuzzily_interpreted_as_tokens() -> None:
    result = classify_local_command("token")

    assert result.is_local is False


def test_enable_typo_is_not_fuzzily_interpreted() -> None:
    result = classify_local_command("enabel server")

    assert result.is_local is False
