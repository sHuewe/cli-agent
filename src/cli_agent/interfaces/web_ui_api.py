# API-backed variant of the existing Web-UI JavaScript.
# The browser talks only to the Starlette API endpoints and never to agent.ask().
from importlib.resources import files

# Packaged browser code; keep the module-level API_APP_JS contract for callers.
API_APP_JS = files("cli_agent.interfaces").joinpath("static", "app.js").read_text(encoding="utf-8")



async def run_web_ui_via_api(
    *,
    args,
    config,
    admin_config,
    workspace,
    file_contexts,
    output_target,
    excluded_paths,
):
    """Keep cli-agent --with-web-ui a one-command localhost launch.

    Uvicorn and the session worker run in one process. All agent operations
    execute on the AgentSession worker task, including MCP enter/exit.
    """
    import asyncio
    import secrets
    import socket
    import webbrowser

    import uvicorn

    from cli_agent.interfaces.agent_api import API_HOST, AgentApiService, create_app
    from cli_agent.interfaces.cli import build_preapproval_callback
    from cli_agent.settings.config import McpServerConfig, default_config_file
    from cli_agent.context.file_context import ContextFileCliAgent
    from cli_agent.models.model_factory import create_model_client
    from cli_agent.platform.logging_setup import configure_logging
    from cli_agent.platform.os_operations import Workspace
    from cli_agent.interfaces.terminal_output import sanitize_terminal_text

    configure_logging(config.logging)
    config_file = args.config or default_config_file()
    exclusions = tuple(excluded_paths)
    preview_workspace = (
        Workspace.from_directory(
            workspace,
            McpServerConfig(name="os", config={"allow_write_files": True}),
            protected_paths=(config_file, *exclusions),
            mutation_protected_paths=tuple(
                item.source_path for item in file_contexts
            ),
        )
        if args.os_access == "write" else None
    )

    def factory(session):
        client = create_model_client(
            config.model, network=admin_config.network,
            credential_rules=admin_config.model_credentials,
        )
        agent = ContextFileCliAgent(
            workspace, client, config.mcp_servers,
            logging_config=config.logging,
            agent_timezone=config.agent.timezone,
            config_file=config_file,
            dump_llm_context=False,
            network=admin_config.network,
            web_providers=admin_config.web.providers,
            mcp_policy=admin_config.mcp,
            approval_callback=build_preapproval_callback(
                getattr(args, "approve_tool", ()) or (),
                fallback=session.approve,
            ),
            user_message_callback=session.broker.show_mcp_user_message,
            okf=config.okf,
            knowledge_vector=config.knowledge_vector,
            model_credential_rules=admin_config.model_credentials,
            file_contexts=tuple(file_contexts),
            response_format=getattr(args, "response_format", "text"),
            workspace_access=args.os_access,
        )
        agent.event_callback = session.on_agent_event
        session.broker.set_event_sender(session._send_user_message)
        return agent

    service = AgentApiService(
        factory,
        approval_workspace=preview_workspace,
        output_target=output_target,
        debug_errors=bool(getattr(args, 'debug', False)),
        initial_commands=tuple(
            f"add_web_context {url}"
            for url in getattr(args, "add_web_context", ()) or ()
        ),
    )

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((API_HOST, 0))
        sock.listen(128)
        sock.setblocking(False)
        port = int(sock.getsockname()[1])
        origin = f"http://{API_HOST}:{port}"
        session = await service.create()
        from cli_agent.interfaces.web_ui_sessions import BrowserSessionRegistry
        registry = BrowserSessionRegistry(service, session.id)
        browser_token = secrets.token_urlsafe(32)
        ui = {
            "registry": registry,
            "token": browser_token,
            "origin": origin,
            "session_id": session.id,
            "info": {
                "workspace": str(workspace),
                "model": config.model.model,
                "mcp_servers": [item.name for item in config.mcp_servers],
            },
        }
        app = create_app(service, ui=ui)
        server = uvicorn.Server(uvicorn.Config(
            app, host=API_HOST, port=port,
            log_level="warning", access_log=False,
            proxy_headers=False, server_header=False,
        ))
        ui["shutdown"] = lambda: setattr(server, "should_exit", True)
        print(sanitize_terminal_text(
            f"Web-UI: {origin}/ (nur localhost)",
            multiline=False, escape_invisible_formatting=True,
        ))
        print("Web-UI Access Token:", browser_token)
        print("Zum Beenden des Servers Strg+C im Terminal verwenden.")
        server_task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started and not server_task.done():
            await asyncio.sleep(0.01)
        if server_task.done():
            await server_task
            return
        await asyncio.to_thread(webbrowser.open, f"{origin}/#token={browser_token}", new=2)
        await server_task
    finally:
        await service.shutdown()
        sock.close()
