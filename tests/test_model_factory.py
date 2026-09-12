from __future__ import annotations

from cli_agent.config import ModelConfig
from cli_agent.model_factory import create_model_client
from cli_agent.openai_client import OpenAIClient


def test_openai_client_uses_configured_api_key_env(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "secret")

    client = create_model_client(
        ModelConfig(
            provider="openai",
            model="test-model",
            base_url="http://localhost:8000/v1",
            api_key_env="LLM_API_KEY",
        )
    )

    assert isinstance(client, OpenAIClient)
    assert client.api_key == "secret"


def test_openai_client_uses_dummy_when_api_key_env_is_missing(monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    client = create_model_client(
        ModelConfig(
            provider="openai",
            model="test-model",
            base_url="http://localhost:8000/v1",
            api_key_env="LLM_API_KEY",
        )
    )

    assert isinstance(client, OpenAIClient)
    assert client.api_key == "dummy"


def test_openai_client_uses_dummy_without_api_key_env() -> None:
    client = create_model_client(
        ModelConfig(
            provider="openai",
            model="test-model",
            base_url="http://localhost:8000/v1",
        )
    )

    assert isinstance(client, OpenAIClient)
    assert client.api_key == "dummy"


def test_model_factory_passes_context_length() -> None:
    client = create_model_client(
        ModelConfig(
            provider="openai",
            model="test-model",
            base_url="http://localhost:8000/v1",
            context_length=262_144,
        )
    )

    assert isinstance(client, OpenAIClient)
    assert client.context_length == 262_144
