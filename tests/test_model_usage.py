from cli_agent.model import TokenUsage
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
