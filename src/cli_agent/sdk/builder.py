"""Fluent, instance-local SDK configuration. No global policy is consulted by default."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from cli_agent.settings.admin_config import (
    AdminConfig, McpPolicy, WebPolicy, load_admin_config,
    ModelCredentialRule, TrustedMcpServer, WebProviderConfig,
)
from cli_agent.settings.config import (
    AppConfig, ModelConfig, McpServerConfig, load_config,
)
from cli_agent.security.network_policy import NetworkConfig, LOCAL_HOSTS


def _host_list(values: Any, key: str) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or any(
        not isinstance(x, str) or not x.strip() or "://" in x or "/" in x
        for x in values
    ):
        raise ValueError(f"{key} benötigt eine Liste von Hostnamen ohne Schema/Pfad.")
    normalized = tuple(x.strip().lower().rstrip(".") for x in values)
    if any(not x for x in normalized) or len(set(normalized)) != len(normalized):
        raise ValueError(f"{key} enthält leere oder doppelte Hostnamen.")
    return normalized


def _admin_overrides(policy: AdminConfig, overrides: dict[str, Any]) -> AdminConfig:
    allowed = {"model_allowed_hosts", "mcp_allowed_hosts", "web_allowed_hosts",
               "model_credentials", "trusted_servers", "web_providers"}
    unknown = set(overrides) - allowed
    if unknown:
        raise TypeError(f"Unbekannte Admin-Optionen: {sorted(unknown)}")
    net = {
        k: _host_list(v, k)
        for k, v in overrides.items() if k.endswith("_allowed_hosts")
    }
    result = replace(policy, network=replace(policy.network, **net))
    if "model_credentials" in overrides:
        credentials = overrides["model_credentials"]
        if not isinstance(credentials, (tuple, list)) or not all(
            isinstance(x, ModelCredentialRule) for x in credentials
        ):
            raise TypeError("model_credentials benötigt ModelCredentialRule-Objekte.")
        result = replace(result, model_credentials=tuple(credentials))
    if "trusted_servers" in overrides:
        servers = overrides["trusted_servers"]
        if not isinstance(servers, (tuple, list)) or not all(
            isinstance(x, TrustedMcpServer) for x in servers
        ):
            raise TypeError("trusted_servers benötigt TrustedMcpServer-Objekte.")
        result = replace(result, mcp=McpPolicy(tuple(servers)))
    if "web_providers" in overrides:
        providers = overrides["web_providers"]
        if not isinstance(providers, (tuple, list)) or not all(
            isinstance(x, WebProviderConfig) for x in providers
        ):
            raise TypeError("web_providers benötigt WebProviderConfig-Objekte.")
        result = replace(result, web=WebPolicy(tuple(providers)))
    for rule in result.model_credentials:
        if rule.host not in result.network.model_allowed_hosts:
            raise ValueError("Credential-Host muss in model_allowed_hosts enthalten sein.")
    for provider in result.web.providers:
        from urllib.parse import urlsplit
        if urlsplit(provider.base_url).hostname not in result.network.web_allowed_hosts:
            raise ValueError("Web-Provider-Host muss in web_allowed_hosts enthalten sein.")
    return result


_PROJECT_OPTIONS = {
    "model", "provider", "base_url", "api_key_env", "timeout", "headers",
    "context_length", "timezone", "mcp_servers", "okf", "knowledge_vector",
    "logging", "dump_llm_context", "max_pdf_pages",
}
_RUN_OPTIONS = {
    "workspace", "workspace_access", "with_data", "with_mcp",
    "response_format", "context_files", "excluded_paths",
    "mutation_protected_paths", "retry_policy", "approval_callback",
    "user_message_callback", "add_web_context", "dump_file_prefix",
}


class AgentBuilder:
    """Build a persistent Agent from independent admin and project settings.

    No with_admin_config: localhost-only AdminConfig() regardless of system file.
    with_admin_config(): explicitly read system policy.
    with_admin_config(path=...): read selected admin file.
    with_admin_config(overrides...): start from strict defaults, no file.
    """

    def __init__(self) -> None:
        self._admin: AdminConfig = AdminConfig()
        self._project: AppConfig = AppConfig()
        self._project_path: Path | None = None
        self._run_options: dict[str, Any] = {}

    def with_admin_config(self, *, path: str | Path | None = None, **overrides: Any) -> AgentBuilder:
        if path is not None:
            policy = load_admin_config(Path(path))
            if not Path(path).expanduser().exists():
                raise FileNotFoundError(path)
        elif not overrides:
            policy = load_admin_config()
        else:
            policy = AdminConfig()
        self._admin = _admin_overrides(policy, overrides)
        return self

    def with_project_config(self, *, path: str | Path | None = None, **overrides: Any) -> AgentBuilder:
        unknown = set(overrides) - _PROJECT_OPTIONS - _RUN_OPTIONS
        if unknown:
            raise TypeError(f"Unbekannte Projekt-Optionen: {sorted(unknown)}")
        self._project_path = Path(path) if path is not None else None
        config = load_config(Path(path)) if path is not None else AppConfig()
        model_fields = set(ModelConfig.__dataclass_fields__)
        model_opts = {k: overrides[k] for k in model_fields if k in overrides}
        if model_opts:
            config = replace(config, model=replace(config.model, **model_opts))
        if "timezone" in overrides:
            config = replace(config, agent=replace(config.agent, timezone=overrides["timezone"]))
        for key in ("mcp_servers", "okf", "knowledge_vector", "logging",
                    "dump_llm_context", "max_pdf_pages"):
            if key in overrides:
                value = overrides[key]
                if key == "mcp_servers":
                    if not isinstance(value, (tuple, list)) or not all(
                        isinstance(item, McpServerConfig) for item in value
                    ):
                        raise TypeError("mcp_servers benötigt McpServerConfig-Objekte.")
                    value = tuple(value)
                config = replace(config, **{key: value})
        self._project = config
        self._run_options = {k: v for k, v in overrides.items() if k in _RUN_OPTIONS}
        return self

    def build(self) -> "Agent":
        from .agent import Agent
        # An instance is a snapshot; subsequent builder updates do not affect it.
        return Agent(
            project_config=self._project,
            admin_config=self._admin,
            project_path=self._project_path,
            run_options=dict(self._run_options),
        )
