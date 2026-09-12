from __future__ import annotations

import pytest

from cli_agent.config import ModelConfig
from cli_agent.model_factory import create_model_client
from cli_agent.network_policy import NetworkConfig
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


def test_model_endpoint_must_be_allowlisted() -> None:
    with pytest.raises(ValueError, match="nicht erlaubt"):
        create_model_client(
            ModelConfig(
                provider="openai",
                model="test-model",
                base_url="https://provider.example/v1",
            )
        )


def test_model_endpoint_can_use_an_explicit_internal_host() -> None:
    client = create_model_client(
        ModelConfig(
            provider="openai",
            model="test-model",
            base_url="https://llm.internal/v1",
        ),
        network=NetworkConfig(model_allowed_hosts=("llm.internal",)),
    )

    assert isinstance(client, OpenAIClient)
