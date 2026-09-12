from __future__ import annotations

import os

from .config import ModelConfig
from .model import ModelClient
from .network_policy import NetworkConfig, validate_http_url
from .ollama import OllamaClient
from .openai_client import OpenAIClient


def create_model_client(
    config: ModelConfig,
    *,
    network: NetworkConfig | None = None,
) -> ModelClient:
    network = network or NetworkConfig()
    validate_http_url(
        config.base_url,
        allowed_hosts=network.model_allowed_hosts,
        purpose="Modell",
    )
    if config.provider == "ollama":
        return OllamaClient(
            base_url=config.base_url,
            model=config.model,
            timeout=config.timeout,
            context_length=config.context_length,
            allowed_hosts=network.model_allowed_hosts,
        )

    if config.provider == "openai":
        api_key = "dummy"
        if config.api_key_env:
            api_key = os.getenv(config.api_key_env) or "dummy"

        return OpenAIClient(
            base_url=config.base_url,
            model=config.model,
            api_key=api_key,
            timeout=config.timeout,
            headers=config.headers,
            context_length=config.context_length,
            allowed_hosts=network.model_allowed_hosts,
        )

    raise ValueError(f"Unbekannter Modell-Provider: {config.provider!r}")
