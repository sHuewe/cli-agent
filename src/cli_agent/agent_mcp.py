from __future__ import annotations

import json
import logging
import os
from contextlib import AsyncExitStack
from typing import Any, Self

import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .agent_knowledge import (
    EXPECTED_KNOWLEDGE_TOOLS,
    ServerConfig,
    ToolRoute,
    _RuntimeMcpServerConfig,
)
from .network_policy import validate_http_url

logger = logging.getLogger("cli_agent.agent_mcp")


class McpLifecycleMixin:
    async def set_server_enabled(self, server_name: str, *, enabled: bool) -> bool:
        """Enable or disable a server and close its resources when disabled."""
        if (
            server_name not in self._server_configs
            and server_name not in self._sessions
        ):
            raise ValueError(f"Unbekannter MCP-Server: {server_name}")

        was_enabled = server_name in self._active_servers
        if enabled:
            if not was_enabled:
                server_config = self._server_configs.get(server_name)
                if server_config is not None and self._exit_stack is not None:
                    await self._start_server(server_config, self._exit_stack)
                self._active_servers.add(server_name)
        else:
            if was_enabled:
                self._active_servers.discard(server_name)
                server_stack = self._server_stacks.pop(server_name, None)
                if server_stack is not None:
                    try:
                        await server_stack.aclose()
                    finally:
                        self._sessions.pop(server_name, None)
                        self._server_tools.pop(server_name, None)
                        self._server_instructions.pop(server_name, None)
                        self._tool_routes = {
                            name: route
                            for name, route in self._tool_routes.items()
                            if route[2].name != server_name
                        }
        messages = getattr(self, "messages", None)
        if isinstance(messages, list) and messages:
            first_message = messages[0]
            if (
                isinstance(first_message, dict)
                and first_message.get("role") == "system"
            ):
                first_message["content"] = self._build_system_prompt()
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
                await self._start_server(server_config, stack)
                self._active_servers.add(server_config.name)

            if self._okf_options is not None:
                await self._start_knowledge_server(stack)

            self.history = []
            system_prompt = self._build_system_prompt()
            if self.logging_config.log_model_messages:
                logger.info(
                    "System prompt length=%d: %s",
                    len(system_prompt),
                    system_prompt,
                )
                logger.info(
                    "Tools: %s", json.dumps(self._model_tools(), ensure_ascii=False)
                )
            self._exit_stack = stack
        except BaseException:
            await stack.aclose()
            self._sessions.clear()
            self._server_stacks.clear()
            self._server_configs.clear()
            self._tool_routes.clear()
            self._server_tools.clear()
            self._server_instructions.clear()
            self._active_servers.clear()
            self._knowledge_session = None
            self._knowledge_tools.clear()
            self._knowledge_routes.clear()
            self._knowledge_instructions = None
            self.history.clear()
            raise

    async def _start_server(
        self,
        server_config: ServerConfig,
        parent_stack: AsyncExitStack,
    ) -> None:
        if (
            not getattr(server_config, "built_in", False)
            and server_config.transport == "stdio"
            and getattr(server_config, "config", {}).get(
                "allow_untrusted_stdio",
                False,
            )
            is not True
        ):
            raise PermissionError(
                f"Der untrusted stdio-MCP-Server {server_config.name!r} wird "
                "nicht automatisch gestartet. Setze "
                "config.allow_untrusted_stdio=true nur für einen auditierten "
                "Prozess und verwende zusätzlich eine OS-/Container-Sandbox."
            )
        if server_config.name in self._sessions:
            raise RuntimeError(f"MCP-Server bereits verbunden: {server_config.name}")

        server_stack = AsyncExitStack()
        await server_stack.__aenter__()
        try:
            session, instructions = await self._connect_server(
                server_stack,
                server_config,
            )
            listed = await session.list_tools()
            tool_names: list[str] = []
            server_tools: list[dict[str, Any]] = []
            routes: dict[str, ToolRoute] = {}
            for tool in listed.tools:
                exposed_name = f"{server_config.name}__{tool.name}"
                if exposed_name in self._tool_routes:
                    raise RuntimeError(f"Doppelter Toolname: {exposed_name}")
                routes[exposed_name] = (session, tool.name, server_config)
                server_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": exposed_name,
                            "description": (
                                f"MCP-Server {server_config.name}: "
                                f"{tool.description or ''}"
                            ),
                            "parameters": tool.inputSchema,
                        },
                    }
                )
                tool_names.append(exposed_name)
        except BaseException:
            await server_stack.aclose()
            raise

        self._sessions[server_config.name] = session
        self._server_stacks[server_config.name] = server_stack
        self._server_configs[server_config.name] = server_config
        self._server_tools[server_config.name] = server_tools
        self._tool_routes.update(routes)
        if instructions:
            self._server_instructions[server_config.name] = instructions
        parent_stack.push_async_callback(server_stack.aclose)
        logger.info(
            "mcp_server_connected name=%s transport=%s tools=%s",
            server_config.name,
            server_config.transport,
            json.dumps(tool_names, ensure_ascii=False),
        )

    async def _start_knowledge_server(self, stack: AsyncExitStack) -> None:
        options = self._okf_options
        if options is None:
            return

        if not options.repository.is_dir():
            error = ValueError(
                f"Konfiguriertes OKF-Repository existiert nicht oder ist kein "
                f"Verzeichnis: {options.repository}"
            )
            if options.required:
                raise error
            logger.error("knowledge_server_disabled reason=%s", error)
            return

        server_config = _RuntimeMcpServerConfig(
            name="okf",
            command="{python}",
            args=(
                "-m",
                "cli_agent.okf_mcp_server.server",
                "--project-directory",
                str(options.repository),
                "--max-read-bytes",
                str(options.max_read_bytes),
                "--max-index-entries",
                str(options.max_index_entries),
                "--config-file",
                str(self.config_file),
            ),
            compress_result=True,
            compress_min_chars=options.compress_min_chars,
        )

        knowledge_stack = AsyncExitStack()
        await knowledge_stack.__aenter__()
        try:
            session, instructions = await self._connect_server(
                knowledge_stack,
                server_config,
            )
            listed = await session.list_tools()
            available_names = {tool.name for tool in listed.tools}
            if available_names != EXPECTED_KNOWLEDGE_TOOLS:
                raise RuntimeError(
                    "Der OKF-MCP-Server muss exakt die Tools "
                    f"{sorted(EXPECTED_KNOWLEDGE_TOOLS)} anbieten; erhalten: "
                    f"{sorted(available_names)}."
                )

            knowledge_tools: list[dict[str, Any]] = []
            knowledge_routes: dict[str, ToolRoute] = {}
            for tool in listed.tools:
                exposed_name = f"okf__{tool.name}"
                knowledge_routes[exposed_name] = (
                    session,
                    tool.name,
                    server_config,
                )
                knowledge_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": exposed_name,
                            "description": (
                                "Read-only OKF knowledge tool: "
                                f"{tool.description or ''}"
                            ),
                            "parameters": tool.inputSchema,
                        },
                    }
                )
        except BaseException as exc:
            await knowledge_stack.aclose()
            logger.error(
                "knowledge_server_start_failed error_type=%s",
                type(exc).__name__,
            )
            if not isinstance(exc, Exception):
                raise
            if options.required:
                raise RuntimeError(
                    "Der konfigurierte OKF-Wissensserver konnte nicht gestartet werden."
                ) from exc
            logger.error(
                "knowledge_server_start_failed_optional error_type=%s",
                type(exc).__name__,
            )
            return

        stack.push_async_callback(knowledge_stack.aclose)
        self._knowledge_session = session
        self._knowledge_tools = knowledge_tools
        self._knowledge_routes = knowledge_routes
        self._knowledge_instructions = instructions
        logger.info(
            "knowledge_server_connected repository=%s tools=%s",
            options.repository,
            json.dumps(
                [tool["function"]["name"] for tool in knowledge_tools],
                ensure_ascii=False,
            ),
        )

    @staticmethod
    def _stdio_environment() -> dict[str, str]:
        """Pass only process essentials; secrets require explicit configuration."""
        safe_names = (
            "PATH",
            "HOME",
            "USERPROFILE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "LANG",
            "LC_ALL",
            "PYTHONIOENCODING",
        )
        environment = {
            name: os.environ[name]
            for name in safe_names
            if os.environ.get(name) is not None
        }
        return environment

    async def _connect_server(
        self,
        stack: AsyncExitStack,
        server_config: ServerConfig,
    ) -> tuple[ClientSession, str | None]:
        if server_config.transport == "stdio":
            if server_config.command is None:
                raise ValueError(
                    f"stdio-MCP-Server {server_config.name!r} ohne command."
                )
            environment = self._stdio_environment()
            environment.update(
                {key: self._resolve(value) for key, value in server_config.env.items()}
            )
            parameters = StdioServerParameters(
                command=self._resolve(server_config.command),
                args=[self._resolve(value) for value in server_config.args],
                env=environment,
            )
            read_stream, write_stream = await stack.enter_async_context(
                stdio_client(parameters)
            )
        elif server_config.transport == "streamable_http":
            if server_config.url is None:
                raise ValueError(f"HTTP-MCP-Server {server_config.name!r} ohne URL.")
            server_url = validate_http_url(
                self._resolve(server_config.url),
                allowed_hosts=self.network.mcp_allowed_hosts,
                purpose="MCP-Server",
            )
            http_client = await stack.enter_async_context(
                httpx.AsyncClient(
                    headers={
                        key: self._resolve(value)
                        for key, value in server_config.headers.items()
                    },
                    follow_redirects=False,
                    trust_env=False,
                )
            )
            connection = await stack.enter_async_context(
                streamable_http_client(
                    server_url,
                    http_client=http_client,
                )
            )
            read_stream, write_stream, _ = connection
        else:
            raise ValueError(
                f"Nicht unterstützter MCP-Transport: {server_config.transport!r}"
            )

        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        initialize_result = await session.initialize()
        return session, initialize_result.instructions

    async def close(self) -> None:
        if self._exit_stack is None:
            return
        stack = self._exit_stack
        self._exit_stack = None
        self._sessions.clear()
        self._server_stacks.clear()
        self._server_configs.clear()
        self._tool_routes.clear()
        self._server_tools.clear()
        self._server_instructions.clear()
        self._active_servers.clear()
        self._knowledge_session = None
        self._knowledge_tools.clear()
        self._knowledge_routes.clear()
        self._knowledge_instructions = None
        self.history.clear()
        await stack.aclose()
