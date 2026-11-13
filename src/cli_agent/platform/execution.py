from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
import logging
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

from cli_agent.settings.admin_config import AdminConfig, _normalize_mcp_url, load_admin_config
from cli_agent.settings.config import (
    AppConfig,
    McpServerConfig,
    default_config_file,
    load_config,
)
from cli_agent.context.file_context import (
    ContextFileCliAgent,
    FileContext,
    OutputTarget,
    is_local_agent_command,
    prepare_file_options,
)
from cli_agent.platform.logging_setup import configure_logging
from cli_agent.platform.telemetry import configure_telemetry
from cli_agent.models.model import ModelRetryPolicy, RetryingModelClient
from cli_agent.models.model_factory import create_model_client
from cli_agent.platform.workspace_access import normalize_workspace_access, workspace_access_allows

OS_MCP_SERVER_NAME = "os"
DATA_MCP_SERVER_NAME = "data"
logger = logging.getLogger("cli_agent.platform.execution")
ApprovalCallback = Callable[
    [str, dict[str, object]],
    Awaitable[bool | str],
]


def build_preapproval_callback(
    preapproved_tools: Iterable[str],
    *,
    fallback: ApprovalCallback | None,
) -> ApprovalCallback:
    approved = frozenset(preapproved_tools)

    async def callback(
        tool_name: str,
        arguments: dict[str, object],
    ) -> bool | str:
        if tool_name in approved:
            logger.info(
                "tool_call_preapproved name=%s",
                tool_name,
            )
            return True
        if fallback is None:
            return False
        return await fallback(tool_name, arguments)

    return callback


@dataclass(frozen=True)
class ExecutionDependencies:
    load_config: Callable[[Path | None], AppConfig] = load_config
    load_admin_config: Callable[[], AdminConfig] = load_admin_config
    configure_logging: Callable[..., None] = configure_logging
    create_model_client: Callable[..., Any] = create_model_client
    agent_type: type = ContextFileCliAgent


@dataclass(frozen=True)
class OneShotRunOptions:
    workspace: Path
    prompt: str
    config_file: Path | None = None
    model: str | None = None
    workspace_access: str | None = None
    with_data: bool = False
    retry_policy: ModelRetryPolicy | None = None
    response_format: str = "text"
    context_files: tuple[Path, ...] = ()
    output: Path | None = None
    overwrite_output: bool = False
    add_web_context: tuple[str, ...] = ()
    approval_callback: ApprovalCallback | None = None
    user_message_callback: Callable[[str, str | None], Awaitable[None]] | None = None
    prepared_file_contexts: tuple[FileContext, ...] = ()
    prepared_output_target: OutputTarget | None = None
    mutation_protected_paths: tuple[Path, ...] = ()
    excluded_paths: tuple[Path, ...] = ()
    dump_file_prefix: str | None = None
    with_mcp: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConversationRunOptions:
    workspace: Path
    prompts: tuple[str, ...]
    config_file: Path | None = None
    model: str | None = None
    workspace_access: str | None = None
    with_data: bool = False
    retry_policy: ModelRetryPolicy | None = None
    response_format: str = "text"
    context_files: tuple[Path, ...] = ()
    output: Path | None = None
    overwrite_output: bool = False
    add_web_context: tuple[str, ...] = ()
    approval_callback: ApprovalCallback | None = None
    user_message_callback: Callable[[str, str | None], Awaitable[None]] | None = None
    prepared_file_contexts: tuple[FileContext, ...] = ()
    prepared_output_target: OutputTarget | None = None
    mutation_protected_paths: tuple[Path, ...] = ()
    excluded_paths: tuple[Path, ...] = ()
    dump_file_prefix: str | None = None
    with_mcp: tuple[str, ...] = ()


@dataclass(frozen=True)
class OneShotRunResult:
    answer: str
    usage: str
    web_context_statuses: tuple[str, ...]
    config: AppConfig
    file_contexts: tuple[FileContext, ...]
    output_target: OutputTarget | None


def resolve_excluded_paths(
    workspace: Path,
    paths: Iterable[Path],
) -> tuple[Path, ...]:
    resolved_workspace = workspace.expanduser().resolve()
    resolved_paths: list[Path] = []
    seen: set[Path] = set()
    for value in paths:
        raw = Path(value).expanduser()
        text = str(raw)
        windows = PureWindowsPath(text)
        if ".." in PurePath(text).parts or ".." in windows.parts:
            raise ValueError("Excluded paths must not contain '..'.")
        if windows.drive and not raw.is_absolute():
            raise ValueError(
                "Excluded paths must not use a drive-relative Windows path."
            )
        candidate = raw if raw.is_absolute() else resolved_workspace / raw
        try:
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:
            raise ValueError(
                f"Excluded path could not be resolved: {value}"
            ) from exc
        try:
            resolved.relative_to(resolved_workspace)
        except ValueError as exc:
            raise ValueError(
                f"Excluded path must remain within the workspace: {value}"
            ) from exc
        if resolved not in seen:
            seen.add(resolved)
            resolved_paths.append(resolved)
    return tuple(resolved_paths)


def os_mcp_server_config(
    access: str,
    *,
    mutation_protected_paths: tuple[Path, ...] = (),
    excluded_paths: tuple[Path, ...] = (),
    max_pdf_pages: int = 100,
) -> McpServerConfig:
    if access not in {"read", "write"}:
        raise ValueError(f"Unsupported OS MCP access mode: {access!r}")
    return McpServerConfig(
        name=OS_MCP_SERVER_NAME,
        transport="stdio",
        command="{python}",
        args=(
            "-m",
            "cli_agent.platform.os_mcp_server",
            "--project-directory",
            "{workspace_directory}",
            "--config-file",
            "{config_file}",
            "--access",
            access,
        ),
        literal_args=(
            tuple(
                argument
                for path in excluded_paths
                for argument in (
                    "--protected-path",
                    str(path),
                )
            )
            + tuple(
                argument
                for path in (mutation_protected_paths if access == "write" else ())
                for argument in (
                    "--mutation-protected-path",
                    str(path),
                )
            )
        ),
        config={"allow_write_files": access == "write", "max_pdf_pages": max_pdf_pages},
        built_in=True,
    )


def data_mcp_server_config(
    access: str,
    *,
    mutation_protected_paths: tuple[Path, ...] = (),
    excluded_paths: tuple[Path, ...] = (),
) -> McpServerConfig:
    if access not in {"none", "read", "write"}:
        raise ValueError(f"Unsupported Data MCP access mode: {access!r}")
    return McpServerConfig(
        name=DATA_MCP_SERVER_NAME,
        transport="stdio",
        command="{python}",
        args=(
            "-m",
            "cli_agent.data.data_mcp_server",
            "--project-directory",
            "{workspace_directory}",
            "--config-file",
            "{config_file}",
            "--access",
            access,
        ),
        literal_args=(
            tuple(
                argument
                for path in excluded_paths
                for argument in (
                    "--protected-path",
                    str(path),
                )
            )
            + tuple(
                argument
                for path in mutation_protected_paths
                for argument in (
                    "--mutation-protected-path",
                    str(path),
                )
            )
        ),
        config={"allow_write_files": access == "write"},
        built_in=True,
    )


def apply_data_override(
    config: AppConfig,
    *,
    enabled: bool,
    workspace_access: str | None,
    mutation_protected_paths: tuple[Path, ...] = (),
    excluded_paths: tuple[Path, ...] = (),
) -> AppConfig:
    if not enabled:
        return config
    access = (
        workspace_access
        if workspace_access in {"read", "write"}
        else "none"
    )
    servers = tuple(
        server
        for server in config.mcp_servers
        if server.name != DATA_MCP_SERVER_NAME
    ) + (
        data_mcp_server_config(
            access,
            mutation_protected_paths=(
                mutation_protected_paths
                if access == "write"
                else ()
            ),
            excluded_paths=(
                excluded_paths
                if access in {"read", "write"}
                else ()
            ),
        ),
    )
    return replace(config, mcp_servers=servers)


def apply_workspace_access_override(
    config: AppConfig,
    *,
    workspace_access: str | None,
    mutation_protected_paths: tuple[Path, ...] = (),
    excluded_paths: tuple[Path, ...] = (),
) -> AppConfig:
    if workspace_access is None:
        return config
    if workspace_access == "none":
        servers = tuple(
            server for server in config.mcp_servers
            if server.name != OS_MCP_SERVER_NAME
        )
        return replace(config, mcp_servers=servers)
    if workspace_access not in {"read", "write"}:
        raise ValueError(
            "workspace_access muss 'none', 'read' oder 'write' sein."
        )
    servers = tuple(
        server for server in config.mcp_servers
        if server.name != OS_MCP_SERVER_NAME
    ) + (
        os_mcp_server_config(
            workspace_access,
            mutation_protected_paths=mutation_protected_paths,
            excluded_paths=excluded_paths,
            max_pdf_pages=config.max_pdf_pages,
        ),
    )
    return replace(config, mcp_servers=servers)


def _same_http_mcp_identity(
    existing: McpServerConfig,
    selected: McpServerConfig,
    *,
    workspace: Path | None,
) -> bool:
    """Use the runtime's URL and case-insensitive header identity rules."""
    def expand(value: str) -> str:
        unsupported = ("{python}", "{config_file}")
        if any(part in value for part in unsupported):
            raise ValueError("Disallowed HTTP MCP placeholder.")
        if workspace is None:
            return value
        return (
            value.replace("{workspace_directory}", str(workspace))
            .replace("{project_directory}", str(workspace))
        )

    if existing.url is None or selected.url is None:
        return False
    try:
        first = _normalize_mcp_url(
            expand(existing.url), section=f"MCP-Server {existing.name!r}"
        )
        second = _normalize_mcp_url(
            expand(selected.url), section=f"Trusted MCP-Server {selected.name!r}"
        )
        first_headers = sorted(
            (key.casefold(), expand(value)) for key, value in existing.headers.items()
        )
        second_headers = sorted(
            (key.casefold(), expand(value)) for key, value in selected.headers.items()
        )
    except ValueError:
        return False
    return first == second and first_headers == second_headers


def apply_named_mcp_overrides(
    config: AppConfig,
    *,
    admin_config: AdminConfig,
    server_names: Iterable[str],
    workspace_access: str | None,
    workspace: Path | None = None,
) -> AppConfig:
    """Select admin-defined external MCPs for this invocation only.

    Never accept launch parameters from the caller: stdio process details and
    HTTP endpoint/headers are taken exclusively from the machine-wide policy.
    """
    requested = tuple(server_names)
    if not requested:
        return config

    trusted_by_name = {server.name: server for server in admin_config.mcp.trusted_servers}
    configured = {server.name: server for server in config.mcp_servers}
    added: list[McpServerConfig] = []

    for raw_name in requested:
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError("--with-mcp requires a nonempty MCP server name.")
        name = raw_name.strip()
        if name.casefold() in {OS_MCP_SERVER_NAME, DATA_MCP_SERVER_NAME, "knowledge_reentry"} or (
            name.casefold() == "okf" and config.okf is not None
        ):
            raise ValueError(
                f"MCP server name {name!r} is reserved for a built-in server."
            )

        trusted = trusted_by_name.get(name)
        if trusted is None:
            raise ValueError(
                f"MCP server {name!r} is not defined in [[mcp.trusted_servers]] "
                "of machine-wide admin policy."
            )

        if not workspace_access_allows(
            normalize_workspace_access(workspace_access),
            trusted.required_workspace_access,
        ):
            required_flag = (
                "--with-os-write"
                if trusted.required_workspace_access == "write"
                else "--with-os-read or --with-os-write"
            )
            raise PermissionError(
                f"MCP server {name!r} requires workspace access "
                f"{trusted.required_workspace_access!r}. Use {required_flag}."
            )

        if trusted.transport == "stdio":
            selected = McpServerConfig(name=name)
        elif trusted.transport == "streamable_http":
            selected = McpServerConfig(
                name=name,
                transport="streamable_http",
                url=trusted.url,
                headers=dict(trusted.headers),
            )
        else:
            raise ValueError(
                f"Unsupported administrative MCP transport: "
                f"{trusted.transport!r}."
            )

        existing = configured.get(name)
        if existing is not None:
            same_identity = (
                not existing.built_in
                and existing.transport == selected.transport
                and existing.command is None
                and not existing.args
                and not existing.env
                and (
                    selected.transport == "stdio"
                    or (
                        _same_http_mcp_identity(
                            existing, selected, workspace=workspace
                        )
                    )
                )
            )
            if not same_identity:
                raise ValueError(
                    f"MCP server {name!r} is already configured with a different "
                    "server identity."
                )
            continue

        added.append(selected)
        configured[name] = selected

    return replace(config, mcp_servers=config.mcp_servers + tuple(added))


def _validate_response_format(value: str) -> str:
    if value not in {"text", "json"}:
        raise ValueError("response_format muss 'text' oder 'json' sein.")
    return value


def apply_model_override(
    config: AppConfig,
    *,
    model: str | None,
) -> AppConfig:
    if model is None:
        return config
    return replace(config, model=replace(config.model, model=model))


@dataclass(frozen=True)
class PreparedAgent:
    """Fully configured agent, not yet entered or connected to MCP servers."""

    agent: Any
    config: AppConfig
    file_contexts: tuple[FileContext, ...]
    output_target: OutputTarget | None


def prepare_agent(
    options: OneShotRunOptions | ConversationRunOptions,
    *,
    dependencies: ExecutionDependencies | None = None,
    project_config: AppConfig | None = None,
    admin_config: AdminConfig | None = None,
) -> PreparedAgent:
    """Shared runtime factory for CLI, flow and the Python SDK.

    Project/admin overrides are instance-local; the ordinary CLI always loads
    its configured project file and the mandatory system admin policy.
    No MCP sessions are opened until the caller enters the returned agent.
    """
    deps = dependencies or ExecutionDependencies()
    workspace = options.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"Arbeitsordner existiert nicht: {workspace}")
    response_format = _validate_response_format(options.response_format)
    effective_workspace_access = normalize_workspace_access(options.workspace_access)
    excluded_paths = resolve_excluded_paths(workspace, options.excluded_paths)
    if excluded_paths and options.workspace_access not in {"read", "write"}:
        raise ValueError(
            "excluded_paths requires workspace_access='read' or 'write'."
        )

    if project_config is not None:
        # Explicit SDK configurations remain authoritative over workspace settings.
        config = project_config
    else:
        from cli_agent.settings.project_config import read_project_overrides
        overrides = read_project_overrides(workspace)
        config = (
            load_config(options.config_file, workspace=workspace)
            if overrides else deps.load_config(options.config_file)
        )
    policy = admin_config if admin_config is not None else deps.load_admin_config()
    if project_config is not None and options.config_file is None and config.okf is not None:
        # An in-memory SDK project has no config-file directory. Resolve
        # relative OKF repositories against its workspace before the runtime
        # sees the fallback machine-local config_file path. Explicit project
        # files retain the established config-file-relative semantics.
        repository = Path(config.okf.repository).expanduser()
        if not repository.is_absolute():
            config = replace(
                config, okf=replace(config.okf, repository=str(workspace / repository)),
            )
    config = apply_model_override(config, model=options.model)
    config = apply_workspace_access_override(
        config,
        workspace_access=options.workspace_access,
        mutation_protected_paths=options.mutation_protected_paths,
        excluded_paths=excluded_paths,
    )
    config = apply_data_override(
        config,
        enabled=options.with_data,
        workspace_access=options.workspace_access,
        mutation_protected_paths=options.mutation_protected_paths,
        excluded_paths=excluded_paths,
    )
    config = apply_named_mcp_overrides(
        config,
        admin_config=policy,
        server_names=options.with_mcp,
        workspace_access=options.workspace_access,
        workspace=workspace,
    )

    if project_config is not None:
        # Built-in subprocesses must not load an unrelated default config.toml:
        # the SDK has already resolved the effective project configuration.
        # Access and PDF limits are explicitly passed in the subprocess argv.
        #
        # The explicitly selected project file (when inside the workspace)
        # must remain invisible and immutable to both built-in MCP servers
        # even though it is no longer passed as --config-file.
        protected_project_file: Path | None = None
        if options.config_file is not None:
            project_path = options.config_file.expanduser().resolve()
            if project_path.is_relative_to(workspace):
                protected_project_file = project_path

        servers = []
        for server in config.mcp_servers:
            if server.built_in and server.name in {OS_MCP_SERVER_NAME, DATA_MCP_SERVER_NAME}:
                args = list(server.args)
                marker = args.index("--config-file")
                del args[marker:marker + 2]
                args.append("--sdk-configless")
                if server.name == OS_MCP_SERVER_NAME:
                    args.extend(("--max-pdf-pages", str(config.max_pdf_pages)))
                literal_args = server.literal_args
                if protected_project_file is not None:
                    literal_args += ("--protected-path", str(protected_project_file))
                server = replace(server, args=tuple(args), literal_args=literal_args)
            servers.append(server)
        config = replace(config, mcp_servers=tuple(servers))

    if options.prepared_file_contexts or options.prepared_output_target is not None:
        if options.context_files or options.output is not None:
            raise ValueError(
                "Prepared file options must not be provided together with "
                "context_files/output."
            )
        file_contexts = options.prepared_file_contexts
        output_target = options.prepared_output_target
    else:
        file_contexts, _, output_target = prepare_file_options(
            workspace,
            context_files=options.context_files,
            prompt_file=None,
            output=options.output,
            overwrite_output=options.overwrite_output,
        )

    deps.configure_logging(config.logging)
    configure_telemetry(config.telemetry)
    model_client = deps.create_model_client(
        config.model,
        network=policy.network,
        credential_rules=policy.model_credentials,
    )
    if options.retry_policy is not None:
        model_client = RetryingModelClient(model_client, options.retry_policy)
    from cli_agent.settings.project_config import read_project_system_prompt
    project_system_prompt = read_project_system_prompt(workspace, config.agent.system_prompt)
    agent = deps.agent_type(
        workspace,
        model_client,
        config.mcp_servers,
        logging_config=config.logging,
        agent_timezone=config.agent.timezone,
        system_prompt=project_system_prompt,
        config_file=options.config_file or default_config_file(),
        dump_llm_context=config.dump_llm_context,
        dump_file_prefix=options.dump_file_prefix,
        network=policy.network,
        web_providers=policy.web.providers,
        mcp_policy=policy.mcp,
        approval_callback=options.approval_callback,
        user_message_callback=options.user_message_callback,
        okf=config.okf,
        knowledge_vector=config.knowledge_vector,
        model_credential_rules=policy.model_credentials,
        file_contexts=file_contexts,
        response_format=response_format,
        workspace_access=effective_workspace_access,
        **({"sdk_configless": True} if project_config is not None else {}),
    )
    agent.telemetry_config = config.telemetry
    return PreparedAgent(
        agent=agent,
        config=config,
        file_contexts=file_contexts,
        output_target=output_target,
    )


async def _run_prompt_sequence(
    options: OneShotRunOptions | ConversationRunOptions,
    *,
    prompts: tuple[str, ...],
    dependencies: ExecutionDependencies | None = None,
) -> OneShotRunResult:
    prepared = prepare_agent(options, dependencies=dependencies)
    agent = prepared.agent
    web_statuses: list[str] = []
    async with agent:
        for url in options.add_web_context:
            web_statuses.append(await agent.ask(f"add_web_context {url}"))
        answer = ""
        for prompt in prompts:
            answer = await agent.ask(prompt)
        usage = await agent.ask("tokens")

    final_prompt = prompts[-1]
    if prepared.output_target is not None and not is_local_agent_command(final_prompt):
        prepared.output_target.write_text(answer)

    return OneShotRunResult(
        answer=answer,
        usage=usage,
        web_context_statuses=tuple(web_statuses),
        config=prepared.config,
        file_contexts=prepared.file_contexts,
        output_target=prepared.output_target,
    )


async def run_once(
    options: OneShotRunOptions,
    *,
    dependencies: ExecutionDependencies | None = None,
) -> OneShotRunResult:
    if not options.prompt or options.prompt.isspace():
        raise ValueError("One-Shot-Prompt darf nicht leer sein.")
    return await _run_prompt_sequence(
        options,
        prompts=(options.prompt,),
        dependencies=dependencies,
    )


async def run_conversation(
    options: ConversationRunOptions,
    *,
    dependencies: ExecutionDependencies | None = None,
) -> OneShotRunResult:
    if not options.prompts:
        raise ValueError("Conversation requires at least one prompt.")
    if any(not prompt or prompt.isspace() for prompt in options.prompts):
        raise ValueError("Conversation prompts must not be empty.")
    if any(is_local_agent_command(prompt) for prompt in options.prompts):
        raise ValueError(
            "Conversation prompts must not contain local agent commands."
        )
    return await _run_prompt_sequence(
        options,
        prompts=options.prompts,
        dependencies=dependencies,
    )
