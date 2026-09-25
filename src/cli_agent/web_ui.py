from __future__ import annotations

import asyncio
import importlib
import json
import secrets
import socket
import traceback
import webbrowser
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from .approval_display import approval_arguments
from .file_context import OutputTarget, is_local_agent_command
from .terminal_output import sanitize_terminal_text

WEB_UI_HOST = "127.0.0.1"
MAX_PROMPT_BYTES = 512 * 1024
MAX_WS_MESSAGE_BYTES = 1024 * 1024

_WEB_EXTRA_ERROR = (
    "Die Web-UI ist nicht installiert. Installiere cli-agent mit dem optionalen "
    "Extra 'web', z. B. mit: pipx install 'cli-agent[web]'"
)


def _load_web_dependencies() -> dict[str, Any]:
    try:
        starlette_applications = importlib.import_module("starlette.applications")
        starlette_middleware = importlib.import_module("starlette.middleware")
        starlette_responses = importlib.import_module("starlette.responses")
        starlette_routing = importlib.import_module("starlette.routing")
        trustedhost = importlib.import_module("starlette.middleware.trustedhost")
        uvicorn = importlib.import_module("uvicorn")
        importlib.import_module("websockets")
    except ModuleNotFoundError as exc:
        if (exc.name or "").split(".", 1)[0] in {
            "starlette",
            "uvicorn",
            "websockets",
        }:
            raise RuntimeError(_WEB_EXTRA_ERROR) from exc
        raise

    return {
        "Starlette": starlette_applications.Starlette,
        "Middleware": starlette_middleware.Middleware,
        "HTMLResponse": starlette_responses.HTMLResponse,
        "PlainTextResponse": starlette_responses.PlainTextResponse,
        "Route": starlette_routing.Route,
        "WebSocketRoute": starlette_routing.WebSocketRoute,
        "TrustedHostMiddleware": trustedhost.TrustedHostMiddleware,
        "uvicorn": uvicorn,
    }


def ensure_web_ui_available() -> None:
    """Fail before model/MCP startup when the optional web extra is absent."""

    _load_web_dependencies()


class WebUiApprovalBroker:
    """Bridge the agent's approval callback to one authenticated browser."""

    def __init__(self) -> None:
        self._sender: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._pending: dict[str, asyncio.Future[bool | str]] = {}

    def attach(
        self,
        sender: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        self._sender = sender

    def detach(
        self,
        sender: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if self._sender is not sender:
            return
        self._sender = None
        self.deny_all()

    def deny_all(self) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_result(False)

    def resolve(self, request_id: str, decision: str) -> bool:
        future = self._pending.get(request_id)
        if future is None or future.done():
            return False
        if decision == "session":
            future.set_result("session")
        elif decision == "yes":
            future.set_result(True)
        elif decision == "no":
            future.set_result(False)
        else:
            return False
        return True

    async def approve_tool_call(
        self,
        tool_name: str,
        arguments: dict[str, object],
    ) -> bool | str:
        sender = self._sender
        if sender is None:
            # The browser is the only interactive approval channel in Web-UI
            # mode. If it is absent or disconnected, fail closed.
            return False

        request_id = secrets.token_urlsafe(18)
        future: asyncio.Future[bool | str] = (
            asyncio.get_running_loop().create_future()
        )
        self._pending[request_id] = future
        safe_tool_name = sanitize_terminal_text(
            tool_name,
            multiline=False,
            escape_invisible_formatting=True,
            escape_literal_backslashes=True,
        )
        try:
            await sender(
                {
                    "type": "approval_required",
                    "id": request_id,
                    "tool": safe_tool_name,
                    "arguments": approval_arguments(arguments),
                }
            )
        except Exception:
            self._pending.pop(request_id, None)
            return False

        try:
            return await future
        finally:
            self._pending.pop(request_id, None)


class _WebUiSession:
    def __init__(
        self,
        *,
        agent: Any,
        approval_broker: WebUiApprovalBroker,
        token: str,
        expected_origin: str,
        workspace: Path,
        model: str,
        mcp_servers: tuple[str, ...],
        output_target: OutputTarget | None,
        initial_messages: tuple[str, ...],
        debug: bool,
    ) -> None:
        self.agent = agent
        self.approval_broker = approval_broker
        self.token = token
        self.expected_origin = expected_origin
        self.workspace = workspace
        self.model = model
        self.mcp_servers = mcp_servers
        self.output_target = output_target
        self.initial_messages = initial_messages
        self.debug = debug
        self._active_sender: Callable[[dict[str, Any]], Awaitable[None]] | None = None
        self._connection_lock = asyncio.Lock()
        self._agent_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task[None]] = set()
        self.shutdown_callback: Callable[[], None] | None = None

    def _authorized(self, websocket: Any) -> bool:
        token = websocket.query_params.get("token")
        origin = websocket.headers.get("origin")
        return (
            isinstance(token, str)
            and secrets.compare_digest(token, self.token)
            and origin == self.expected_origin
        )

    async def _handle_prompt(
        self,
        prompt: str,
        sender: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        async with self._agent_lock:
            try:
                answer = await self.agent.ask(prompt)
                await sender({"type": "answer", "content": answer})
                if self.output_target is not None and not is_local_agent_command(prompt):
                    try:
                        self.output_target.write_text(answer)
                    except Exception as exc:
                        if self.debug:
                            message = "".join(traceback.format_exception(exc))
                        else:
                            detail = str(exc).strip()
                            message = (
                                f"{type(exc).__name__}: {detail}"
                                if detail
                                else type(exc).__name__
                            )
                        await sender({"type": "error", "content": message})
            except Exception as exc:
                if self.debug:
                    message = "".join(traceback.format_exception(exc))
                else:
                    detail = str(exc).strip()
                    message = (
                        f"{type(exc).__name__}: {detail}"
                        if detail
                        else type(exc).__name__
                    )
                try:
                    await sender({"type": "error", "content": message})
                except Exception:
                    pass
            finally:
                try:
                    await sender({"type": "busy", "value": False})
                except Exception:
                    pass

    def _track(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def websocket(self, websocket: Any) -> None:
        if not self._authorized(websocket):
            await websocket.close(code=4403)
            return

        async with self._connection_lock:
            if self._active_sender is not None:
                await websocket.close(code=4409)
                return

            await websocket.accept()
            send_lock = asyncio.Lock()

            async def sender(payload: dict[str, Any]) -> None:
                async with send_lock:
                    await websocket.send_json(payload)

            self._active_sender = sender
            self.approval_broker.attach(sender)
            try:
                await sender(
                    {
                        "type": "session",
                        "workspace": str(self.workspace),
                        "model": self.model,
                        "mcp_servers": list(self.mcp_servers),
                        "initial_messages": list(self.initial_messages),
                    }
                )
                while True:
                    try:
                        payload = await websocket.receive_json()
                    except Exception:
                        break
                    if not isinstance(payload, dict):
                        await sender(
                            {
                                "type": "error",
                                "content": "Ungültige Web-UI-Nachricht.",
                            }
                        )
                        continue

                    kind = payload.get("type")
                    if kind == "approval_response":
                        request_id = payload.get("id")
                        decision = payload.get("decision")
                        if not isinstance(request_id, str) or not isinstance(
                            decision, str
                        ):
                            continue
                        if not self.approval_broker.resolve(
                            request_id,
                            decision,
                        ):
                            await sender(
                                {
                                    "type": "error",
                                    "content": (
                                        "Die Tool-Freigabe ist nicht mehr aktiv "
                                        "oder ungültig."
                                    ),
                                }
                            )
                        continue

                    if kind == "quit":
                        self.approval_broker.deny_all()
                        await sender(
                            {
                                "type": "shutdown",
                                "content": "Web-UI wird beendet.",
                            }
                        )
                        if self.shutdown_callback is not None:
                            self.shutdown_callback()
                        return

                    if kind != "message":
                        await sender(
                            {
                                "type": "error",
                                "content": "Unbekannter Web-UI-Nachrichtentyp.",
                            }
                        )
                        continue

                    content = payload.get("content")
                    if not isinstance(content, str):
                        await sender(
                            {
                                "type": "error",
                                "content": "Prompt muss Text sein.",
                            }
                        )
                        continue
                    prompt = content.strip()
                    if not prompt:
                        continue
                    if len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
                        await sender(
                            {
                                "type": "error",
                                "content": (
                                    "Prompt überschreitet das Web-UI-Limit von "
                                    f"{MAX_PROMPT_BYTES} Bytes."
                                ),
                            }
                        )
                        await sender({"type": "busy", "value": False})
                        continue
                    if self._agent_lock.locked():
                        await sender(
                            {
                                "type": "error",
                                "content": (
                                    "Es läuft bereits eine Anfrage. "
                                    "Bitte warte auf deren Abschluss."
                                ),
                            }
                        )
                        continue
                    if prompt.casefold() in {"exit", "quit"}:
                        self.approval_broker.deny_all()
                        await sender(
                            {
                                "type": "shutdown",
                                "content": "Web-UI wird beendet.",
                            }
                        )
                        if self.shutdown_callback is not None:
                            self.shutdown_callback()
                        return

                    await sender({"type": "busy", "value": True})
                    self._track(
                        asyncio.create_task(
                            self._handle_prompt(prompt, sender)
                        )
                    )
            finally:
                self.approval_broker.detach(sender)
                self._active_sender = None

    async def close(self) -> None:
        self.approval_broker.deny_all()
        if self._tasks:
            for task in tuple(self._tasks):
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)


INDEX_HTML = """<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>cli-agent</title>
  <link rel="stylesheet" href="/assets/app.css">
  <script src="/assets/app.js" defer></script>
</head>
<body>
  <main class="shell">
    <header>
      <div>
        <h1>cli-agent</h1>
        <div id="session-meta" class="meta">Verbindung wird hergestellt …</div>
      </div>
      <button id="quit" class="secondary" type="button">Sitzung beenden</button>
    </header>
    <section id="chat" class="chat" aria-live="polite"></section>
    <form id="composer">
      <textarea id="prompt" rows="3" placeholder="Nachricht an cli-agent" required></textarea>
      <button id="send" type="submit">Senden</button>
    </form>
  </main>
  <dialog id="approval">
    <h2>Tool-Freigabe erforderlich</h2>
    <div id="approval-tool" class="tool"></div>
    <pre id="approval-arguments"></pre>
    <div class="approval-actions">
      <button data-decision="no" class="secondary" type="button">Nein</button>
      <button data-decision="session" class="secondary" type="button">Für Session</button>
      <button data-decision="yes" type="button">Ja</button>
    </div>
  </dialog>
</body>
</html>
"""

APP_CSS = """
:root {
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  color-scheme: light dark;
}
* { box-sizing: border-box; }
body { margin: 0; background: Canvas; color: CanvasText; }
.shell { max-width: 960px; min-height: 100vh; margin: 0 auto; padding: 24px; display: flex; flex-direction: column; gap: 18px; }
header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; border-bottom: 1px solid color-mix(in srgb, CanvasText 18%, transparent); padding-bottom: 14px; }
h1 { margin: 0 0 4px; font-size: 1.35rem; }
.meta { opacity: .72; font-size: .9rem; overflow-wrap: anywhere; }
.chat { flex: 1; display: flex; flex-direction: column; gap: 12px; min-height: 50vh; }
.message { max-width: 88%; padding: 11px 13px; border-radius: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
.message.user { align-self: flex-end; background: color-mix(in srgb, Highlight 18%, Canvas); }
.message.assistant, .message.system { align-self: flex-start; background: color-mix(in srgb, CanvasText 8%, Canvas); }
.message.error { align-self: flex-start; border: 1px solid #b42318; }
form { display: grid; grid-template-columns: 1fr auto; gap: 10px; position: sticky; bottom: 0; background: Canvas; padding: 10px 0 4px; }
textarea { width: 100%; resize: vertical; min-height: 70px; padding: 10px; font: inherit; }
button { border: 0; border-radius: 9px; padding: 10px 16px; background: Highlight; color: HighlightText; font: inherit; cursor: pointer; }
button.secondary { background: color-mix(in srgb, CanvasText 10%, Canvas); color: CanvasText; border: 1px solid color-mix(in srgb, CanvasText 20%, transparent); }
button:disabled, textarea:disabled { opacity: .55; cursor: not-allowed; }
dialog { width: min(760px, calc(100vw - 32px)); border: 1px solid color-mix(in srgb, CanvasText 20%, transparent); border-radius: 12px; padding: 20px; }
dialog::backdrop { background: rgb(0 0 0 / .45); }
.tool { font-weight: 650; margin-bottom: 10px; overflow-wrap: anywhere; }
pre { max-height: 45vh; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; padding: 12px; background: color-mix(in srgb, CanvasText 8%, Canvas); border-radius: 8px; }
.approval-actions { display: flex; justify-content: flex-end; gap: 8px; }
@media (max-width: 640px) {
  .shell { padding: 14px; }
  form { grid-template-columns: 1fr; }
  .message { max-width: 96%; }
}
"""

APP_JS = """
(() => {
  "use strict";

  const chat = document.getElementById("chat");
  const form = document.getElementById("composer");
  const prompt = document.getElementById("prompt");
  const send = document.getElementById("send");
  const quit = document.getElementById("quit");
  const meta = document.getElementById("session-meta");
  const approval = document.getElementById("approval");
  const approvalTool = document.getElementById("approval-tool");
  const approvalArguments = document.getElementById("approval-arguments");

  let socket = null;
  let pendingApprovalId = null;
  let busy = false;

  function addMessage(kind, content) {
    const element = document.createElement("div");
    element.className = "message " + kind;
    element.textContent = content;
    chat.appendChild(element);
    element.scrollIntoView({ block: "end", behavior: "smooth" });
  }

  function setBusy(value) {
    busy = Boolean(value);
    prompt.disabled = busy;
    send.disabled = busy || !socket || socket.readyState !== WebSocket.OPEN;
    if (!busy) {
      prompt.focus();
    }
  }

  function tokenFromFragment() {
    const params = new URLSearchParams(window.location.hash.slice(1));
    return params.get("token");
  }

  const token = tokenFromFragment();
  if (!token) {
    meta.textContent = "Ungültiger Start-Link: Session-Token fehlt.";
    prompt.disabled = true;
    send.disabled = true;
    return;
  }

  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  socket = new WebSocket(
    scheme + "://" + window.location.host + "/ws?token=" + encodeURIComponent(token)
  );

  socket.addEventListener("open", () => setBusy(false));

  socket.addEventListener("close", () => {
    setBusy(true);
    quit.disabled = true;
    if (approval.open) {
      approval.close();
    }
    meta.textContent = "Web-UI-Verbindung beendet.";
  });

  socket.addEventListener("message", (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch (_error) {
      addMessage("error", "Ungültige Antwort vom lokalen Web-UI-Server.");
      return;
    }

    if (payload.type === "session") {
      const servers = Array.isArray(payload.mcp_servers)
        ? payload.mcp_servers.join(", ")
        : "";
      meta.textContent =
        "Workspace: " + payload.workspace +
        " · Modell: " + payload.model +
        " · MCP: " + (servers || "(keine)");
      for (const message of payload.initial_messages || []) {
        addMessage("system", String(message));
      }
      return;
    }
    if (payload.type === "busy") {
      setBusy(payload.value);
      return;
    }
    if (payload.type === "answer") {
      addMessage("assistant", String(payload.content || ""));
      return;
    }
    if (payload.type === "error") {
      addMessage("error", String(payload.content || "Unbekannter Fehler."));
      return;
    }
    if (payload.type === "approval_required") {
      pendingApprovalId = String(payload.id);
      approvalTool.textContent = String(payload.tool || "");
      approvalArguments.textContent = String(payload.arguments || "{}");
      approval.showModal();
      return;
    }
    if (payload.type === "shutdown") {
      addMessage("system", String(payload.content || "Sitzung beendet."));
      setBusy(true);
    }
  });

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (busy || !socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }
    const content = prompt.value.trim();
    if (!content) {
      return;
    }
    addMessage("user", content);
    socket.send(JSON.stringify({ type: "message", content }));
    prompt.value = "";
    setBusy(true);
  });

  prompt.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  approval.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-decision]");
    if (!button || !pendingApprovalId || !socket) {
      return;
    }
    socket.send(JSON.stringify({
      type: "approval_response",
      id: pendingApprovalId,
      decision: button.dataset.decision
    }));
    pendingApprovalId = null;
    approval.close();
  });

  approval.addEventListener("cancel", (event) => {
    event.preventDefault();
    if (pendingApprovalId && socket) {
      socket.send(JSON.stringify({
        type: "approval_response",
        id: pendingApprovalId,
        decision: "no"
      }));
    }
    pendingApprovalId = null;
    approval.close();
  });

  quit.addEventListener("click", () => {
    if (socket && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: "quit" }));
    }
  });
})();
"""


def _security_headers(response: Any) -> Any:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "connect-src 'self' ws://127.0.0.1:*; "
        "img-src 'none'; "
        "object-src 'none'; "
        "frame-ancestors 'none'; "
        "base-uri 'none'; "
        "form-action 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=()"
    )
    return response


async def run_web_ui(
    *,
    agent: Any,
    approval_broker: WebUiApprovalBroker,
    workspace: Path,
    model: str,
    mcp_servers: tuple[str, ...],
    output_target: OutputTarget | None,
    initial_messages: tuple[str, ...] = (),
    debug: bool = False,
) -> None:
    deps = _load_web_dependencies()
    Starlette = deps["Starlette"]
    Middleware = deps["Middleware"]
    HTMLResponse = deps["HTMLResponse"]
    PlainTextResponse = deps["PlainTextResponse"]
    Route = deps["Route"]
    WebSocketRoute = deps["WebSocketRoute"]
    TrustedHostMiddleware = deps["TrustedHostMiddleware"]
    uvicorn = deps["uvicorn"]

    token = secrets.token_urlsafe(32)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((WEB_UI_HOST, 0))
        sock.listen(128)
        sock.setblocking(False)
        port = int(sock.getsockname()[1])
        origin = f"http://{WEB_UI_HOST}:{port}"

        session = _WebUiSession(
            agent=agent,
            approval_broker=approval_broker,
            token=token,
            expected_origin=origin,
            workspace=workspace,
            model=model,
            mcp_servers=mcp_servers,
            output_target=output_target,
            initial_messages=initial_messages,
            debug=debug,
        )

        async def index(_request: Any) -> Any:
            return _security_headers(HTMLResponse(INDEX_HTML))

        async def css(_request: Any) -> Any:
            return _security_headers(
                PlainTextResponse(APP_CSS, media_type="text/css")
            )

        async def js(_request: Any) -> Any:
            return _security_headers(
                PlainTextResponse(
                    APP_JS,
                    media_type="application/javascript",
                )
            )

        async def websocket_endpoint(websocket: Any) -> None:
            await session.websocket(websocket)

        app = Starlette(
            debug=False,
            routes=[
                Route("/", index),
                Route("/assets/app.css", css),
                Route("/assets/app.js", js),
                WebSocketRoute("/ws", websocket_endpoint),
            ],
            middleware=[
                Middleware(
                    TrustedHostMiddleware,
                    allowed_hosts=[WEB_UI_HOST],
                )
            ],
        )

        config = uvicorn.Config(
            app,
            host=WEB_UI_HOST,
            port=port,
            log_level="warning",
            access_log=False,
            ws_max_size=MAX_WS_MESSAGE_BYTES,
        )
        server = uvicorn.Server(config)
        session.shutdown_callback = lambda: setattr(server, "should_exit", True)

        url = f"{origin}/#token={token}"
        print(
            sanitize_terminal_text(
                f"Web-UI: {origin}/ (nur localhost)",
                multiline=False,
                escape_invisible_formatting=True,
            )
        )
        print("Web-UI Access Token:", token)
        print("Zum Beenden Strg+C im Terminal oder 'Sitzung beenden' im Browser.")

        server_task = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started and not server_task.done():
            await asyncio.sleep(0.01)
        if server_task.done():
            await server_task
            return

        await asyncio.to_thread(webbrowser.open, url, new=2)
        try:
            await server_task
        finally:
            await session.close()
    finally:
        try:
            sock.close()
        except OSError:
            pass
