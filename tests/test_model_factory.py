from __future__ import annotations

import pytest

from cli_agent.admin_config import ModelCredentialRule
from cli_agent.config import ModelConfig
from cli_agent.model_factory import create_model_client
from cli_agent.network_policy import NetworkConfig
from cli_agent.openai_client import OpenAIClient


def test_openai_client_uses_configured_api_key_env_for_localhost(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "secret")
    client = create_model_client(ModelConfig(provider="openai", model="test-model", base_url="http://localhost:8000/v1", api_key_env="LLM_API_KEY"))
    assert isinstance(client, OpenAIClient)
    assert client.api_key == "secret"


def test_openai_client_uses_dummy_when_api_key_env_is_missing(monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    client = create_model_client(ModelConfig(provider="openai", model="test-model", base_url="http://localhost:8000/v1", api_key_env="LLM_API_KEY"))
    assert client.api_key == "dummy"


def test_openai_client_uses_dummy_without_api_key_env() -> None:
    client = create_model_client(ModelConfig(provider="openai", model="test-model", base_url="http://localhost:8000/v1"))
    assert client.api_key == "dummy"


def test_remote_model_rejects_unapproved_api_key_environment(monkeypatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-leak")
    with pytest.raises(PermissionError, match="nicht administrativ freigegeben"):
        create_model_client(
            ModelConfig(provider="openai", model="test", base_url="https://llm.internal/v1", api_key_env="AWS_SECRET_ACCESS_KEY"),
            network=NetworkConfig(model_allowed_hosts=("llm.internal",)),
        )


def test_remote_model_accepts_host_bound_api_key_environment(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "approved-secret")
    client = create_model_client(
        ModelConfig(provider="openai", model="test", base_url="https://llm.internal/v1", api_key_env="LLM_API_KEY"),
        network=NetworkConfig(model_allowed_hosts=("llm.internal",)),
        credential_rules=(ModelCredentialRule(provider="openai", host="llm.internal", allowed_api_key_envs=("LLM_API_KEY",)),),
    )
    assert client.api_key == "approved-secret"


def test_credential_rule_is_bound_to_host(monkeypatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "secret")
    with pytest.raises(PermissionError):
        create_model_client(
            ModelConfig(provider="openai", model="test", base_url="https://other.internal/v1", api_key_env="LLM_API_KEY"),
            network=NetworkConfig(model_allowed_hosts=("other.internal",)),
            credential_rules=(ModelCredentialRule(provider="openai", host="llm.internal", allowed_api_key_envs=("LLM_API_KEY",)),),
        )


def test_model_endpoint_must_be_allowlisted() -> None:
    with pytest.raises(ValueError, match="nicht erlaubt"):
        create_model_client(ModelConfig(provider="openai", model="test-model", base_url="https://provider.example/v1"))


def test_model_endpoint_can_use_an_explicit_internal_host() -> None:
    client = create_model_client(ModelConfig(provider="openai", model="test-model", base_url="https://llm.internal/v1"), network=NetworkConfig(model_allowed_hosts=("llm.internal",)))
    assert isinstance(client, OpenAIClient)


def test_model_factory_passes_context_length() -> None:
    client = create_model_client(ModelConfig(provider="openai", model="test-model", base_url="http://localhost:8000/v1", context_length=262_144))
    assert client.context_length == 262_144
