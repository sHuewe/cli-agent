from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path, PureWindowsPath
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_FORBIDDEN_ROUTING_HEADERS = frozenset(
    {
        "host",
        ":authority",
        "forwarded",
        "connection",
        "proxy-connection",
        "proxy-authorization",
        "upgrade",
        "x-original-host",
    }
)


def _validated_user_headers(
    values: Any,
    *,
    section: str,
    allow_authorization: bool = True,
) -> dict[str, str]:
    if not isinstance(values, dict):
        raise ValueError(f"{section} muss eine Tabelle sein.")
    result: dict[str, str] = {}
    for raw_name, raw_value in values.items():
        name = str(raw_name).strip()
        if not name:
            raise ValueError(f"{section} contains an empty header name.")
        normalized = name.casefold()
        if normalized in _FORBIDDEN_ROUTING_HEADERS or normalized.startswith("x-forwarded-"):
            raise ValueError(
                f"{section} may not set the routing/proxy-sensitive header {name!r}. "
                "Host and routing are derived solely from the validated URL."
            )
        if normalized == "authorization" and not allow_authorization:
            raise ValueError(
                f"{section} may not set a static Authorization header. "
                "Use the designated credential mechanism "
                "instead of storing secrets in user configuration."
            )
        result[name] = str(raw_value)
    return result


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
    config_file = default_config_file().expanduser()
    if config_file.exists():
        return config_file
    template = files("cli_agent.settings").joinpath("config.example.toml").read_text(encoding="utf-8")
    config_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        with config_file.open("x", encoding="utf-8") as handle:
            handle.write(template)
    except FileExistsError:
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
    context_length: int | None = None


@dataclass(frozen=True)
class AgentConfig:
    timezone: str | None = None
    system_prompt: str | None = None



@dataclass(frozen=True)
class UiCommandButtonsConfig:
    tokens: bool = True
    add_web_context: bool = True
    clear_web_context: bool = True
    enable: bool = True
    disable: bool = True


@dataclass(frozen=True)
class UiButtonsConfig:
    new_session: bool = True
    list_sessions: bool = True
    reset_history: bool = True
    show_working_context: bool = True
    end_session: bool = True
    language_selector: bool = True
    commands: UiCommandButtonsConfig = field(default_factory=UiCommandButtonsConfig)


@dataclass(frozen=True)
class UiConfig:
    title: str = "cli-agent"
    default_language: str | None = None
    template: str | None = None
    theme: str = "bright"
    stylesheets: tuple[str, ...] = ()
    buttons: UiButtonsConfig = field(default_factory=UiButtonsConfig)


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
class VectorKnowledgeConfig:
    mode: str = "okf"  # "okf" (SQLite hybrid) or "chroma" (external read-only)
    source: str | None = None
    database: str = "knowledge.sqlite"
    collection: str = "knowledge"  # Chroma collection name
    provider: str = "ollama"
    model: str = "embeddinggemma"
    base_url: str = "http://localhost:11434"
    api_key_env: str | None = None
    search_limit: int = 5
    max_files: int = 10000
    required: bool = True


@dataclass(frozen=True)
class McpServerConfig:
    name: str
    transport: str = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    literal_args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    config: dict[str, Any] = field(default_factory=dict)
    compress_result: bool = False
    compress_min_chars: int = 8000
    built_in: bool = False
    required_workspace_access: str = "none"

    def allow_write_files(self) -> bool:
        return self.config.get("allow_write_files", False) is True


@dataclass(frozen=True)
class AppConfig:
    dump_llm_context: bool = False
    agent: AgentConfig = AgentConfig()
    ui: UiConfig = field(default_factory=UiConfig)
    model: ModelConfig = ModelConfig()
    logging: LoggingConfig = LoggingConfig()
    mcp_servers: tuple[McpServerConfig, ...] = ()
    okf: OkfConfig | None = None
    knowledge_vector: VectorKnowledgeConfig | None = None
    max_pdf_pages: int = 100


def _bool_value(values: dict[str, Any], key: str, default: bool, *, section: str) -> bool:
    value = values.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{section}.{key} muss true oder false sein.")
    return value


def _agent_config(values: Any) -> AgentConfig:
    if not isinstance(values, dict):
        raise ValueError("[agent] must be a table.")
    if set(values) - {"timezone", "system_prompt"}:
        raise ValueError("[agent] contains unknown options.")
    timezone = values.get("timezone")
    if timezone is not None:
        if not isinstance(timezone, str) or not timezone or timezone != timezone.strip():
            raise ValueError("agent.timezone must be a valid IANA time zone.")
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise ValueError(f"Invalid IANA time zone in agent.timezone: {timezone!r}") from exc
    system_prompt = values.get("system_prompt")
    if system_prompt is not None and (not isinstance(system_prompt, str) or not system_prompt.strip()):
        raise ValueError("agent.system_prompt must be a nonempty filename.")
    return AgentConfig(timezone=timezone, system_prompt=system_prompt)


def _logging_file(values: dict[str, Any], defaults: LoggingConfig) -> Path:
    if "file" not in values:
        return defaults.file

    raw = str(values["file"]).strip()
    if not raw:
        raise ValueError("[logging].file darf nicht leer sein.")
    relative = Path(raw)
    if relative.is_absolute() or PureWindowsPath(raw).is_absolute():
        raise ValueError(
            "[logging].file muss ein relativer Pfad innerhalb des cli-agent-State-Verzeichnisses sein."
        )
    if any(part == ".." for part in relative.parts):
        raise ValueError("[logging].file darf das cli-agent-State-Verzeichnis nicht mit '..' verlassen.")

    base = application_directory().expanduser().resolve()
    target = (base / relative).resolve()
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError(
            "[logging].file muss innerhalb des cli-agent-State-Verzeichnisses liegen."
        ) from exc
    return target


def _logging_config(values: dict[str, Any]) -> LoggingConfig:
    defaults = LoggingConfig()
    return LoggingConfig(
        enabled=_bool_value(values, "enabled", defaults.enabled, section="[logging]"),
        level=str(values.get("level", defaults.level)).upper(),
        file=_logging_file(values, defaults),
        log_prompts=_bool_value(values, "log_prompts", defaults.log_prompts, section="[logging]"),
        log_tool_calls=_bool_value(values, "log_tool_calls", defaults.log_tool_calls, section="[logging]"),
        log_model_messages=_bool_value(values, "log_model_messages", defaults.log_model_messages, section="[logging]"),
        log_tool_results=_bool_value(values, "log_tool_results", defaults.log_tool_results, section="[logging]"),
        max_bytes=int(values.get("max_bytes", defaults.max_bytes)),
        backup_count=int(values.get("backup_count", defaults.backup_count)),
    )


def _model_config(values: dict[str, Any]) -> ModelConfig:
    defaults = ModelConfig()
    provider = str(values.get("provider", defaults.provider)).strip().lower()
    if provider not in {"ollama", "openai"}:
        raise ValueError(f"Unsupported model provider: {provider!r}.")
    model = str(values.get("model", defaults.model)).strip()
    base_url = str(values.get("base_url", defaults.base_url)).strip()
    if not model:
        raise ValueError("[model].model darf nicht leer sein.")
    if not base_url:
        raise ValueError("[model].base_url darf nicht leer sein.")
    api_key_env_value = values.get("api_key_env")
    api_key_env = str(api_key_env_value).strip() if api_key_env_value is not None else None
    raw_headers = values.get("headers", {})
    headers = _validated_user_headers(
        raw_headers,
        section="[model].headers",
        allow_authorization=False,
    )
    raw_context_length = values.get("context_length")
    if raw_context_length is None:
        context_length = None
    elif not isinstance(raw_context_length, int) or isinstance(raw_context_length, bool) or raw_context_length <= 0:
        raise ValueError("[model].context_length muss eine positive Ganzzahl sein.")
    else:
        context_length = raw_context_length
    return ModelConfig(
        provider=provider,
        model=model,
        base_url=base_url.rstrip("/"),
        api_key_env=api_key_env,
        timeout=float(values.get("timeout", defaults.timeout)),
        headers=headers,
        context_length=context_length,
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


def _vector_config(values: dict[str, Any], *, has_okf: bool) -> VectorKnowledgeConfig:
    allowed = {
        "backend", "mode", "source", "database", "collection",
        "provider", "model", "base_url", "api_key_env",
        "search_limit", "max_files", "required",
    }
    if set(values) - allowed:
        raise ValueError("[knowledge.vector] contains unknown options.")
    backend = values.get("backend", "sqlite")
    if backend not in {"sqlite", "chroma"}:
        raise ValueError("knowledge.vector.backend muss 'sqlite' oder 'chroma' sein.")
    mode = values.get("mode", "okf")
    provider = values.get("provider", "ollama")
    if provider not in {"ollama", "openai"}:
        raise ValueError("knowledge.vector.provider muss 'ollama' oder 'openai' sein.")
    if backend == "sqlite":
        if mode != "okf":
            raise ValueError("SQLite vector RAG supports only mode='okf'.")
        if not has_okf:
            raise ValueError("Vector RAG requires an [okf] configuration.")
        if "collection" in values:
            raise ValueError("knowledge.vector.collection is allowed only for Chroma.")
    else:
        if "mode" in values:
            raise ValueError("Chroma verwendet backend='chroma' ohne mode.")
        if "max_files" in values:
            raise ValueError("knowledge.vector.max_files is not applicable to Chroma.")
        mode = "chroma"
    if values.get("source") is not None:
        raise ValueError("knowledge.vector.source ist nicht erlaubt.")
    for key in ("database", "model", "base_url"):
        if not isinstance(values.get(key, "x"), str) or not values.get(key, "x").strip():
            raise ValueError(f"knowledge.vector.{key} darf nicht leer sein.")
    collection = values.get("collection", "knowledge")
    if not isinstance(collection, str) or not collection.strip():
        raise ValueError("knowledge.vector.collection darf nicht leer sein.")
    for key, upper in (("search_limit", 20), ("max_files", 100000)):
        value = values.get(key, 5 if key == "search_limit" else 10000)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
            raise ValueError(f"knowledge.vector.{key} muss zwischen 1 und {upper} liegen.")
    env = values.get("api_key_env")
    if env is not None and (not isinstance(env, str) or not env.strip()):
        raise ValueError("api_key_env muss ein nicht-leerer Variablenname sein.")
    return VectorKnowledgeConfig(
        mode=mode, source=None,
        database=values.get("database", "knowledge.sqlite"),
        collection=collection, provider=provider,
        model=values.get("model", "embeddinggemma"),
        base_url=values.get("base_url", "http://localhost:11434"),
        api_key_env=env, search_limit=values.get("search_limit", 5),
        max_files=values.get("max_files", 10000),
        required=_bool_value(values, "required", True, section="[knowledge.vector]"),
    )

def _mcp_server_config(values: dict[str, Any]) -> McpServerConfig:
    name = str(values.get("name", "")).strip()
    transport = str(values.get("transport", "stdio")).strip().lower()
    if not name:
        raise ValueError("Every [[mcp_servers]] entry requires a name.")
    if name.casefold() == "knowledge_reentry":
        raise ValueError("MCP server name 'knowledge_reentry' is reserved for an internal agent tool.")
    if transport not in {"stdio", "streamable_http"}:
        raise ValueError(f"Unsupported MCP transport for {name!r}: {transport!r}.")

    if transport == "stdio":
        extra_keys = set(values) - {"name"}
        if extra_keys:
            raise ValueError(
                f"The stdio MCP server {name!r} may be referenced in user configuration only "
                "by name. Launch configuration is defined exclusively "
                "in [[mcp.trusted_servers]] of admin policy."
            )
        return McpServerConfig(name=name)

    raw_args = values.get("args", [])
    if not isinstance(raw_args, list) or not all(isinstance(value, str) for value in raw_args):
        raise ValueError(f"args von MCP-Server {name!r} muss eine String-Liste sein.")
    raw_config = values.get("config", {})
    if not isinstance(raw_config, dict):
        raise ValueError(f"config von MCP-Server {name!r} muss eine Tabelle sein.")
    raw_env = values.get("env", {})
    if not isinstance(raw_env, dict):
        raise ValueError(f"env von MCP-Server {name!r} muss eine Tabelle sein.")
    raw_headers = values.get("headers", {})
    headers = _validated_user_headers(
        raw_headers,
        section=f"headers von MCP-Server {name!r}",
        allow_authorization=False,
    )
    command_value = values.get("command")
    command = str(command_value).strip() if command_value is not None else None
    url_value = values.get("url")
    url = str(url_value).strip() if url_value is not None else None
    if not url:
        raise ValueError(f"The HTTP MCP server {name!r} requires a url.")
    if command or raw_args or raw_env:
        raise ValueError(f"The HTTP MCP server {name!r} must not contain command, args or env.")
    compress_result = values.get("compress_result", False)
    if not isinstance(compress_result, bool):
        raise ValueError(f"compress_result von MCP-Server {name!r} muss true oder false sein.")
    compress_min_chars = values.get("compress_min_chars", 8_000)
    if not isinstance(compress_min_chars, int) or isinstance(compress_min_chars, bool) or compress_min_chars < 0:
        raise ValueError(f"compress_min_chars von MCP-Server {name!r} muss eine nichtnegative Ganzzahl sein.")
    return McpServerConfig(
        name=name,
        transport=transport,
        url=url,
        headers=headers,
        config=raw_config,
        compress_result=compress_result,
        compress_min_chars=compress_min_chars,
    )


def _ui_config(values: Any) -> UiConfig:
    """Parse presentation-only browser settings, with strict field validation."""
    if not isinstance(values, dict):
        raise ValueError("[ui] must be a table.")
    if set(values) - {"title", "default_language", "template", "theme", "stylesheets", "buttons"}:
        raise ValueError("[ui] contains unknown options.")

    title = values.get("title", "cli-agent")
    if (
        not isinstance(title, str)
        or not title
        or title != title.strip()
        or len(title) > 120
        or any(ord(char) < 32 or ord(char) == 127 for char in title)
    ):
        raise ValueError("ui.title must be a nonempty title of at most 120 characters without control characters.")

    default_language = values.get("default_language")
    if default_language is not None:
        from cli_agent.interfaces.web_ui_locales import available_locales
        if (
            not isinstance(default_language, str)
            or default_language != default_language.strip()
            or default_language not in available_locales()
        ):
            raise ValueError("ui.default_language must name an available UI translation.")

    template = values.get("template")
    if template is not None:
        from cli_agent.settings.project_config import validate_project_ui_template_name
        validate_project_ui_template_name(template)

    theme = values.get("theme", "bright")
    if not isinstance(theme, str) or theme not in {"bright", "dark"}:
        raise ValueError("ui.theme must be 'bright' or 'dark'.")

    stylesheets = values.get("stylesheets", [])
    if not isinstance(stylesheets, list) or len(stylesheets) > 8:
        raise ValueError("ui.stylesheets must be a list of at most 8 CSS files.")
    from cli_agent.settings.project_config import validate_project_ui_stylesheet_name
    for stylesheet in stylesheets:
        validate_project_ui_stylesheet_name(stylesheet)
    if len(stylesheets) != len(set(stylesheets)):
        raise ValueError("ui.stylesheets must not contain duplicates.")

    buttons = values.get("buttons", {})
    if not isinstance(buttons, dict):
        raise ValueError("[ui.buttons] must be a table.")
    button_fields = (
        "new_session", "list_sessions", "reset_history",
        "show_working_context", "end_session", "language_selector",
    )
    if set(buttons) - {*button_fields, "commands"}:
        raise ValueError("[ui.buttons] contains unknown options.")

    commands = buttons.get("commands", {})
    if not isinstance(commands, dict):
        raise ValueError("[ui.buttons.commands] must be a table.")
    command_fields = (
        "tokens", "add_web_context", "clear_web_context", "enable", "disable",
    )
    if set(commands) - set(command_fields):
        raise ValueError("[ui.buttons.commands] contains unknown options.")

    command_config = UiCommandButtonsConfig(**{
        name: _bool_value(commands, name, True, section="ui.buttons.commands")
        for name in command_fields
    })
    button_config = UiButtonsConfig(
        **{
            name: _bool_value(buttons, name, True, section="ui.buttons")
            for name in button_fields
        },
        commands=command_config,
    )
    return UiConfig(
        title=title, default_language=default_language, template=template,
        theme=theme, stylesheets=tuple(stylesheets), buttons=button_config,
    )


def _parse_config_values(values: dict[str, Any]) -> AppConfig:
    """Validate the effective configuration after field-level overrides."""
    if "network" in values:
        raise ValueError(
            "[network] is no longer user configuration. Network allowlists "
            "are loaded only from machine-wide admin_config.toml."
        )
    model_values = values.get("model", {})
    if not isinstance(model_values, dict):
        raise ValueError("[model] in der Konfiguration muss eine Tabelle sein.")
    logging_values = values.get("logging", {})
    if not isinstance(logging_values, dict):
        raise ValueError("[logging] in der Konfiguration muss eine Tabelle sein.")
    raw_servers = values.get("mcp_servers")
    if raw_servers is None:
        mcp_servers = ()
    else:
        if not isinstance(raw_servers, list):
            raise ValueError("[[mcp_servers]] muss eine Liste von Tabellen sein.")
        mcp_servers = tuple(_mcp_server_config(value) for value in raw_servers)
        names = [server.name for server in mcp_servers]
        if len(names) != len(set(names)):
            raise ValueError("MCP server names must be unique.")
    pdf_values = values.get("pdf", {})
    if not isinstance(pdf_values, dict):
        raise ValueError("[pdf] muss eine Tabelle sein.")
    if set(pdf_values) - {"max_pdf_pages"}:
        raise ValueError("[pdf] contains unknown options.")
    max_pdf_pages = pdf_values.get("max_pdf_pages", 100)
    if isinstance(max_pdf_pages, bool) or not isinstance(max_pdf_pages, int) or not 1 <= max_pdf_pages <= 2_000:
        raise ValueError("pdf.max_pdf_pages muss zwischen 1 und 2000 liegen.")
    okf = _okf_config(values.get("okf", {})) if "okf" in values else None
    if okf is not None and any(
        server.name.casefold() == "okf" for server in mcp_servers
    ):
        raise ValueError(
            "Der MCP-Servername 'okf' ist reserviert, wenn [okf] konfiguriert ist."
        )
    knowledge_values = values.get("knowledge", {})
    if not isinstance(knowledge_values, dict) or set(knowledge_values) - {"vector"}:
        raise ValueError("[knowledge] contains unknown options.")
    vector_values = knowledge_values.get("vector")
    if vector_values is not None and not isinstance(vector_values, dict):
        raise ValueError("[knowledge.vector] muss eine Tabelle sein.")
    knowledge_vector = _vector_config(vector_values, has_okf=okf is not None) if vector_values is not None else None
    return AppConfig(
        knowledge_vector=knowledge_vector,
        dump_llm_context=_bool_value(values, "dump_llm_context", False, section="Root-Konfiguration"),
        agent=_agent_config(values.get("agent", {})),
        ui=_ui_config(values.get("ui", {})),
        model=_model_config(model_values),
        logging=_logging_config(logging_values),
        mcp_servers=mcp_servers,
        okf=okf,
        max_pdf_pages=max_pdf_pages,
    )

def load_config(path: Path | None = None, *, workspace: Path | None = None) -> AppConfig:
    """Read global config and optionally overlay workspace/.cli-agent/project.toml."""
    if path is None:
        config_file = ensure_default_config_file()
    else:
        config_file = path.expanduser()
        if not config_file.exists():
            raise FileNotFoundError(
                f"Explizit angegebene Konfigurationsdatei existiert nicht: {config_file}"
            )
    values: dict[str, Any] = {}
    if config_file.exists():
        with config_file.open("rb") as handle:
            values = tomllib.load(handle)
    if workspace is not None:
        from cli_agent.settings.project_config import (
            merge_project_overrides, read_project_overrides,
        )
        values = merge_project_overrides(values, read_project_overrides(workspace))
    return _parse_config_values(values)
