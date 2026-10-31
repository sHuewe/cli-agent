from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class NetworkConfig:
    """Explicit host allowlists for every network-capable feature."""

    model_allowed_hosts: tuple[str, ...] = LOCAL_HOSTS
    mcp_allowed_hosts: tuple[str, ...] = LOCAL_HOSTS
    # Web retrieval is disabled until an operator explicitly allowlists hosts.
    web_allowed_hosts: tuple[str, ...] = ()


def _normalized_hosts(hosts: tuple[str, ...] | list[str]) -> set[str]:
    return {
        host.strip().lower().rstrip(".")
        for host in hosts
        if isinstance(host, str) and host.strip()
    }


def validate_http_url(
    url: str,
    *,
    allowed_hosts: tuple[str, ...] | list[str],
    purpose: str,
) -> str:
    """Validate a URL and require an exact, operator-provided host allowlist."""
    value = url.strip()
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise ValueError(f"{purpose} unterstützt nur http:// und https:// URLs.")
    if not parsed.hostname:
        raise ValueError(f"{purpose} URL enthält keinen Hostnamen.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"Credentials in {purpose}-URLs werden nicht unterstützt.")

    hostname = parsed.hostname.lower().rstrip(".")
    allowed = _normalized_hosts(allowed_hosts)
    if hostname not in allowed:
        configured = ", ".join(sorted(allowed)) or "(keine)"
        raise ValueError(
            f"{purpose}-Host {parsed.hostname!r} ist nicht erlaubt. "
            f"Erlaubte Hosts: {configured}."
        )
    if scheme == "http" and hostname not in _normalized_hosts(LOCAL_HOSTS):
        raise ValueError(
            f"Unverschlüsseltes HTTP ist für {purpose} nur zu lokalen Hosts erlaubt."
        )
    return value
