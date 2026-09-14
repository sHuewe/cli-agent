from __future__ import annotations

import os
from urllib.parse import urlsplit

from .admin_config import ModelCredentialRule
from .config import ModelConfig
from .model import ModelClient
from .network_policy import LOCAL_HOSTS, NetworkConfig, validate_http_url
from .ollama import OllamaClient
from .openai_client import OpenAIClient


def _validate_api_key_env(
    config: ModelConfig,
    credential_rules: tuple[ModelCredentialRule, ...],
) -> None:
    if not config.api_key_env:
        return
    hostname = (urlsplit(config.base_url).hostname or "").lower().rstrip(".")
    if hostname in LOCAL_HOSTS:
        return
    matching = next(
        (
            rule
            for rule in credential_rules
            if rule.provider == config.provider and rule.host == hostname
        ),
        None,
    )
    if matching is None or config.api_key_env not in matching.allowed_api_key_envs:
        raise PermissionError(
            f"Die Credential-Umgebungsvariable {config.api_key_env!r} ist für "
            f"Modell-Host {hostname!r} nicht administrativ freigegeben."
        )


def create_model_client(
    config: ModelConfig,
    *,
    network: NetworkConfig | None = None,
    credential_rules: tuple[ModelCredentialRule, ...] = (),
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
        _validate_api_key_env(config, credential_rules)
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
