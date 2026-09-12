import pytest

from cli_agent.model import CONTEXT_LIMIT_MARGIN, ContextLimitReachedError, TokenUsage
from cli_agent.ollama import OllamaClient
from cli_agent.openai_client import OpenAIClient


def test_openai_token_usage_uses_chat_completion_fields() -> None:
    usage = OpenAIClient._token_usage(
        {
            "usage": {
                "prompt_tokens": 123,
                "completion_tokens": 45,
                "total_tokens": 168,
            }
        }
    )

    assert usage == TokenUsage(
        input_tokens=123,
        output_tokens=45,
        total_tokens=168,
    )


def test_openai_token_usage_accepts_input_output_aliases() -> None:
    usage = OpenAIClient._token_usage(
        {
            "usage": {
                "input_tokens": 200,
                "output_tokens": 30,
            }
        }
    )

    assert usage == TokenUsage(
        input_tokens=200,
        output_tokens=30,
        total_tokens=230,
    )


def test_ollama_token_usage_uses_eval_counts() -> None:
    usage = OllamaClient._token_usage(
        {
            "prompt_eval_count": 321,
            "eval_count": 54,
        }
    )

    assert usage == TokenUsage(
        input_tokens=321,
        output_tokens=54,
        total_tokens=375,
    )


def test_context_guard_triggers_at_configured_margin() -> None:
    client = OpenAIClient(
        base_url="http://localhost:8000/v1",
        model="test",
        api_key=None,
        context_length=10_000,
    )
    client.last_usage = TokenUsage(
        input_tokens=10_000 - CONTEXT_LIMIT_MARGIN,
        output_tokens=20,
        total_tokens=10_000 - CONTEXT_LIMIT_MARGIN + 20,
    )

    with pytest.raises(ContextLimitReachedError, match="Context-Limit"):
        client._check_context_limit()

    assert client._context_limit_reached is True


def test_context_guard_does_not_trigger_below_margin() -> None:
    client = OpenAIClient(
        base_url="http://localhost:8000/v1",
        model="test",
        api_key=None,
        context_length=10_000,
    )
    client.last_usage = TokenUsage(
        input_tokens=10_000 - CONTEXT_LIMIT_MARGIN - 1,
        output_tokens=20,
        total_tokens=10_000 - CONTEXT_LIMIT_MARGIN + 19,
    )

    client._check_context_limit()

    assert client._context_limit_reached is False


def test_context_guard_is_disabled_without_configured_limit() -> None:
    client = OpenAIClient(
        base_url="http://localhost:8000/v1",
        model="test",
        api_key=None,
    )
    client.last_usage = TokenUsage(
        input_tokens=1_000_000,
        output_tokens=20,
        total_tokens=1_000_020,
    )

    client._check_context_limit()

    assert client._context_limit_reached is False
