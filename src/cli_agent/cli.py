from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import traceback
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .admin_config import AdminConfig, default_admin_config_file, load_admin_config
from .agent import CliAgent
from .approval_display import approval_arguments as _approval_arguments
from .config import AppConfig, McpServerConfig, default_config_file, load_config
from .file_context import (
    ContextFileCliAgent as WebContextCliAgent,
    is_local_agent_command,
    prepare_file_options,
)
from .execution import (
    ExecutionDependencies,
    OneShotRunOptions,
    apply_model_override,
    build_preapproval_callback,
    apply_workspace_access_override,
    os_mcp_server_config,
    run_once,
)
from .logging_setup import configure_logging
from .mcp_contracts import tool_contract_fingerprint
from .model_factory import create_model_client
from .prompt_template import PromptTemplate, parse_variable_assignments
from .terminal_output import sanitize_terminal_text

OS_MCP_SERVER_NAME = "os"
REDACTED_CONFIG_VALUE = "<WERT AUS KONFIGURATION ÜBERNEHMEN>"
logger = logging.getLogger("cli_agent.cli")


@dataclass(frozen=True)
class McpToolInspection:
    server_name: str
    transport: str
    tool_name: str
    description: str
    input_schema: Any
    contract_sha256: str
    trusted_server_found: bool
    existing_contract_sha256: str | None


class _InspectionModel:
    model = "mcp-contract-inspection"
    base_url = "local://not-used"

    async def chat(self, *_args, **_kwargs):
        raise RuntimeError("Der MCP-Inspektionsmodus führt keine Modellaufrufe aus.")


async def approve_tool_call(tool_name: str, arguments: dict[str, object]) -> bool | str:
    if not sys.stdin.isatty():
        return False
    safe_tool_name = sanitize_terminal_text(
        tool_name,
        multiline=False,
        escape_invisible_formatting=True,
        escape_literal_backslashes=True,
    )
    print(
        "\nExplizite Freigabe erforderlich: "
        f"{safe_tool_name}({_approval_arguments(arguments)})"
    )
    answer = await asyncio.to_thread(input, "Aktion ausführen? [j]a / [s] dieses Tool für die Session / [N]ein ")
    normalized = answer.strip().casefold()
    if normalized in {"s", "session"}:
        return "session"
    return normalized in {"j", "ja", "y", "yes"}


def build_approval_callback(preapproved_tools: Iterable[str]) -> Callable[[str, dict[str, object]], Awaitable[bool | str]]:
    return build_preapproval_callback(
        preapproved_tools,
        fallback=approve_tool_call,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli-agent", description="General local agent using configurable MCP servers")
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt; omit it for interactive mode")
    parser.add_argument("--workspace", type=Path, default=Path.cwd(), help="Fixed workspace directory (default: current directory)")
    parser.add_argument("--config", type=Path, default=None, help=f"Configuration file (default: {default_config_file()})")
    parser.add_argument("--model", default=None, help="Override model.model from the configuration file")
    parser.add_argument(
        "--response-format",
        choices=("text", "json"),
        default="text",
        help=(
            "Expected final response format. 'text' is the default; 'json' "
            "requires syntactically valid JSON and triggers up to two format "
            "correction attempts."
        ),
    )
    os_access = parser.add_mutually_exclusive_group()
    os_access.add_argument("--with-os-read", action="store_const", const="read", dest="os_access", help="Enable the built-in workspace OS MCP server with read-only access. Overrides an 'os' MCP server from the config.")
    os_access.add_argument("--with-os-write", action="store_const", const="write", dest="os_access", help="Enable the built-in workspace OS MCP server with read and write access. Overrides an 'os' MCP server from the config.")
    parser.add_argument(
        "--context-file",
        "--add-file-context",
        dest="context_files",
        action="append",
        type=Path,
        default=[],
        metavar="FILE",
        help=(
            "Add one explicit UTF-8 text file from the workspace as "
            "untrusted reference context. Repeat for multiple files. "
            "--add-file-context is an alias."
        ),
    )
    parser.add_argument("--prompt-file", type=Path, default=None, metavar="FILE", help="Read the one-shot user prompt from one explicit UTF-8 text file inside the workspace. Supports {{var:name}} template variables.")
    parser.add_argument("--var", action="append", default=[], metavar="NAME=VALUE", help="Set one prompt-template variable. Repeat for multiple variables.")
    parser.add_argument("--output", type=Path, default=None, metavar="FILE", help="Write the latest model answer to a workspace-local UTF-8 text file in addition to stdout.")
    parser.add_argument("--overwrite-output", action="store_true", help="Allow --output to replace an existing regular file. Requires --output.")
    parser.add_argument("--approve-tool", action="append", default=[], metavar="TOOL", help="Pre-approve one exact exposed tool name for this process run; repeat for multiple tools.")
    parser.add_argument("--add-web-context", action="append", default=[], metavar="URL", help="Load a web URL before processing the prompt; repeat for multiple URLs.")
    parser.add_argument("--debug", action="store_true", help="Show a complete traceback when an error occurs")
    return parser


def build_admin_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli-agent admin", description="Inspect MCP tool contracts and generate admin-policy fragments. These commands never modify admin_config.toml.")
    subparsers = parser.add_subparsers(dest="admin_command", required=True)
    inspect_tool = subparsers.add_parser("inspect-tool", help="Inspect the current MCP tool schema and contract fingerprint.")
    inspect_tool.add_argument("server", help="Configured MCP server name")
    inspect_tool.add_argument("tool", help="Native MCP tool name")
    inspect_tool.add_argument("--config", type=Path, default=None)
    inspect_tool.add_argument("--workspace", type=Path, default=Path.cwd())
    trust_tool = subparsers.add_parser("trust-tool", help="Generate a pinned auto-approval fragment for one MCP tool.")
    trust_tool.add_argument("server", help="Configured MCP server name")
    trust_tool.add_argument("tool", help="Native MCP tool name")
    trust_tool.add_argument("--config", type=Path, default=None)
    trust_tool.add_argument("--workspace", type=Path, default=Path.cwd())
    trust_tool.add_argument("--update", action="store_true", help="Compare against an existing pinned approval and print a replacement fragment. The admin policy is never written automatically.")
    return parser


def _os_mcp_server_config(access: str) -> McpServerConfig:
    return os_mcp_server_config(access)


def apply_mcp_cli_overrides(config: AppConfig, *, os_access: str | None) -> AppConfig:
    return apply_workspace_access_override(
        config,
        workspace_access=os_access,
    )


def apply_model_cli_override(config: AppConfig, *, model: str | None) -> AppConfig:
    return apply_model_override(config, model=model)


def exception_details(exc: BaseException) -> str:
    leaves: list[str] = []

    def collect(current: BaseException) -> None:
        nested = getattr(current, "exceptions", None)
        if nested:
            for child in nested:
                collect(child)
            return
        message = str(current).strip()
        label = type(current).__name__
        leaves.append(f"{label}: {message}" if message else label)

    collect(exc)
    return "\n".join(dict.fromkeys(leaves))


def print_error(exc: BaseException, *, debug: bool) -> None:
    if debug:
        rendered = "".join(traceback.format_exception(exc))
        sys.stderr.write(sanitize_terminal_text(rendered, multiline=True))
    else:
        print(
            "Fehler: "
            + sanitize_terminal_text(exception_details(exc), multiline=True)
        )


async def _inspect_mcp_tool(*, server: McpServerConfig, tool_name: str, workspace: Path, config_file: Path, admin_config: AdminConfig) -> McpToolInspection:
    if getattr(server, "built_in", False):
        raise ValueError("Built-in MCP-Tools werden nicht über permanente Admin-Auto-Approvals freigegeben.")
    native_tool_name = tool_name.strip()
    exposed_prefix = f"{server.name}__"
    if native_tool_name.startswith(exposed_prefix):
        native_tool_name = native_tool_name[len(exposed_prefix):]
    if not native_tool_name:
        raise ValueError("Toolname darf nicht leer sein.")
    agent = CliAgent(workspace, _InspectionModel(), (server,), config_file=config_file, network=admin_config.network, mcp_policy=admin_config.mcp)
    async with agent:
        exposed_name = f"{server.name}__{native_tool_name}"
        tool_metadata = next((tool for tool in agent._server_tools.get(server.name, ()) if tool.get("function", {}).get("name") == exposed_name), None)
        if tool_metadata is None:
            available = [tool.get("function", {}).get("name", "") for tool in agent._server_tools.get(server.name, ())]
            raise ValueError(f"MCP-Server {server.name!r} bietet Tool {native_tool_name!r} nicht an. Verfügbare Tools: {', '.join(name for name in available if name) or '(keine)'}")
        function = tool_metadata.get("function", {})
        schema = function.get("parameters", {})
        description = str(function.get("description") or "")
        contract = tool_contract_fingerprint(native_tool_name, schema, description)
        effective_server = getattr(agent, "_server_configs", {}).get(server.name, server)
        trusted = next((item for item in admin_config.mcp.trusted_servers if agent._trusted_server_matches(effective_server, item)), None)
        existing = None
        if trusted is not None:
            existing_approval = next((approval for approval in trusted.auto_approve_tools if approval.name == native_tool_name), None)
            if existing_approval is not None:
                existing = existing_approval.contract_sha256
        return McpToolInspection(server_name=effective_server.name, transport=effective_server.transport, tool_name=native_tool_name, description=description, input_schema=schema, contract_sha256=contract, trusted_server_found=trusted is not None, existing_contract_sha256=existing)


def _render_tool_approval_fragment(inspection: McpToolInspection) -> str:
    return "\n".join(["# Unter dem zugehörigen [[mcp.trusted_servers]]-Eintrag einfügen:", "[[mcp.trusted_servers.auto_approve_tools]]", f"name = {json.dumps(inspection.tool_name, ensure_ascii=False)}", f"contract_sha256 = {json.dumps(inspection.contract_sha256)}"])


def _config_pairs(values: Any) -> tuple[tuple[str, str], ...]:
    if isinstance(values, dict):
        return tuple(sorted((str(key), str(value)) for key, value in values.items()))
    return tuple(sorted((str(key), str(value)) for key, value in (values or ())))


def _render_inline_toml_table(values: Any, *, redact_values: bool = False) -> str:
    pairs = _config_pairs(values)
    return "{ " + ", ".join(
        f"{json.dumps(key, ensure_ascii=False)} = {json.dumps(REDACTED_CONFIG_VALUE if redact_values else value, ensure_ascii=False)}"
        for key, value in pairs
    ) + " }"


def _render_trusted_server_fragment(server: Any) -> str:
    name = str(server.name)
    transport = str(server.transport)
    lines = [
        "[[mcp.trusted_servers]]",
        f"name = {json.dumps(name, ensure_ascii=False)}",
        f"transport = {json.dumps(transport, ensure_ascii=False)}",
    ]
    bearer_token_env = None
    if transport == "streamable_http":
        url = getattr(server, "url", None)
        if url:
            lines.append(f"url = {json.dumps(str(url), ensure_ascii=False)}")
        headers = getattr(server, "headers", None)
        if headers:
            lines.append("# Header-Werte werden aus Sicherheitsgründen nicht ausgegeben; Werte aus der vorhandenen Konfiguration übernehmen.")
            lines.append(f"headers = {_render_inline_toml_table(headers, redact_values=True)}")
        bearer_token_env = getattr(server, "bearer_token_env", None)
    elif transport == "stdio":
        command = getattr(server, "command", None)
        if command:
            lines.append(f"command = {json.dumps(str(command), ensure_ascii=False)}")
        args = tuple(str(value) for value in (getattr(server, "args", ()) or ()))
        if args:
            lines.append("args = [" + ", ".join(json.dumps(value, ensure_ascii=False) for value in args) + "]")
        env = getattr(server, "env", None)
        if env:
            lines.append("# Environment-Werte werden aus Sicherheitsgründen nicht ausgegeben; Werte aus der vorhandenen Admin-Konfiguration übernehmen.")
            lines.append(f"env = {_render_inline_toml_table(env, redact_values=True)}")
    lines.append(f"trust_instructions = {'true' if bool(getattr(server, 'trust_instructions', False)) else 'false'}")
    if bearer_token_env:
        lines.extend(
            [
                "",
                "[mcp.trusted_servers.from_env.authentication]",
                f"bearer = {json.dumps(str(bearer_token_env), ensure_ascii=False)}",
            ]
        )
    return "\n".join(lines)


async def run_admin(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    admin_config = load_admin_config()
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"Arbeitsordner existiert nicht: {workspace}")
    server = next((item for item in config.mcp_servers if item.name == args.server), None)
    if server is None:
        raise ValueError(f"MCP-Server {args.server!r} ist in der Benutzerkonfiguration nicht definiert.")
    config_file = args.config or default_config_file()
    inspection = await _inspect_mcp_tool(server=server, tool_name=args.tool, workspace=workspace, config_file=config_file, admin_config=admin_config)
    print(
        "MCP-Server: "
        + sanitize_terminal_text(
            inspection.server_name,
            multiline=False,
            escape_invisible_formatting=True,
            escape_literal_backslashes=True,
        )
        + " ("
        + sanitize_terminal_text(
            inspection.transport,
            multiline=False,
            escape_invisible_formatting=True,
            escape_literal_backslashes=True,
        )
        + ")"
    )
    print(
        "Tool: "
        + sanitize_terminal_text(
            inspection.tool_name,
            multiline=False,
            escape_invisible_formatting=True,
            escape_literal_backslashes=True,
        )
    )
    if inspection.description:
        print(
            "Beschreibung: "
            + sanitize_terminal_text(
                inspection.description,
                multiline=True,
                escape_invisible_formatting=True,
                escape_literal_backslashes=True,
            )
        )
    print(f"Contract: {inspection.contract_sha256}")
    print("Input-Schema:")
    print(
        sanitize_terminal_text(
            json.dumps(
                inspection.input_schema,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            multiline=True,
            escape_invisible_formatting=True,
        )
    )
    if args.admin_command == "inspect-tool":
        return
    if args.update:
        if not inspection.trusted_server_found:
            raise ValueError("--update benötigt einen identitätsgleichen [[mcp.trusted_servers]]-Eintrag in der maschinenweiten Admin-Policy.")
        if inspection.existing_contract_sha256 is None:
            raise ValueError("--update benötigt bereits eine gepinnte Auto-Freigabe für dieses Tool.")
        print(f"Bisheriger Contract: {inspection.existing_contract_sha256}")
        if inspection.existing_contract_sha256 == inspection.contract_sha256:
            print("Der Tool-Contract ist unverändert.")
        else:
            print("Der Tool-Contract hat sich geändert. Prüfe Beschreibung und Schema vor einer erneuten Freigabe.")
    elif not inspection.trusted_server_found:
        print("Hinweis: In der aktuellen Admin-Policy existiert noch kein identitätsgleicher [[mcp.trusted_servers]]-Eintrag. Der folgende Contract kann vorbereitet werden, greift aber erst zusammen mit einer passenden administrativen Serveridentität.")
    trusted_server = next(
        (
            item
            for item in admin_config.mcp.trusted_servers
            if item.name == inspection.server_name and item.transport == inspection.transport
        ),
        None,
    )
    fragment_server = trusted_server if inspection.trusted_server_found and trusted_server is not None else server
    print("\nDie Admin-Konfiguration wurde NICHT geändert.")
    print("Prüfe Tool, Beschreibung und Schema und übernimm die folgenden Blöcke bei bewusster Freigabe manuell in die Admin-Policy.")
    print("\nPfad zur Admin-Konfiguration:")
    print(default_admin_config_file())
    print("\nBeispiel für den Trusted-Server-Eintrag:")
    print(
        sanitize_terminal_text(
            _render_trusted_server_fragment(fragment_server),
            multiline=True,
            escape_invisible_formatting=True,
        )
    )
    print("\nAuto-Approval für dieses Tool:")
    print(
        sanitize_terminal_text(
            _render_tool_approval_fragment(inspection),
            multiline=True,
            escape_invisible_formatting=True,
        )
    )


async def run(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    admin_config: AdminConfig = load_admin_config()
    config = apply_model_cli_override(config, model=args.model)
    config = apply_mcp_cli_overrides(config, os_access=args.os_access)

    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"Arbeitsordner existiert nicht: {workspace}")

    prompt_file_arg = getattr(args, "prompt_file", None)
    if prompt_file_arg is not None and args.prompt:
        raise ValueError("--prompt-file darf nicht zusammen mit einem positional Prompt verwendet werden.")

    file_contexts, prompt_file, output_target = prepare_file_options(
        workspace,
        context_files=tuple(getattr(args, "context_files", ()) or ()),
        prompt_file=prompt_file_arg,
        output=getattr(args, "output", None),
        overwrite_output=bool(getattr(args, "overwrite_output", False)),
    )
    raw_variables = tuple(getattr(args, "var", ()) or ())
    if raw_variables and prompt_file is None:
        raise ValueError("--var ist nur zusammen mit --prompt-file erlaubt.")

    if prompt_file is not None:
        template = PromptTemplate.parse(prompt_file.content)
        variable_values = parse_variable_assignments(raw_variables)
        unknown_variables = sorted(set(variable_values) - set(template.variables))
        if unknown_variables:
            raise ValueError(
                "Mit --var gesetzte Variable(n) kommen im Prompt-Template nicht vor: "
                + ", ".join(unknown_variables)
            )

        missing_variables = [
            name for name in template.variables if name not in variable_values
        ]
        if missing_variables and not sys.stdin.isatty():
            raise ValueError(
                "Prompt-Template benötigt interaktive Variablenwerte, aber stdin "
                "ist kein TTY. Fehlend: "
                + ", ".join(missing_variables)
            )
        for name in missing_variables:
            variable_values[name] = await asyncio.to_thread(
                input,
                f"Wert für Prompt-Variable {name}: ",
            )
        one_shot_prompt = template.render(variable_values)
        if not one_shot_prompt or one_shot_prompt.isspace():
            raise ValueError(
                "Gerenderter Prompt darf nicht leer sein oder nur aus Leerraum bestehen."
            )
    else:
        one_shot_prompt = " ".join(args.prompt) if args.prompt else None

    print(f"Arbeitsordner: {workspace}")
    print(f"Admin-Policy: {default_admin_config_file()}")
    print("MCP-Server: " + (", ".join(server.name for server in config.mcp_servers) or "(keine)"))
    print(f"Modell: {config.model.model} ({config.model.provider}, {config.model.base_url})")
    if config.logging.enabled:
        print(f"Logdatei: {config.logging.file}")
    if config.okf:
        print(f"OKF-Repository: {config.okf.repository}")
    for file_context in file_contexts:
        print(
            f"Context-Datei: {file_context.relative_path} "
            f"({len(file_context.content)} Zeichen)"
        )
    if prompt_file is not None:
        print(f"Prompt-Datei: {prompt_file.relative_path} ({len(prompt_file.content)} Zeichen)")
    if output_target is not None:
        print(f"Output-Datei: {output_target.path}")

    approval_callback = build_approval_callback(getattr(args, "approve_tool", ()))

    if one_shot_prompt is not None:
        result = await run_once(
            OneShotRunOptions(
                workspace=workspace,
                prompt=one_shot_prompt,
                config_file=args.config,
                model=args.model,
                workspace_access=args.os_access,
                response_format=args.response_format,
                add_web_context=tuple(getattr(args, "add_web_context", ()) or ()),
                approval_callback=approval_callback,
                prepared_file_contexts=file_contexts,
                prepared_output_target=output_target,
            ),
            dependencies=ExecutionDependencies(
                load_config=load_config,
                load_admin_config=load_admin_config,
                configure_logging=configure_logging,
                create_model_client=create_model_client,
                agent_type=WebContextCliAgent,
            ),
        )
        for status in result.web_context_statuses:
            print(sanitize_terminal_text(status, multiline=True))
        print(sanitize_terminal_text(result.answer, multiline=True))
        print(result.usage)
        return

    configure_logging(config.logging)
    model_client = create_model_client(
        config.model,
        network=admin_config.network,
        credential_rules=admin_config.model_credentials,
    )
    agent = WebContextCliAgent(
        workspace,
        model_client,
        config.mcp_servers,
        logging_config=config.logging,
        config_file=args.config or default_config_file(),
        dump_llm_context=config.dump_llm_context,
        network=admin_config.network,
        web_providers=admin_config.web.providers,
        mcp_policy=admin_config.mcp,
        approval_callback=approval_callback,
        okf=config.okf,
        file_contexts=file_contexts,
        response_format=args.response_format,
    )

    async with agent:
        for url in getattr(args, "add_web_context", ()):
            print(
                sanitize_terminal_text(
                    await agent.ask(f"add_web_context {url}"),
                    multiline=True,
                )
            )
        print("Interaktiver Modus; 'enable <server>' und 'disable <server>' steuern MCP-Server, 'add_web_context <url>' lädt Web-Kontext, 'clear_web_context' entfernt ihn, 'tokens' zeigt die Usage des letzten Agentenlaufs, 'exit' oder 'quit' beendet die Sitzung.")
        while True:
            try:
                prompt = input("\n> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if prompt.lower() in {"exit", "quit"}:
                return
            if not prompt:
                continue
            try:
                answer = await agent.ask(prompt)
                print(sanitize_terminal_text(answer, multiline=True))
                if output_target is not None and not is_local_agent_command(prompt):
                    output_target.write_text(answer)
            except Exception as exc:
                print_error(exc, debug=args.debug)


def main() -> None:
    raw_args = sys.argv[1:]
    try:
        if raw_args and raw_args[0] == "admin":
            args = build_admin_parser().parse_args(raw_args[1:])
            asyncio.run(run_admin(args))
        else:
            args = build_parser().parse_args(raw_args)
            asyncio.run(run(args))
    except Exception as exc:
        debug = bool(getattr(locals().get("args", None), "debug", False))
        print_error(exc, debug=debug)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
