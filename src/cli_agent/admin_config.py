from __future__ import annotations

import platform
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .network_policy import LOCAL_HOSTS, NetworkConfig


@dataclass(frozen=True)
class McpPolicy:
    """Administrator-controlled policy for MCP process and tool execution."""

    allow_untrusted_stdio: bool = False
    auto_approve_tools: tuple[str, ...] = ()


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

    approval_values = mcp_values.get("approval", {})
    if not isinstance(approval_values, dict):
        raise ValueError("[mcp.approval] in admin_config.toml muss eine Tabelle sein.")

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
            auto_approve_tools=_string_list(
                approval_values,
                "auto_approve_tools",
                (),
                section="[mcp.approval]",
            ),
        ),
    )
