from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from contextlib import AsyncExitStack
from typing import Any, Self

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from cli_agent.mcp_path_policy import write_path_policy
from cli_agent.agent_knowledge import EXPECTED_KNOWLEDGE_TOOLS
from cli_agent.agent_types import ServerConfig, ToolRoute, _RuntimeMcpServerConfig
from cli_agent.config import McpServerConfig
from cli_agent.evidence_tools import PLAN_TOOL_NAME, ADD_EVIDENCE_TOOL_NAME, UPDATE_EVIDENCE_TOOL_NAME
from cli_agent.artifact_publication import PUBLISH_TOOL_NAME
from cli_agent.mcp_limits import (
    MCP_INITIALIZE_TIMEOUT_SECONDS,
    MCP_LIST_TOOLS_TIMEOUT_SECONDS,
    await_mcp_operation,
)
from cli_agent.mcp_schema_guard import validate_mcp_server_metadata
from cli_agent.network_policy import validate_http_url
from cli_agent.workspace_access import (
    WORKSPACE_ACCESS_ENV,
    WORKSPACE_DIRECTORY_ENV,
    is_reserved_workspace_environment_name,
    workspace_access_allows,
)

logger = logging.getLogger("cli_agent.agent_mcp")

# Notification delivery is best-effort and must not delay a cancelled run.
MCP_CANCEL_NOTIFY_TIMEOUT_SECONDS = 0.5


class CancellableClientSession(ClientSession):
    """Send MCP cancellation notifications for locally cancelled requests.

    The upstream SDK's send_request() drops its response waiter on cancellation
    but does not inform the server. Capture the ID before calling the base
    method (which assigns it synchronously before its first await).
    This is best-effort: remote MCP servers may ignore cancellations.
    """

    async def send_request(self, request, result_type, request_read_timeout_seconds=None,
                           metadata=None, progress_callback=None):
        import asyncio
        from mcp import types

        request_id = self._request_id
        try:
            return await super().send_request(
                request, result_type,
                request_read_timeout_seconds=request_read_timeout_seconds,
                metadata=metadata, progress_callback=progress_callback,
            )
        except asyncio.CancelledError:
            import anyio
            with anyio.move_on_after(MCP_CANCEL_NOTIFY_TIMEOUT_SECONDS, shield=True) as scope:
                try:
                    await self.send_notification(
                        types.ClientNotification(types.CancelledNotification(
                            params=types.CancelledNotificationParams(
                                requestId=request_id,
                                reason="Agent run was cancelled",
                            )
                        )),
                        related_request_id=request_id,
                    )
                except Exception:
                    logger.warning(
                        "mcp_cancel_notification_failed request_id=%s",
                        request_id, exc_info=True,
                    )
            if scope.cancel_called:
                logger.warning("mcp_cancel_notification_timeout request_id=%s", request_id)
            raise



class McpLifecycleMixin:
    async def set_server_enabled(self, server_name: str, *, enabled: bool) -> bool:
        if server_name not in self._server_configs and server_name not in self._sessions:
            raise ValueError(f"Unbekannter MCP-Server: {server_name}")
        was_enabled = server_name in self._active_servers
        if enabled:
            if not was_enabled:
                server_config = self._server_configs.get(server_name)
                # The in-process knowledge reentry adapter has no stdio/HTTP
                # connection to restart. Its route remains registered while
                # disabled; only the active-server flag needs to change.
                if server_name == "knowledge_reentry":
                    if (
                        server_config is None
                        or not getattr(server_config, "built_in", False)
                        or "knowledge_reentry__request_additional_knowledge" not in self._tool_routes
                    ):
                        raise RuntimeError("Interner Knowledge-Reentry-Adapter ist nicht verfügbar.")
                elif server_config is not None and self._exit_stack is not None:
                    started = await self._start_server(server_config, self._exit_stack)
                    if started is False:
                        return False
                self._active_servers.add(server_name)
        elif was_enabled:
            self._active_servers.discard(server_name)
            server_stack = self._server_stacks.pop(server_name, None)
            if server_stack is not None:
                try:
                    await server_stack.aclose()
                finally:
                    self._sessions.pop(server_name, None)
                    self._server_tools.pop(server_name, None)
                    self._server_instructions.pop(server_name, None)
                    self._server_untrusted_instructions.pop(server_name, None)
                    self._tool_routes = {name: route for name, route in self._tool_routes.items() if route[2].name != server_name}
        messages = getattr(self, "messages", None)
        if isinstance(messages, list) and messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            messages[0]["content"] = self._build_system_prompt()
        logger.info("mcp_server_enabled name=%s enabled=%s", server_name, enabled)
        return was_enabled != enabled

    async def enable_server(self, server_name: str) -> bool:
        return await self.set_server_enabled(server_name, enabled=True)

    async def disable_server(self, server_name: str) -> bool:
        return await self.set_server_enabled(server_name, enabled=False)

    async def __aenter__(self) -> Self:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        await self.close()

    async def start(self) -> None:
        if self._exit_stack is not None:
            return
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            for server_config in self.mcp_servers:
                started = await self._start_server(server_config, stack)
                if started is not False:
                    self._active_servers.add(server_config.name)
            if self._okf_options is not None:
                await self._start_knowledge_server(stack)
            if self._vector_config is not None:
                await self._start_vector_knowledge()
            if self._knowledge_session is not None or self._vector_session is not None:
                self._register_knowledge_reentry_tool()
            self.history = []
            system_prompt = self._build_system_prompt()
            if self.logging_config.log_model_messages:
                logger.info("System prompt length=%d: %s", len(system_prompt), system_prompt)
                logger.info("Tools: %s", json.dumps(self._model_tools(), ensure_ascii=False))
            self._exit_stack = stack
        except BaseException:
            await stack.aclose()
            self._sessions.clear(); self._server_stacks.clear(); self._server_configs.clear(); self._tool_routes.clear(); self._server_tools.clear(); self._server_instructions.clear(); self._server_untrusted_instructions.clear(); self._active_servers.clear()
            if self._vector_session is not None:
                self._vector_session.close()
                self._vector_session = None
            self._knowledge_session = None; self._knowledge_tools.clear(); self._knowledge_routes.clear(); self._knowledge_instructions = None; self.history.clear()
            raise

    def _resolve_external_stdio_server(self, server_config: ServerConfig) -> ServerConfig:
        if getattr(server_config, "built_in", False) or server_config.transport != "stdio":
            return server_config
        trusted = next(
            (
                item
                for item in self.mcp_policy.trusted_servers
                if item.name == server_config.name and item.transport == "stdio"
            ),
            None,
        )
        if trusted is None:
            raise PermissionError(
                f"Der externe stdio-MCP-Server {server_config.name!r} ist nicht in "
                "[[mcp.trusted_servers]] der maschinenweiten Admin-Policy definiert."
            )
        return McpServerConfig(
            name=trusted.name,
            transport="stdio",
            command=trusted.command,
            args=trusted.args,
            env=dict(trusted.env),
            required_workspace_access=trusted.required_workspace_access,
        )

    async def _start_server(self, server_config: ServerConfig, parent_stack: AsyncExitStack) -> bool:
        server_config = self._resolve_external_stdio_server(server_config)
        required_access = getattr(
            server_config,
            "required_workspace_access",
            "none",
        )
        if not workspace_access_allows(self.workspace_access, required_access):
            self._server_configs[server_config.name] = server_config
            logger.info(
                "mcp_server_workspace_access_blocked name=%s required=%s granted=%s",
                server_config.name,
                required_access,
                self.workspace_access,
            )
            return False
        if server_config.name in self._sessions:
            raise RuntimeError(f"MCP-Server bereits verbunden: {server_config.name}")
        server_stack = AsyncExitStack(); await server_stack.__aenter__()
        try:
            session, instructions = await self._connect_server(server_stack, server_config)
            listed = await await_mcp_operation(
                session.list_tools(),
                timeout_seconds=MCP_LIST_TOOLS_TIMEOUT_SECONDS,
                operation=f"MCP-Server {server_config.name!r}: list_tools",
            )
            validate_mcp_server_metadata(
                server_name=server_config.name,
                instructions=instructions,
                tools=list(listed.tools),
            )
            native_names: set[str] = set()
            tool_names: list[str] = []; server_tools: list[dict[str, Any]] = []; routes: dict[str, ToolRoute] = {}; identities: dict[str, tuple[str, str]] = {}
            for tool in listed.tools:
                if tool.name in native_names:
                    raise RuntimeError(f"MCP-Server {server_config.name!r} bietet Tool {tool.name!r} mehrfach an.")
                native_names.add(tool.name)
                exposed_name = f"{server_config.name}__{tool.name}"
                # This name is handled inside the main model loop, not by
                # MCP routing. Reject any MCP spelling that would shadow it
                # before committing routes, tool definitions or identity state.
                if exposed_name in {PLAN_TOOL_NAME, ADD_EVIDENCE_TOOL_NAME, UPDATE_EVIDENCE_TOOL_NAME, PUBLISH_TOOL_NAME}:
                    raise RuntimeError(
                        f"MCP-Toolname {exposed_name!r} ist für ein "
                        "internes Agent-Tool reserviert."
                    )
                identity = (server_config.name, tool.name)
                known_identity = self._tool_identities.get(exposed_name)
                if known_identity is not None and known_identity != identity:
                    raise RuntimeError(
                        f"MCP-Toolname {exposed_name!r} wurde in diesem Agent-Lauf "
                        f"bereits für {known_identity[0]!r}/{known_identity[1]!r} "
                        f"verwendet und darf nicht für {identity[0]!r}/{identity[1]!r} "
                        "wiederverwendet werden."
                    )
                if exposed_name in self._tool_routes:
                    raise RuntimeError(f"Doppelter Toolname: {exposed_name}")
                identities[exposed_name] = identity
                routes[exposed_name] = (session, tool.name, server_config)
                server_tools.append({"type": "function", "function": {"name": exposed_name, "description": f"MCP-Server {server_config.name}: {tool.description or ''}", "parameters": tool.inputSchema}})
                tool_names.append(exposed_name)
        except BaseException:
            await server_stack.aclose(); raise
        self._tool_identities.update(identities)
        self._sessions[server_config.name] = session; self._server_stacks[server_config.name] = server_stack; self._server_configs[server_config.name] = server_config; self._server_tools[server_config.name] = server_tools; self._tool_routes.update(routes)
        if instructions and self._instructions_are_trusted(server_config):
            self._server_instructions[server_config.name] = instructions
        elif instructions:
            self._server_untrusted_instructions[server_config.name] = instructions
            logger.info("mcp_server_instructions_untrusted name=%s", server_config.name)
        parent_stack.push_async_callback(server_stack.aclose)
        logger.info("mcp_server_connected name=%s transport=%s tools=%s", server_config.name, server_config.transport, json.dumps(tool_names, ensure_ascii=False))
        return True

    async def _start_knowledge_server(self, stack: AsyncExitStack) -> None:
        options = self._okf_options
        if options is None: return
        if not options.repository.is_dir():
            error = ValueError(f"Konfiguriertes OKF-Repository existiert nicht oder kein Verzeichnis: {options.repository}")
            if options.required: raise error
            logger.error("knowledge_server_disabled reason=%s", error); return
        server_config = _RuntimeMcpServerConfig(name="okf", command="{python}", args=("-m", "cli_agent.okf_mcp_server.server", "--project-directory", str(options.repository), "--max-read-bytes", str(options.max_read_bytes), "--max-index-entries", str(options.max_index_entries), "--config-file", str(self.config_file)), compress_result=True, compress_min_chars=options.compress_min_chars)
        knowledge_stack = AsyncExitStack(); await knowledge_stack.__aenter__()
        try:
            session, instructions = await self._connect_server(knowledge_stack, server_config)
            listed = await await_mcp_operation(
                session.list_tools(),
                timeout_seconds=MCP_LIST_TOOLS_TIMEOUT_SECONDS,
                operation=f"MCP-Server {server_config.name!r}: list_tools",
            )
            validate_mcp_server_metadata(
                server_name=server_config.name,
                instructions=instructions,
                tools=list(listed.tools),
            )
            available_names = {tool.name for tool in listed.tools}
            if available_names != EXPECTED_KNOWLEDGE_TOOLS:
                raise RuntimeError(f"Der OKF-MCP-Server muss exakt die Tools {sorted(EXPECTED_KNOWLEDGE_TOOLS)} anbieten; erhalten: {sorted(available_names)}.")
            knowledge_tools: list[dict[str, Any]] = []; knowledge_routes: dict[str, ToolRoute] = {}
            for tool in listed.tools:
                exposed_name = f"okf__{tool.name}"; knowledge_routes[exposed_name] = (session, tool.name, server_config)
                knowledge_tools.append({"type": "function", "function": {"name": exposed_name, "description": f"Read-only OKF knowledge tool: {tool.description or ''}", "parameters": tool.inputSchema}})
        except BaseException as exc:
            await knowledge_stack.aclose(); logger.error("knowledge_server_start_failed error_type=%s", type(exc).__name__)
            if not isinstance(exc, Exception): raise
            if options.required: raise RuntimeError("Der konfigurierte OKF-Wissensserver konnte nicht gestartet werden.") from exc
            return
        stack.push_async_callback(knowledge_stack.aclose); self._knowledge_session = session; self._knowledge_tools = knowledge_tools; self._knowledge_routes = knowledge_routes; self._knowledge_instructions = instructions
        logger.info("knowledge_server_connected repository=%s tools=%s", options.repository, json.dumps([tool["function"]["name"] for tool in knowledge_tools], ensure_ascii=False))

    async def _start_vector_knowledge(self) -> None:
        config = self._vector_config
        if config is None:
            return
        try:
            if config.mode == "okf" and self._knowledge_session is None:
                raise RuntimeError("Hybrid-RAG erfordert einen verfügbaren OKF-Server.")
            root = source_root(
                config,
                okf_root=self._okf_options.repository if self._okf_options else None,
                config_file=self.config_file,
            )
            path = db_path(config, config_file=self.config_file)
            session = VectorKnowledgeSession(
                config, root=root, path=path,
                network=self.network, credential_rules=self._model_credential_rules,
                okf_max_read_bytes=self._okf_options.max_read_bytes,
            )
            self._vector_session = session
            runtime = _RuntimeMcpServerConfig(name="vector", built_in=True)
            search_tool = {"type": "function", "function": {
                "name": "vector__knowledge_search",
                "description": "Search the indexed knowledge source for relevant document candidates. Read returned paths before selecting them.",
                "parameters": {"type": "object", "properties": {
                    "query": {"type": "string", "minLength": 1, "maxLength": 2000}
                }, "required": ["query"], "additionalProperties": False},
            }}
            self._knowledge_tools.append(search_tool)
            self._knowledge_routes["vector__knowledge_search"] = (session, "knowledge_search", runtime)
        except Exception:
            if config.required:
                raise
            logger.exception("vector_knowledge_disabled")
            self._vector_session = None

    def _register_knowledge_reentry_tool(self) -> None:
        from types import SimpleNamespace

        if "knowledge_reentry" in self._server_configs or "knowledge_reentry" in self._server_tools:
            raise RuntimeError("Der interne MCP-Servername knowledge_reentry ist bereits belegt.")

        class _ReentrySession:
            def __init__(self, agent):
                self.agent = agent

            async def call_tool(self, name, arguments):
                if name != "request_additional_knowledge":
                    return SimpleNamespace(structuredContent={"error": "Unbekanntes Tool"}, content=[], isError=True)
                return await self.agent._request_additional_knowledge(arguments["question"])

        server = _RuntimeMcpServerConfig(name="knowledge_reentry", built_in=True)
        exposed = "knowledge_reentry__request_additional_knowledge"
        tool = {"type": "function", "function": {
            "name": exposed,
            "description": "Nur wenn während der Hauptaufgabe wirklich neue, erforderliche Wissenslücken auftreten: ergänzendes projektspezifisches Knowledge recherchieren. Nicht zur Bestätigung bereits bekannter Fakten oder für wiederholte Fragen verwenden. Maximal zweimal pro Benutzeranfrage.",
            "parameters": {"type": "object", "properties": {
                "question": {"type": "string", "minLength": 1, "maxLength": 2000}
            }, "required": ["question"], "additionalProperties": False},
        }}
        self._server_configs[server.name] = server
        self._server_tools[server.name] = [tool]
        self._tool_routes[exposed] = (_ReentrySession(self), "request_additional_knowledge", server)
        self._active_servers.add(server.name)

    @staticmethod
    def _stdio_environment(*, built_in: bool = False) -> dict[str, str]:
        safe_names = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONIOENCODING")
        environment = {name: os.environ[name] for name in safe_names if os.environ.get(name) is not None}
        environment["PYTHONSAFEPATH"] = "1"
        return environment

    def _http_headers(self, server_config: ServerConfig) -> dict[str, str]:
        if any(key.strip().casefold() == "authorization" for key in server_config.headers):
            raise ValueError(
                f"HTTP-MCP-Server {server_config.name!r} darf Authorization nicht "
                "statisch konfigurieren. Bearer-Authentifizierung wird "
                "ausschließlich aus der Admin-Policy materialisiert."
            )
        headers = {
            key: self._resolve_http_value(value)
            for key, value in server_config.headers.items()
        }
        bearer_token_env = self._http_bearer_token_env(server_config)
        if bearer_token_env is None:
            return headers

        token = os.environ.get(bearer_token_env)
        if not token:
            raise ValueError(
                f"Für HTTP-MCP-Server {server_config.name!r} fehlt die "
                f"konfigurierte Bearer-Token-Umgebungsvariable {bearer_token_env!r}."
            )
        headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _connect_server(self, stack: AsyncExitStack, server_config: ServerConfig) -> tuple[ClientSession, str | None]:
        if server_config.transport == "stdio":
            if server_config.command is None: raise ValueError(f"stdio-MCP-Server {server_config.name!r} ohne command.")
            built_in = bool(getattr(server_config, "built_in", False))
            environment = self._stdio_environment(built_in=built_in)
            environment.update(
                {
                    key: self._resolve_stdio_value(value)
                    for key, value in server_config.env.items()
                    if built_in or not is_reserved_workspace_environment_name(key)
                }
            )
            if not built_in:
                environment[WORKSPACE_ACCESS_ENV] = self.workspace_access
                environment.pop(WORKSPACE_DIRECTORY_ENV, None)
                if self.workspace_access in {"read", "write"}:
                    environment[WORKSPACE_DIRECTORY_ENV] = str(
                        self.workspace_directory
                    )
            literal_args = list(getattr(server_config, "literal_args", ()))
            if built_in and server_config.name in {"os", "data"}:
                # Keep checkpoint lists off the Windows process command line.
                # The manifest lives in a private temporary directory outside
                # the workspace and is removed when the MCP session closes.
                paths = {"protected_paths": [], "mutation_protected_paths": [], "allowed_paths": []}
                filtered_args = []
                index = 0
                while index < len(literal_args):
                    argument = literal_args[index]
                    if argument in {"--protected-path", "--mutation-protected-path"}:
                        if index + 1 >= len(literal_args):
                            raise ValueError("Fehlender Wert für MCP-Schutzpfad.")
                        key = (
                            "protected_paths" if argument == "--protected-path"
                            else "mutation_protected_paths"
                        )
                        paths[key].append(literal_args[index + 1])
                        index += 2
                    else:
                        filtered_args.append(argument)
                        index += 1
                literal_args = filtered_args
                if server_config.name == "data" and hasattr(self, "_result_artifacts"):
                    paths["allowed_paths"].append({
                        "path": str(self._result_artifacts.session_directory),
                        "access": "write" if self.workspace_access == "write" else "read",
                    })
                if any(paths.values()):
                    temp_directory = stack.enter_context(
                        tempfile.TemporaryDirectory(prefix="cli-agent-mcp-policy-")
                    )
                    manifest_path = Path(temp_directory) / "paths.json"
                    write_path_policy(manifest_path, paths)
                    literal_args.extend(("--path-policy-file", str(manifest_path)))
            parameters = StdioServerParameters(
                command=self._resolve_stdio_value(server_config.command),
                args=[
                    self._resolve_stdio_value(value)
                    for value in server_config.args
                ] + literal_args,
                env=environment,
            )
            read_stream, write_stream = await stack.enter_async_context(stdio_client(parameters))
        elif server_config.transport == "streamable_http":
            if server_config.url is None: raise ValueError(f"HTTP-MCP-Server {server_config.name!r} ohne URL.")
            server_url = validate_http_url(
                self._resolve_http_value(server_config.url),
                allowed_hosts=self.network.mcp_allowed_hosts,
                purpose="MCP-Server",
            )
            headers = self._http_headers(server_config)
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers=headers,
                    follow_redirects=False,
                    trust_env=False,
                )
            )
            connection = await stack.enter_async_context(streamable_http_client(server_url, http_client=http_client)); read_stream, write_stream, _ = connection
        else: raise ValueError(f"Nicht unterstützter MCP-Transport: {server_config.transport!r}")
        session = await stack.enter_async_context(CancellableClientSession(read_stream, write_stream))
        initialize_result = await await_mcp_operation(
            session.initialize(),
            timeout_seconds=MCP_INITIALIZE_TIMEOUT_SECONDS,
            operation=f"MCP-Server {server_config.name!r}: initialize",
        )
        return session, initialize_result.instructions

    async def close(self) -> None:
        if self._exit_stack is None:
            self._result_artifacts.cleanup()
            return
        stack = self._exit_stack; self._exit_stack = None
        try:
            if self._vector_session is not None:
                self._vector_session.close()
                self._vector_session = None
            self._sessions.clear(); self._server_stacks.clear(); self._server_configs.clear(); self._tool_routes.clear(); self._server_tools.clear(); self._server_instructions.clear(); self._server_untrusted_instructions.clear(); self._active_servers.clear(); self._knowledge_session = None; self._knowledge_tools.clear(); self._knowledge_routes.clear(); self._knowledge_instructions = None; self.history.clear()
            await stack.aclose()
        finally:
            self._result_artifacts.cleanup()
