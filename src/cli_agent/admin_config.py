from __future__ import annotations

import platform
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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


def _string_map(values: Any, *, section: str, key: str) -> tuple[tuple[str, str], ...]:
    if values is None:
        return ()
    if not isinstance(values, dict):
        raise ValueError(f"{section}.{key} muss eine Tabelle sein.")
    normalized = tuple(sorted((str(name), str(value)) for name, value in values.items()))
    return normalized


@dataclass(frozen=True)
class TrustedMcpServer:
    """Administrator-defined MCP identity that may receive persistent approvals."""

    name: str
    transport: str
    url: str | None = None
    command: str | None = None
    args: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    headers: tuple[tuple[str, str], ...] = ()
    auto_approve_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class McpPolicy:
    """Administrator-controlled policy for MCP process and tool execution."""

    allow_untrusted_stdio: bool = False
    trusted_servers: tuple[TrustedMcpServer, ...] = ()


@dataclass(frozen=True)
class AdminConfig:
    network: NetworkConfig = NetworkConfig()
    mcp: McpPolicy = McpPolicy()


def default_admin_config_file() -> Path:
    """Return the fixed machine-wide security-policy path.

    On Windows the policy location is deliberately independent of environment
    variables such as PROGRAMDATA, because those can be changed by the caller
    and therefore must not select a security-policy file.
    """
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
    if not isinstance(raw_values, list) or not all(
        isinstance(value, str) and value.strip() for value in raw_values
    ):
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


def _trusted_server(values: dict[str, Any], index: int) -> TrustedMcpServer:
    section = f"[[mcp.trusted_servers]] #{index + 1}"
    name = str(values.get("name", "")).strip()
    transport = str(values.get("transport", "")).strip().lower()
    if not name:
        raise ValueError(f"{section}.name darf nicht leer sein.")
    if transport not in {"stdio", "streamable_http"}:
        raise ValueError(f"{section}.transport muss 'stdio' oder 'streamable_http' sein.")

    tools = _string_list(values, "auto_approve_tools", (), section=section)
    raw_args = values.get("args", [])
    if not isinstance(raw_args, list) or not all(isinstance(value, str) for value in raw_args):
        raise ValueError(f"{section}.args muss eine String-Liste sein.")

    url_value = values.get("url")
    command_value = values.get("command")
    url = str(url_value).strip() if url_value is not None else None
    command = str(command_value).strip() if command_value is not None else None
    env = _string_map(values.get("env", {}), section=section, key="env")
    headers = _string_map(values.get("headers", {}), section=section, key="headers")

    if transport == "streamable_http":
        if not url:
            raise ValueError(f"{section} benötigt für streamable_http eine url.")
        if command or raw_args or env:
            raise ValueError(f"{section} darf für streamable_http kein command, args oder env enthalten.")
        url = _normalize_mcp_url(url, section=section)
    else:
        if not command:
            raise ValueError(f"{section} benötigt für stdio ein command.")
        if url or headers:
            raise ValueError(f"{section} darf für stdio keine url oder headers enthalten.")

    return TrustedMcpServer(
        name=name,
        transport=transport,
        url=url,
        command=command,
        args=tuple(raw_args),
        env=env,
        headers=headers,
        auto_approve_tools=tools,
    )


def load_admin_config(path: Path | None = None) -> AdminConfig:
    """Load the machine-wide security policy or safe built-in defaults.

    The normal user configuration never participates in this policy. If the
    machine-wide file does not exist, only local model/MCP endpoints are allowed,
    web access is disabled, untrusted stdio MCPs are denied, and no external MCP
    tool is auto-approved.
    """
    config_file = default_admin_config_file() if path is None else path.expanduser()
    if not config_file.exists():
        return AdminConfig()

    with config_file.open("rb") as handle:
        values = tomllib.load(handle)

    network_values = values.get("network", {})
    if not isinstance(network_values, dict):
        raise ValueError("[network] in admin_config.toml muss eine Tabelle sein.")

    mcp_values = values.get("mcp", {})
    if not isinstance(mcp_values, dict):
        raise ValueError("[mcp] in admin_config.toml muss eine Tabelle sein.")

    # The legacy name-only approval mechanism is intentionally rejected. A
    # persistent approval must identify the server as well as the tool.
    if "approval" in mcp_values:
        raise ValueError(
            "[mcp.approval] ist nicht mehr unterstützt. Permanente Freigaben "
            "müssen über [[mcp.trusted_servers]] an eine Serveridentität gebunden werden."
        )

    raw_trusted_servers = mcp_values.get("trusted_servers", [])
    if not isinstance(raw_trusted_servers, list) or not all(
        isinstance(value, dict) for value in raw_trusted_servers
    ):
        raise ValueError("[[mcp.trusted_servers]] muss eine Liste von Tabellen sein.")
    trusted_servers = tuple(
        _trusted_server(server_values, index)
        for index, server_values in enumerate(raw_trusted_servers)
    )
    names = [server.name for server in trusted_servers]
    if len(names) != len(set(names)):
        raise ValueError("[[mcp.trusted_servers]].name darf nicht doppelt vorkommen.")

    return AdminConfig(
        network=NetworkConfig(
            model_allowed_hosts=_host_list(network_values, "model_allowed_hosts", LOCAL_HOSTS),
            mcp_allowed_hosts=_host_list(network_values, "mcp_allowed_hosts", LOCAL_HOSTS),
            web_allowed_hosts=_host_list(network_values, "web_allowed_hosts", ()),
        ),
        mcp=McpPolicy(
            allow_untrusted_stdio=_bool_value(
                mcp_values,
                "allow_untrusted_stdio",
                False,
                section="[mcp]",
            ),
            trusted_servers=trusted_servers,
        ),
    )
