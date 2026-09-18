from __future__ import annotations

import platform
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .mcp_contracts import validate_tool_contract_fingerprint
from .network_policy import LOCAL_HOSTS, NetworkConfig


def _normalize_mcp_url(value: str, *, section: str) -> str:
    raw = value.strip()
    parsed = urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"{section}.url muss eine http:// oder https:// URL sein.")
    if not parsed.hostname:
        raise ValueError(f"{section}.url enthält keinen Hostnamen.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"Credentials in {section}.url werden nicht unterstützt.")
    if parsed.fragment:
        raise ValueError(f"{section}.url darf keinen Fragment-Teil enthalten.")

    host = parsed.hostname.lower().rstrip(".")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, host, path, parsed.query, ""))


def _normalize_web_provider_base_url(value: str, *, section: str) -> str:
    raw = value.strip()
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "https":
        raise ValueError(f"{section}.base_url muss eine https:// URL sein.")
    if not parsed.hostname:
        raise ValueError(f"{section}.base_url enthält keinen Hostnamen.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"Credentials in {section}.base_url werden nicht unterstützt.")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{section}.base_url darf weder Query noch Fragment enthalten.")

    host = parsed.hostname.lower().rstrip(".")
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if parsed.port is not None:
        host = f"{host}:{parsed.port}"
    path = (parsed.path or "/").rstrip("/") or "/"
    return urlunsplit(("https", host, path, "", ""))


def _string_map(values: Any, *, section: str, key: str) -> tuple[tuple[str, str], ...]:
    if values is None:
        return ()
    if not isinstance(values, dict):
        raise ValueError(f"{section}.{key} muss eine Tabelle sein.")
    return tuple(sorted((str(name), str(value)) for name, value in values.items()))


def _trusted_stdio_command_is_deterministic(command: str) -> bool:
    if command == "{python}":
        return True
    return Path(command).is_absolute() or PureWindowsPath(command).is_absolute()


@dataclass(frozen=True)
class ModelCredentialRule:
    """Administrator-approved environment-variable names for one model host/provider."""

    provider: str
    host: str
    allowed_api_key_envs: tuple[str, ...] = ()


@dataclass(frozen=True)
class WebProviderConfig:
    """Administrator-defined authenticated provider for one web URL namespace."""

    provider_type: str
    base_url: str
    token_env: str


@dataclass(frozen=True)
class WebPolicy:
    providers: tuple[WebProviderConfig, ...] = ()


@dataclass(frozen=True)
class TrustedMcpToolApproval:
    """Persistent approval for one exact MCP tool contract."""

    name: str
    contract_sha256: str


@dataclass(frozen=True)
class TrustedMcpServer:
    """Administrator-defined MCP identity with optional trusted capabilities."""

    name: str
    transport: str
    url: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    bearer_token_env: str | None = None
    auto_approve_tools: tuple[TrustedMcpToolApproval, ...] = ()
    trust_instructions: bool = False


@dataclass(frozen=True)
class McpPolicy:
    """Administrator-controlled policy for MCP process and tool execution."""

    trusted_servers: tuple[TrustedMcpServer, ...] = ()


@dataclass(frozen=True)
class AdminConfig:
    network: NetworkConfig = NetworkConfig()
    model_credentials: tuple[ModelCredentialRule, ...] = ()
    web: WebPolicy = WebPolicy()
    mcp: McpPolicy = McpPolicy()


def default_admin_config_file() -> Path:
    if platform.system() == "Windows":
        return Path(r"C:\ProgramData\cli-agent\admin_config.toml")
    return Path("/etc/cli-agent/admin_config.toml")


def _bool_value(values: dict[str, Any], key: str, default: bool, *, section: str) -> bool:
    value = values.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{section}.{key} muss true oder false sein.")
    return value


def _string_list(values: dict[str, Any], key: str, default: tuple[str, ...], *, section: str) -> tuple[str, ...]:
    raw_values = values.get(key, list(default))
    if not isinstance(raw_values, list) or not all(isinstance(value, str) and value.strip() for value in raw_values):
        raise ValueError(f"{section}.{key} muss eine Liste nichtleerer Strings sein.")
    normalized = tuple(value.strip() for value in raw_values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{section}.{key} darf keine doppelten Werte enthalten.")
    return normalized


def _host_list(values: dict[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    hosts = _string_list(values, key, default, section="[network]")
    normalized = tuple(host.lower().rstrip(".") for host in hosts)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"[network].{key} darf keine doppelten Hosts enthalten.")
    return normalized


def _model_credential_rule(values: dict[str, Any], index: int) -> ModelCredentialRule:
    section = f"[[model.credentials]] #{index + 1}"
    provider = str(values.get("provider", "")).strip().lower()
    host = str(values.get("host", "")).strip().lower().rstrip(".")
    if provider != "openai":
        raise ValueError(f"{section}.provider muss derzeit 'openai' sein.")
    if not host or "://" in host or "/" in host:
        raise ValueError(f"{section}.host muss ein einzelner Hostname ohne Schema oder Pfad sein.")
    allowed = _string_list(values, "allowed_api_key_envs", (), section=section)
    return ModelCredentialRule(provider=provider, host=host, allowed_api_key_envs=allowed)


def _web_provider(values: dict[str, Any], index: int) -> WebProviderConfig:
    section = f"[[web.providers]] #{index + 1}"
    provider_type = str(values.get("type", "")).strip().lower()
    if provider_type != "confluence":
        raise ValueError(f"{section}.type muss derzeit 'confluence' sein.")

    base_url_value = values.get("base_url")
    if not isinstance(base_url_value, str) or not base_url_value.strip():
        raise ValueError(f"{section}.base_url muss eine nichtleere URL sein.")
    base_url = _normalize_web_provider_base_url(base_url_value, section=section)

    token_env = str(values.get("token_env", "")).strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token_env):
        raise ValueError(
            f"{section}.token_env muss der Name einer Umgebungsvariable sein."
        )
    return WebProviderConfig(
        provider_type=provider_type,
        base_url=base_url,
        token_env=token_env,
    )


def _trusted_tool_approvals(values: dict[str, Any], *, section: str) -> tuple[TrustedMcpToolApproval, ...]:
    raw = values.get("auto_approve_tools", [])
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(
            f"{section}.auto_approve_tools muss über "
            "[[mcp.trusted_servers.auto_approve_tools]]-Tabellen definiert werden."
        )
    if raw and all(isinstance(value, str) for value in raw):
        raise ValueError(
            f"{section}.auto_approve_tools unterstützt keine name-only Freigaben mehr. "
            "Erzeuge einen gepinnten Tool-Contract mit "
            "'cli-agent admin trust-tool <server> <tool> --config <config>'."
        )
    if not all(isinstance(value, dict) for value in raw):
        raise ValueError(
            f"{section}.auto_approve_tools muss eine Liste von Tabellen sein."
        )

    approvals: list[TrustedMcpToolApproval] = []
    for index, approval_values in enumerate(raw):
        approval_section = f"{section}.auto_approve_tools #{index + 1}"
        name = str(approval_values.get("name", "")).strip()
        contract = str(approval_values.get("contract_sha256", "")).strip()
        if not name:
            raise ValueError(f"{approval_section}.name darf nicht leer sein.")
        contract = validate_tool_contract_fingerprint(
            contract,
            section=approval_section,
        )
        approvals.append(
            TrustedMcpToolApproval(name=name, contract_sha256=contract)
        )

    names = [approval.name for approval in approvals]
    if len(names) != len(set(names)):
        raise ValueError(f"{section}.auto_approve_tools darf Toolnamen nicht doppelt enthalten.")
    return tuple(approvals)


def _trusted_http_bearer_token_env(values: Any, *, section: str) -> str:
    if not isinstance(values, dict):
        raise ValueError(f"{section}.from_env muss eine Tabelle sein.")

    unknown_from_env = set(values) - {"authentication"}
    if unknown_from_env:
        names = ", ".join(sorted(str(name) for name in unknown_from_env))
        raise ValueError(
            f"{section}.from_env unterstützt derzeit nur 'authentication'; "
            f"unbekannt: {names}."
        )

    authentication = values.get("authentication")
    if not isinstance(authentication, dict):
        raise ValueError(f"{section}.from_env.authentication muss eine Tabelle sein.")

    unknown_authentication = set(authentication) - {"bearer"}
    if unknown_authentication:
        names = ", ".join(sorted(str(name) for name in unknown_authentication))
        raise ValueError(
            f"{section}.from_env.authentication unterstützt derzeit nur 'bearer'; "
            f"unbekannt: {names}."
        )

    bearer = authentication.get("bearer")
    if not isinstance(bearer, str) or not bearer.strip():
        raise ValueError(
            f"{section}.from_env.authentication.bearer muss der Name "
            "einer Umgebungsvariable sein."
        )
    bearer = bearer.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", bearer):
        raise ValueError(
            f"{section}.from_env.authentication.bearer muss der Name "
            "einer Umgebungsvariable sein."
        )
    return bearer


def _reject_static_authorization_header(
    headers: tuple[tuple[str, str], ...],
    *,
    section: str,
) -> None:
    if any(name.strip().casefold() == "authorization" for name, _ in headers):
        raise ValueError(
            f"{section}.headers darf Authorization nicht statisch setzen. "
            "Bearer-Authentifizierung wird ausschließlich über "
            f"{section}.from_env.authentication.bearer konfiguriert."
        )


def _trusted_server(values: dict[str, Any], index: int) -> TrustedMcpServer:
    section = f"[[mcp.trusted_servers]] #{index + 1}"
    name = str(values.get("name", "")).strip()
    transport = str(values.get("transport", "")).strip().lower()
    if not name:
        raise ValueError(f"{section}.name darf nicht leer sein.")
    if transport not in {"stdio", "streamable_http"}:
        raise ValueError(f"{section}.transport muss 'stdio' oder 'streamable_http' sein.")

    tools = _trusted_tool_approvals(values, section=section)
    trust_instructions = _bool_value(values, "trust_instructions", False, section=section)
    raw_args = values.get("args", [])
    if not isinstance(raw_args, list) or not all(isinstance(value, str) for value in raw_args):
        raise ValueError(f"{section}.args muss eine String-Liste sein.")

    url_value = values.get("url")
    command_value = values.get("command")
    url = str(url_value).strip() if url_value is not None else None
    command = str(command_value).strip() if command_value is not None else None
    env = _string_map(values.get("env", {}), section=section, key="env")
    headers = _string_map(values.get("headers", {}), section=section, key="headers")
    bearer_token_env: str | None = None

    if transport == "streamable_http":
        if not url:
            raise ValueError(f"{section} benötigt für streamable_http eine url.")
        if command or raw_args or env:
            raise ValueError(f"{section} darf für streamable_http kein command, args oder env enthalten.")
        _reject_static_authorization_header(headers, section=section)
        if "from_env" in values:
            bearer_token_env = _trusted_http_bearer_token_env(
                values["from_env"],
                section=section,
            )
        url = _normalize_mcp_url(url, section=section)
    else:
        if not command:
            raise ValueError(f"{section} benötigt für stdio ein command.")
        if url or headers or "from_env" in values:
            raise ValueError(
                f"{section} darf für stdio keine url, headers oder from_env enthalten."
            )
        if not _trusted_stdio_command_is_deterministic(command):
            raise ValueError(
                f"{section}.command muss für einen trusted stdio-MCP ein absoluter "
                "Executable-Pfad oder exakt '{{python}}' sein; PATH-basierte Commands sind nicht zulässig."
            )

    return TrustedMcpServer(
        name=name,
        transport=transport,
        url=url,
        command=command,
        args=tuple(raw_args),
        env=env,
        headers=headers,
        bearer_token_env=bearer_token_env,
        auto_approve_tools=tools,
        trust_instructions=trust_instructions,
    )


def load_admin_config(path: Path | None = None) -> AdminConfig:
    config_file = default_admin_config_file() if path is None else path.expanduser()
    if not config_file.exists():
        return AdminConfig()
    with config_file.open("rb") as handle:
        values = tomllib.load(handle)

    network_values = values.get("network", {})
    if not isinstance(network_values, dict):
        raise ValueError("[network] in admin_config.toml muss eine Tabelle sein.")
    network = NetworkConfig(
        model_allowed_hosts=_host_list(network_values, "model_allowed_hosts", LOCAL_HOSTS),
        mcp_allowed_hosts=_host_list(network_values, "mcp_allowed_hosts", LOCAL_HOSTS),
        web_allowed_hosts=_host_list(network_values, "web_allowed_hosts", ()),
    )

    model_values = values.get("model", {})
    if not isinstance(model_values, dict):
        raise ValueError("[model] in admin_config.toml muss eine Tabelle sein.")
    raw_credentials = model_values.get("credentials", [])
    if not isinstance(raw_credentials, list) or not all(isinstance(value, dict) for value in raw_credentials):
        raise ValueError("[[model.credentials]] muss eine Liste von Tabellen sein.")
    model_credentials = tuple(_model_credential_rule(v, i) for i, v in enumerate(raw_credentials))
    credential_keys = [(rule.provider, rule.host) for rule in model_credentials]
    if len(credential_keys) != len(set(credential_keys)):
        raise ValueError("[[model.credentials]] darf dieselbe Provider-/Host-Kombination nicht doppelt enthalten.")
    for rule in model_credentials:
        if rule.host not in network.model_allowed_hosts:
            raise ValueError(f"[[model.credentials]] Host {rule.host!r} muss auch in network.model_allowed_hosts erlaubt sein.")

    web_values = values.get("web", {})
    if not isinstance(web_values, dict):
        raise ValueError("[web] in admin_config.toml muss eine Tabelle sein.")
    raw_web_providers = web_values.get("providers", [])
    if not isinstance(raw_web_providers, list) or not all(isinstance(value, dict) for value in raw_web_providers):
        raise ValueError("[[web.providers]] muss eine Liste von Tabellen sein.")
    web_providers = tuple(_web_provider(v, i) for i, v in enumerate(raw_web_providers))
    provider_urls = [provider.base_url for provider in web_providers]
    if len(provider_urls) != len(set(provider_urls)):
        raise ValueError("[[web.providers]].base_url darf nicht doppelt vorkommen.")
    for provider in web_providers:
        provider_host = urlsplit(provider.base_url).hostname
        if provider_host not in network.web_allowed_hosts:
            raise ValueError(
                f"[[web.providers]] Host {provider_host!r} muss auch in "
                "network.web_allowed_hosts erlaubt sein."
            )

    mcp_values = values.get("mcp", {})
    if not isinstance(mcp_values, dict):
        raise ValueError("[mcp] in admin_config.toml muss eine Tabelle sein.")
    if "approval" in mcp_values:
        raise ValueError("[mcp.approval] ist nicht mehr unterstützt. Permanente Freigaben müssen über [[mcp.trusted_servers]] an eine Serveridentität gebunden werden.")
    if "allow_untrusted_stdio" in mcp_values:
        raise ValueError(
            "[mcp].allow_untrusted_stdio wird nicht mehr unterstützt. Externe stdio-MCP-Server "
            "müssen einzeln über [[mcp.trusted_servers]] administrativ definiert werden."
        )
    raw_trusted_servers = mcp_values.get("trusted_servers", [])
    if not isinstance(raw_trusted_servers, list) or not all(isinstance(value, dict) for value in raw_trusted_servers):
        raise ValueError("[[mcp.trusted_servers]] muss eine Liste von Tabellen sein.")
    trusted_servers = tuple(_trusted_server(v, i) for i, v in enumerate(raw_trusted_servers))
    names = [server.name for server in trusted_servers]
    if len(names) != len(set(names)):
        raise ValueError("[[mcp.trusted_servers]].name darf nicht doppelt vorkommen.")

    return AdminConfig(
        network=network,
        model_credentials=model_credentials,
        web=WebPolicy(providers=web_providers),
        mcp=McpPolicy(trusted_servers=trusted_servers),
    )
