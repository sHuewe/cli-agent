from __future__ import annotations

import os

from .config import ModelConfig
from .model import ModelClient
from .ollama import OllamaClient
from .openai_client import OpenAIClient


def create_model_client(
    config: ModelConfig,
) -> ModelClient:
    if config.provider == "ollama":
        return OllamaClient(
            base_url=config.base_url,
            model=config.model,
            timeout=config.timeout,
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
        )

    raise ValueError(
        f"Unbekannter Modell-Provider: "
        f"{config.provider!r}"
    )
