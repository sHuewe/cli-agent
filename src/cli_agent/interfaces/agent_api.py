"""Local authenticated HTTP and AG-UI routes; session lifecycle lives separately."""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from cli_agent.interfaces.agent_api_sessions import (
    API_HOST, DEFAULT_PORT, MAX_SESSIONS, MAX_CONCURRENT_BROWSER_RUNS,
    MAX_PROMPT_BYTES, MAX_EVENTS_PER_RUN, REPLAY_EVENTS_PER_TOOL,
    REPLAY_LIFECYCLE_ALLOWANCE, MAX_JSON_BYTES, BROWSER_SSE_CONNECT_TIMEOUT,
    _artifact_thread_token, _artifact_thread_from_token,
    AgentRun, AgentSession, AgentApiService,
)

def create_app(service: AgentApiService, *, ui: dict[str, Any] | None = None) -> Any:
    """Create a loopback-only ASGI app. Browser mode shares the same API service."""
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
    from starlette.routing import Route
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    @asynccontextmanager
    async def lifespan(_app: Any):
        try:
            yield
        finally:
            await service.shutdown()

    @asynccontextmanager
    async def checked_json(request: Request):
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            raise ValueError("Content-Type application/json erforderlich.")
        declared_length = request.headers.get("content-length")
        if declared_length is not None:
            try:
                length = int(declared_length)
            except ValueError as exc:
                raise ValueError("Ungültiger Content-Length-Header.") from exc
            if length < 0:
                raise ValueError("Ungültiger Content-Length-Header.")
            if length > MAX_JSON_BYTES:
                raise ValueError("JSON-Anfrage zu groß.")

        # Content-Length is optional (for example with chunked encoding), and
        # client-provided length is not a trusted memory limit. Check the size
        # of each incoming chunk BEFORE appending it to the buffered JSON.
        chunks: list[bytes] = []
        total_bytes = 0
        async for chunk in request.stream():
            total_bytes += len(chunk)
            if total_bytes > MAX_JSON_BYTES:
                raise ValueError("JSON-Anfrage zu groß.")
            chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("Ungültiges JSON.") from exc
        if not isinstance(value, dict):
            raise ValueError("Ein JSON-Objekt wird benötigt.")
        yield value

    @asynccontextmanager
    async def session_from(request: Request):
        session = service.sessions.get(request.path_params["session_id"])
        if session is None:
            raise KeyError("Session nicht gefunden.")
        yield session

    from cli_agent.interfaces.api_routes import build_api_handlers
    from cli_agent.interfaces.browser_routes import build_browser_handlers
    api = build_api_handlers(service, ui, error, checked_json)
    browser = build_browser_handlers(service, ui, error, checked_json) if ui is not None else {}

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def authenticate(request: Request, call_next: Any) -> Response:
        # Reject rebinding / proxies even for public static assets.
        host = request.headers.get("host", "").split(":")[0].lower()
        if host != API_HOST or request.headers.get("x-forwarded-host") or request.headers.get("forwarded"):
            return error(403, "Unzulässiger Host.")
        # Use the ASGI-decoded path; request.url.path may treat decoded # or
        # ? characters as URL delimiters and truncate the real path.
        path = request.scope["path"]
        origin = request.headers.get("origin")
        if ui is not None and path in {"/", "/assets/app.css", "/assets/app.js"}:
            if request.method != "GET" or origin is not None:
                return error(403, "Unzulässige Browser-Anfrage.")
            return await call_next(request)
        header = request.headers.get("authorization", "")
        prefix = "Bearer "
        token = header[len(prefix):] if header.startswith(prefix) else ""
        browser = (
            ui is not None
            and secrets.compare_digest(token, ui["token"])
            and request.headers.get("x-cli-agent-browser") == "1"
            and ((origin is None or origin == ui["origin"])
                 if request.method in {"GET", "HEAD"}
                 else origin == ui["origin"])
            and (path.startswith("/ui/") or path.startswith("/sessions/")
                 or path.startswith("/ag-ui") or path.startswith("/threads/"))
        )
        if browser:
            if path.startswith("/threads/"):
                parts = path.split("/")
                thread_id = _artifact_thread_from_token(parts[2]) if len(parts) == 5 else None
                if (request.method != "GET" or thread_id is None
                        or parts[3] != "artifacts" or not parts[4]
                        or not ui["registry"].is_owner(
                            thread_id, request.headers.get("x-cli-agent-lease"))):
                    return error(403, "Keine gültige Tab-Bindung für den Download.")
            if path.startswith("/sessions/"):
                parts = path.split("/")
                if (len(parts) < 4 or request.method == "DELETE"
                        or not ui["registry"].is_owner(
                            parts[2], request.headers.get("x-cli-agent-lease"))):
                    return error(403, "Keine gültige Tab-Bindung für diese Session.")
            return await call_next(request)
        if origin is not None:
            return error(403, "Browser-Origin nicht zugelassen.")
        if not token or not secrets.compare_digest(token, service.token):
            return error(401, "Authentifizierung erforderlich.")
        return await call_next(request)

    app = Starlette(
        routes=([
            Route("/health", health, methods=["GET"]),
            Route("/ag-ui", api["ag_ui_run"], methods=["POST"]),
            Route("/ag-ui/capabilities", api["ag_ui_capabilities"], methods=["GET"]),
            Route("/threads/{thread_id}/artifacts/{artifact_id}", api["download_artifact"], methods=["GET"]),
            Route("/threads/{thread_id:path}", api["delete_agui_thread"], methods=["DELETE"]),
            Route("/sessions", api["create_session"], methods=["POST"]),
            Route("/sessions/{session_id}", api["delete_session"], methods=["DELETE"]),
            Route("/sessions/{session_id}/context", api["context"], methods=["GET"]),
            Route("/sessions/{session_id}/working-context", api["working_context"], methods=["GET"]),
            Route("/sessions/{session_id}/reset", api["reset_history"], methods=["POST"]),
            Route("/sessions/{session_id}/usage", api["usage"], methods=["GET"]),
            Route("/sessions/{session_id}/context", api["update_context"], methods=["PATCH"]),
        ] + ([
            Route("/", browser["ui_index"], methods=["GET"]),
            Route("/assets/app.css", browser["ui_css"], methods=["GET"]),
            Route("/assets/app.js", browser["ui_js"], methods=["GET"]),
            Route("/ui/bootstrap", browser["ui_bootstrap"], methods=["POST"]),
            Route("/ui/sessions", browser["ui_sessions"], methods=["GET"]),
            Route("/ui/sessions", browser["ui_session_new"], methods=["POST"]),
            Route("/ui/sessions/{session_id}/ticket", browser["ui_session_ticket"], methods=["POST"]),
            Route("/ui/sessions/{session_id}", browser["ui_session_delete"], methods=["DELETE"]),
            Route("/ui/release", browser["ui_release"], methods=["POST"]),
            Route("/ui/heartbeat", browser["ui_heartbeat"], methods=["POST"]),
            Route("/ui/reconnect/{session_id}/{run_id}", api["ui_reconnect_run"], methods=["GET"]),
            Route("/ui/render", browser["ui_render"], methods=["POST"]),
            Route("/ui/command", browser["ui_command"], methods=["POST"]),
        ] if ui is not None else [])),
        lifespan=lifespan,
        middleware=[Middleware(BaseHTTPMiddleware, dispatch=authenticate)],
    )
    return app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cli-agent-api", description="Local-only Agent API")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--model", default=None)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    access = parser.add_mutually_exclusive_group()
    access.add_argument("--with-os-read", action="store_const", const="read", dest="os_access")
    access.add_argument("--with-os-write", action="store_const", const="write", dest="os_access")
    parser.add_argument("--with-mcp", action="append", default=[], metavar="NAME")
    parser.add_argument("--with-data", action="store_true")
    parser.add_argument("--exclude-path", action="append", type=Path, default=[])
    parser.add_argument("--approve-tool", action="append", default=[], metavar="TOOL", help="Pre-approve one exact exposed tool name for this process run; repeat for multiple tools.")
    return parser


def build_service(args: argparse.Namespace) -> AgentApiService:
    from cli_agent.settings.admin_config import load_admin_config
    from cli_agent.settings.config import McpServerConfig, default_config_file, load_config
    from cli_agent.platform.execution import (
        apply_workspace_access_override, apply_data_override,
        apply_named_mcp_overrides, build_preapproval_callback,
        resolve_excluded_paths,
    )
    from cli_agent.context.file_context import ContextFileCliAgent
    from cli_agent.models.model_factory import create_model_client
    from cli_agent.platform.os_operations import Workspace
    from cli_agent.platform.logging_setup import configure_logging

    if not 1 <= args.port <= 65535:
        raise ValueError("Port muss im Bereich 1–65535 liegen.")
    workspace = args.workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError("Workspace existiert nicht.")
    if args.exclude_path and args.os_access not in {"read", "write"}:
        raise ValueError("--exclude-path benötigt OS-Zugriff.")

    config_file = args.config or default_config_file()
    config = load_config(args.config)
    admin = load_admin_config()
    if args.model is not None:
        from dataclasses import replace
        config = replace(config, model=replace(config.model, model=args.model))
    exclusions = resolve_excluded_paths(workspace, tuple(args.exclude_path))
    config = apply_workspace_access_override(
        config, workspace_access=args.os_access, excluded_paths=exclusions
    )
    config = apply_data_override(
        config, enabled=args.with_data,
        workspace_access=args.os_access, excluded_paths=exclusions
    )
    config = apply_named_mcp_overrides(
        config, admin_config=admin, server_names=args.with_mcp,
        workspace_access=args.os_access, workspace=workspace,
    )
    configure_logging(config.logging)
    approval_workspace = None
    if args.os_access == "write":
        approval_workspace = Workspace.from_directory(
            workspace, McpServerConfig(name="os", config={"allow_write_files": True}),
            protected_paths=(config_file, *exclusions),
        )

    def factory(session: AgentSession):
        model_client = create_model_client(
            config.model, network=admin.network,
            credential_rules=admin.model_credentials,
        )
        agent = ContextFileCliAgent(
            workspace, model_client, config.mcp_servers,
            logging_config=config.logging,
            agent_timezone=config.agent.timezone,
            config_file=config_file,
            # API sessions never write LLM context dumps, even when enabled
            # in config.toml for CLI or flow execution.
            dump_llm_context=False,
            network=admin.network,
            web_providers=admin.web.providers,
            mcp_policy=admin.mcp,
            approval_callback=build_preapproval_callback(
                getattr(args, "approve_tool", ()) or (),
                fallback=session.approve,
            ),
            user_message_callback=session.broker.show_mcp_user_message,
            okf=config.okf,
            knowledge_vector=config.knowledge_vector,
            model_credential_rules=admin.model_credentials,
            workspace_access=args.os_access,
        )
        agent.event_callback = session.on_agent_event
        session.broker.set_event_sender(session._send_user_message)
        return agent

    return AgentApiService(factory, approval_workspace=approval_workspace)


def main() -> None:
    import uvicorn
    args = build_parser().parse_args()
    service = build_service(args)
    # Service managers and desktop integrations often pipe stdout. Explicitly
    # flush before Uvicorn blocks so clients can read the API credential.
    print(f"cli-agent-api: http://{API_HOST}:{args.port} (nur localhost)", flush=True)
    print(f"Bearer-Token: {service.token}", flush=True)
    uvicorn.run(
        create_app(service), host=API_HOST, port=args.port,
        proxy_headers=False, access_log=False, server_header=False,
    )
