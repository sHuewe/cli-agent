from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
import logging
from pathlib import Path
from typing import Any

from .admin_config import AdminConfig, load_admin_config
from .config import (
    AppConfig,
    McpServerConfig,
    default_config_file,
    load_config,
)
from .file_context import (
    ContextFileCliAgent,
    FileContext,
    OutputTarget,
    is_local_agent_command,
    prepare_file_options,
)
from .logging_setup import configure_logging
from .model import ModelRetryPolicy, RetryingModelClient
from .model_factory import create_model_client

OS_MCP_SERVER_NAME = "os"
logger = logging.getLogger("cli_agent.execution")
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
    retry_policy: ModelRetryPolicy | None = None
    response_format: str = "text"
    context_files: tuple[Path, ...] = ()
    output: Path | None = None
    overwrite_output: bool = False
    add_web_context: tuple[str, ...] = ()
    approval_callback: ApprovalCallback | None = None
    prepared_file_contexts: tuple[FileContext, ...] = ()
    prepared_output_target: OutputTarget | None = None
    mutation_protected_paths: tuple[Path, ...] = ()


@dataclass(frozen=True)
class OneShotRunResult:
    answer: str
    usage: str
    web_context_statuses: tuple[str, ...]
    config: AppConfig
    file_contexts: tuple[FileContext, ...]
    output_target: OutputTarget | None


def os_mcp_server_config(
    access: str,
    *,
    mutation_protected_paths: tuple[Path, ...] = (),
) -> McpServerConfig:
    if access not in {"read", "write"}:
        raise ValueError(f"Unsupported OS MCP access mode: {access!r}")
    return McpServerConfig(
        name=OS_MCP_SERVER_NAME,
        transport="stdio",
        command="{python}",
        args=(
            "-m",
            "cli_agent.os_mcp_server",
            "--project-directory",
            "{workspace_directory}",
            "--config-file",
            "{config_file}",
            "--access",
            access,
        ),
        literal_args=tuple(
            argument
            for path in mutation_protected_paths
            for argument in (
                "--mutation-protected-path",
                str(path),
            )
        ),
        config={"allow_write_files": access == "write"},
        built_in=True,
    )


def apply_workspace_access_override(
    config: AppConfig,
    *,
    workspace_access: str | None,
    mutation_protected_paths: tuple[Path, ...] = (),
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
        ),
    )
    return replace(config, mcp_servers=servers)


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


async def run_once(
    options: OneShotRunOptions,
    *,
    dependencies: ExecutionDependencies | None = None,
) -> OneShotRunResult:
    deps = dependencies or ExecutionDependencies()
    workspace = options.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"Arbeitsordner existiert nicht: {workspace}")
    if not options.prompt or options.prompt.isspace():
        raise ValueError("One-Shot-Prompt darf nicht leer sein.")
    response_format = _validate_response_format(options.response_format)

    config = deps.load_config(options.config_file)
    admin_config = deps.load_admin_config()
    config = apply_model_override(config, model=options.model)
    config = apply_workspace_access_override(
        config,
        workspace_access=options.workspace_access,
        mutation_protected_paths=options.mutation_protected_paths,
    )

    if options.prepared_file_contexts or options.prepared_output_target is not None:
        if options.context_files or options.output is not None:
            raise ValueError(
                "Vorbereitete Dateioptionen dürfen nicht zusammen mit "
                "context_files/output übergeben werden."
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
    model_client = deps.create_model_client(
        config.model,
        network=admin_config.network,
        credential_rules=admin_config.model_credentials,
    )
    if options.retry_policy is not None:
        model_client = RetryingModelClient(
            model_client,
            options.retry_policy,
        )
    agent = deps.agent_type(
        workspace,
        model_client,
        config.mcp_servers,
        logging_config=config.logging,
        config_file=options.config_file or default_config_file(),
        dump_llm_context=config.dump_llm_context,
        network=admin_config.network,
        web_providers=admin_config.web.providers,
        mcp_policy=admin_config.mcp,
        approval_callback=options.approval_callback,
        okf=config.okf,
        file_contexts=file_contexts,
        response_format=response_format,
    )

    web_statuses: list[str] = []
    async with agent:
        for url in options.add_web_context:
            web_statuses.append(
                await agent.ask(f"add_web_context {url}")
            )
        answer = await agent.ask(options.prompt)
        usage = await agent.ask("tokens")

    if (
        output_target is not None
        and not is_local_agent_command(options.prompt)
    ):
        output_target.write_text(answer)

    return OneShotRunResult(
        answer=answer,
        usage=usage,
        web_context_statuses=tuple(web_statuses),
        config=config,
        file_contexts=file_contexts,
        output_target=output_target,
    )
