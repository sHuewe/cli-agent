from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any

from .network_policy import LOCAL_HOSTS, NetworkConfig


def application_directory() -> Path:
    local_app_data = os.getenv("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "cli-agent"

    xdg_state_home = os.getenv("XDG_STATE_HOME")
    if xdg_state_home:
        return Path(xdg_state_home) / "cli-agent"

    return Path.home() / ".cli-agent"


def default_config_file() -> Path:
    return application_directory() / "config.toml"


def ensure_default_config_file() -> Path:
    """Create the packaged safe default config if no user config exists yet."""
    config_file = default_config_file().expanduser()
    if config_file.exists():
        return config_file

    template = (
        files("cli_agent").joinpath("config.example.toml").read_text(encoding="utf-8")
    )
    config_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with config_file.open("x", encoding="utf-8") as handle:
            handle.write(template)
    except FileExistsError:
        # Another process created the config between exists() and open().
        pass
    return config_file


@dataclass(frozen=True)
class ModelConfig:
    provider: str = "ollama"
    model: str = "qwen3.5:9b"
    base_url: str = "http://localhost:11434"
    api_key_env: str | None = None
    timeout: float = 120.0
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class LoggingConfig:
    enabled: bool = True
    level: str = "INFO"
    file: Path = application_directory() / "cli-agent.log"
    log_prompts: bool = False
    log_tool_calls: bool = False
    log_model_messages: bool = False
    log_tool_results: bool = False
    max_bytes: int = 5_000_000
    backup_count: int = 3


@dataclass(frozen=True)
class OkfConfig:
    repository: str
    max_tool_calls: int = 200
    max_read_bytes: int = 2_560_000
    compress_min_chars: int = 20_000
    required: bool = True


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    transport: str = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    compress_result: bool = False
    compress_min_chars: int = 8000
    # Internal marker for server definitions created by the CLI or built-in
    # retrieval flow. User-provided MCP configurations remain untrusted.
    built_in: bool = False

    def allow_write_files(self) -> bool:
        return self.config.get("allow_write_files", False) is True

    def allow_modify_services(self) -> bool:
        return self.config.get("allow_modify_services", False) is True


@dataclass(frozen=True)
class AppConfig:
    dump_llm_context: bool = False
    model: ModelConfig = ModelConfig()
    logging: LoggingConfig = LoggingConfig()
    network: NetworkConfig = field(default_factory=NetworkConfig)
    mcp_servers: tuple[McpServerConfig, ...] = ()
    okf: OkfConfig | None = None


def _bool_value(
    values: dict[str, Any],
    key: str,
    default: bool,
    *,
    section: str,
) -> bool:
    value = values.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{section}.{key} muss true oder false sein.")
    return value


def _logging_config(values: dict[str, Any]) -> LoggingConfig:
    defaults = LoggingConfig()
    configured_file = values.get("file", defaults.file)
    return LoggingConfig(
        enabled=_bool_value(
            values,
            "enabled",
            defaults.enabled,
            section="[logging]",
        ),
        level=str(values.get("level", defaults.level)).upper(),
        file=Path(configured_file).expanduser(),
        log_prompts=_bool_value(
            values, "log_prompts", defaults.log_prompts, section="[logging]"
        ),
        log_tool_calls=_bool_value(
            values, "log_tool_calls", defaults.log_tool_calls, section="[logging]"
        ),
        log_model_messages=_bool_value(
            values,
            "log_model_messages",
            defaults.log_model_messages,
            section="[logging]",
        ),
        log_tool_results=_bool_value(
            values, "log_tool_results", defaults.log_tool_results, section="[logging]"
        ),
        max_bytes=int(values.get("max_bytes", defaults.max_bytes)),
        backup_count=int(values.get("backup_count", defaults.backup_count)),
    )


def _model_config(values: dict[str, Any]) -> ModelConfig:
    defaults = ModelConfig()
    provider = str(values.get("provider", defaults.provider)).strip().lower()

    if provider not in {"ollama", "openai"}:
        raise ValueError(f"Nicht unterstützter Modell-Provider: {provider!r}.")

    model = str(values.get("model", defaults.model)).strip()
    base_url = str(values.get("base_url", defaults.base_url)).strip()

    if not model:
        raise ValueError("[model].model darf nicht leer sein.")
    if not base_url:
        raise ValueError("[model].base_url darf nicht leer sein.")

    api_key_env_value = values.get("api_key_env")
    api_key_env = (
        str(api_key_env_value).strip() if api_key_env_value is not None else None
    )

    raw_headers = values.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise ValueError("[model].headers muss eine Tabelle sein.")

    return ModelConfig(
        provider=provider,
        model=model,
        base_url=base_url.rstrip("/"),
        api_key_env=api_key_env,
        timeout=float(values.get("timeout", defaults.timeout)),
        headers={str(key): str(value) for key, value in raw_headers.items()},
    )


def _okf_config(values: dict[str, Any]) -> OkfConfig:
    repository = str(values.get("repository", "")).strip()
    if not repository:
        raise ValueError("[okf].repository darf nicht leer sein.")

    return OkfConfig(
        repository=repository,
        max_tool_calls=int(values.get("max_tool_calls", 200)),
        max_read_bytes=int(values.get("max_read_bytes", 2_560_000)),
        compress_min_chars=int(values.get("compress_min_chars", 20_000)),
        required=_bool_value(values, "required", True, section="[okf]"),
    )


def _host_list(
    values: dict[str, Any],
    key: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    raw_hosts = values.get(key, list(default))
    if not isinstance(raw_hosts, list) or not all(
        isinstance(host, str) and host.strip() for host in raw_hosts
    ):
        raise ValueError(f"[network].{key} muss eine Liste nichtleerer Strings sein.")
    hosts = tuple(host.strip().lower().rstrip(".") for host in raw_hosts)
    if len(hosts) != len(set(hosts)):
        raise ValueError(f"[network].{key} darf keine doppelten Hosts enthalten.")
    return hosts


def _network_config(values: dict[str, Any]) -> NetworkConfig:
    if not isinstance(values, dict):
        raise ValueError("[network] in der Konfiguration muss eine Tabelle sein.")
    return NetworkConfig(
        model_allowed_hosts=_host_list(values, "model_allowed_hosts", LOCAL_HOSTS),
        mcp_allowed_hosts=_host_list(values, "mcp_allowed_hosts", LOCAL_HOSTS),
        web_allowed_hosts=_host_list(values, "web_allowed_hosts", ()),
    )


def _mcp_server_config(values: dict[str, Any]) -> McpServerConfig:
    name = str(values.get("name", "")).strip()
    transport = str(values.get("transport", "stdio")).strip().lower()
    if not name:
        raise ValueError("Jeder [[mcp_servers]]-Eintrag benötigt einen Namen.")
    if transport not in {"stdio", "streamable_http"}:
        raise ValueError(
            f"Nicht unterstützter MCP-Transport für {name!r}: {transport!r}."
        )

    raw_args = values.get("args", [])
    if not isinstance(raw_args, list) or not all(
        isinstance(value, str) for value in raw_args
    ):
        raise ValueError(f"args von MCP-Server {name!r} muss eine String-Liste sein.")

    raw_config = values.get("config", {})
    if not isinstance(raw_config, dict):
        raise ValueError(f"config von MCP-Server {name!r} muss eine Tabelle sein.")
    for key in (
        "allow_untrusted_stdio",
        "allow_write_files",
        "allow_modify_services",
        "wsl",
    ):
        if key in raw_config and not isinstance(raw_config[key], bool):
            raise ValueError(
                f"{key} von MCP-Server {name!r} muss true oder false sein."
            )

    raw_env = values.get("env", {})
    if not isinstance(raw_env, dict):
        raise ValueError(f"env von MCP-Server {name!r} muss eine Tabelle sein.")

    raw_headers = values.get("headers", {})
    if not isinstance(raw_headers, dict):
        raise ValueError(f"headers von MCP-Server {name!r} muss eine Tabelle sein.")

    command_value = values.get("command")
    command = str(command_value).strip() if command_value is not None else None
    url_value = values.get("url")
    url = str(url_value).strip() if url_value is not None else None

    if transport == "stdio":
        if not command:
            raise ValueError(f"Der stdio-MCP-Server {name!r} benötigt command.")
        if url:
            raise ValueError(f"Der stdio-MCP-Server {name!r} darf keine url enthalten.")
    else:
        if not url:
            raise ValueError(f"Der HTTP-MCP-Server {name!r} benötigt eine url.")
        if command or raw_args or raw_env:
            raise ValueError(
                f"Der HTTP-MCP-Server {name!r} darf command, args und env "
                "nicht enthalten."
            )

    compress_result = values.get("compress_result", False)
    if not isinstance(compress_result, bool):
        raise ValueError(
            f"compress_result von MCP-Server {name!r} muss true oder false sein."
        )

    compress_min_chars = values.get("compress_min_chars", 8_000)
    if (
        not isinstance(compress_min_chars, int)
        or isinstance(compress_min_chars, bool)
        or compress_min_chars < 0
    ):
        raise ValueError(
            f"compress_min_chars von MCP-Server {name!r} "
            "muss eine nichtnegative Ganzzahl sein."
        )

    return McpServerConfig(
        name=name,
        transport=transport,
        command=command,
        args=tuple(raw_args),
        env={str(key): str(value) for key, value in raw_env.items()},
        url=url,
        headers={str(key): str(value) for key, value in raw_headers.items()},
        config=raw_config,
        compress_result=compress_result,
        compress_min_chars=compress_min_chars,
    )


def load_config(path: Path | None = None) -> AppConfig:
    config_file = ensure_default_config_file() if path is None else path.expanduser()
    if not config_file.exists():
        return AppConfig()

    with config_file.open("rb") as handle:
        values = tomllib.load(handle)

    model_values = values.get("model", {})
    if not isinstance(model_values, dict):
        raise ValueError("[model] in der Konfiguration muss eine Tabelle sein.")

    logging_values = values.get("logging", {})
    if not isinstance(logging_values, dict):
        raise ValueError("[logging] in der Konfiguration muss eine Tabelle sein.")

    network_config = _network_config(values.get("network", {}))

    raw_servers = values.get("mcp_servers")
    if raw_servers is None:
        mcp_servers = ()
    else:
        if not isinstance(raw_servers, list):
            raise ValueError("[[mcp_servers]] muss eine Liste von Tabellen sein.")
        mcp_servers = tuple(_mcp_server_config(value) for value in raw_servers)
        names = [server.name for server in mcp_servers]
        if len(names) != len(set(names)):
            raise ValueError("Die Namen der MCP-Server müssen eindeutig sein.")

    return AppConfig(
        dump_llm_context=_bool_value(
            values,
            "dump_llm_context",
            False,
            section="Root-Konfiguration",
        ),
        model=_model_config(model_values),
        logging=_logging_config(logging_values),
        network=network_config,
        mcp_servers=mcp_servers,
        okf=_okf_config(values.get("okf", {})) if "okf" in values else None,
    )
