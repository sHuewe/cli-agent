"""Fluent, instance-local SDK configuration. No global policy is consulted by default."""
from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from cli_agent.settings.admin_config import (
    AdminConfig, McpPolicy, WebPolicy, load_admin_config,
    _trusted_server, _model_credential_rule, _web_provider,
    ModelCredentialRule, TrustedMcpServer, WebProviderConfig,
)
from cli_agent.settings.config import (
    AppConfig, ModelConfig, McpServerConfig, load_config, default_config_file,
    _model_config, _mcp_server_config, _agent_config,
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
    for index, rule in enumerate(result.model_credentials):
        definition = asdict(rule)
        # TOML provides lists; frozen dataclass fields intentionally use tuples.
        definition["allowed_api_key_envs"] = list(rule.allowed_api_key_envs)
        if _model_credential_rule(definition, index) != rule:
            raise ValueError("Ungültige model_credentials-Konfiguration.")
        if rule.host not in result.network.model_allowed_hosts:
            raise ValueError("Credential-Host muss in model_allowed_hosts enthalten sein.")
    for index, provider in enumerate(result.web.providers):
        if _web_provider({"type": provider.provider_type, "base_url": provider.base_url,
                          "token_env": provider.token_env}, index) != provider:
            raise ValueError("Ungültige web_providers-Konfiguration.")
        from urllib.parse import urlsplit
        if urlsplit(provider.base_url).hostname not in result.network.web_allowed_hosts:
            raise ValueError("Web-Provider-Host muss in web_allowed_hosts enthalten sein.")
    for index, server in enumerate(result.mcp.trusted_servers):
        definition = asdict(server)
        definition["auto_approve_tools"] = [
            asdict(approval) for approval in server.auto_approve_tools
        ]
        if server.bearer_token_env is not None:
            definition["from_env"] = {
                "authentication": {"bearer": server.bearer_token_env}
            }
        definition["args"] = list(server.args)
        definition["env"] = dict(server.env)
        definition["headers"] = dict(server.headers)
        if _trusted_server(definition, index) != server:
            raise ValueError("Ungültige trusted_servers-Konfiguration.")
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
        self._event_callback = None
        self._python_tools = []

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
        # No arguments load the default project file, so its directory
        # remains the base for relative OKF/knowledge paths. Overrides
        # without a path instead create a genuinely configless SDK project.
        self._project_path = (
            Path(path) if path is not None
            else (default_config_file() if not overrides else None)
        )
        config = (load_config(Path(path)) if path is not None
                  else (load_config() if not overrides else AppConfig()))
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
        if model_opts:
            if _model_config(asdict(config.model)) != config.model:
                raise ValueError("Ungültige Modell-Konfiguration.")
        if "timezone" in overrides:
            _agent_config({"timezone": config.agent.timezone})
        if "mcp_servers" in overrides:
            for server in config.mcp_servers:
                if server.built_in:
                    raise ValueError("built_in MCPs dürfen nicht als Projekt-Server übergeben werden.")
                definition = (
                    {"name": server.name}
                    if server.transport == "stdio"
                    else {
                        "name": server.name,
                        "transport": server.transport,
                        "url": server.url,
                        "headers": server.headers,
                        "config": server.config,
                        "compress_result": server.compress_result,
                        "compress_min_chars": server.compress_min_chars,
                    }
                )
                if _mcp_server_config(definition) != server:
                    raise ValueError("Ungültige MCP-Projektkonfiguration.")
        self._project = config
        run_options = {k: v for k, v in overrides.items() if k in _RUN_OPTIONS}
        # Callback configuration is independent of project configuration.
        # Keep the existing approval callback across project updates, unless
        # a caller explicitly replaces or clears it in this call.
        if "approval_callback" not in run_options and "approval_callback" in self._run_options:
            run_options["approval_callback"] = self._run_options["approval_callback"]
        self._run_options = run_options
        return self

    def with_event_callback(self, callback: Any) -> AgentBuilder:
        """Receive safe, structured AgentEvent objects during execution."""
        if not callable(callback):
            raise TypeError("event callback must be callable")
        self._event_callback = callback
        return self

    def with_default_event_callback(self, *, verbose: bool = False) -> AgentBuilder:
        """Opt in to safe console progress output."""
        from functools import partial
        from .events import default_event_callback
        self._event_callback = partial(default_event_callback, verbose=verbose)
        return self

    def with_approval_callback(self, callback: Any) -> AgentBuilder:
        """Install a Python approval callback (True, False or 'session')."""
        if not callable(callback):
            raise TypeError("approval callback must be callable")
        self._run_options["approval_callback"] = callback
        return self

    def register_tool(self, function: Any, *, name: str | None = None,
                      description: str | None = None) -> AgentBuilder:
        from .python_tools import make_python_tool
        tool = make_python_tool(function, name=name, description=description)
        if any(existing.name == tool.name for existing in self._python_tools):
            raise ValueError("Duplicate Python tool")
        self._python_tools.append(tool)
        return self

    def build(self) -> "Agent":
        from .agent import Agent
        # An instance is a snapshot; subsequent builder updates do not affect it.
        return Agent(
            project_config=self._project,
            admin_config=self._admin,
            project_path=self._project_path,
            run_options=dict(self._run_options),
            event_callback=self._event_callback,
            python_tools=tuple(self._python_tools),
        )
