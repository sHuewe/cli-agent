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

from markdown_it import MarkdownIt

from .approval_display import approval_arguments
from .file_context import OutputTarget, is_local_agent_command
from .local_commands import classify_local_command
from .terminal_output import sanitize_terminal_text

WEB_UI_HOST = "127.0.0.1"
MAX_PROMPT_BYTES = 512 * 1024
MAX_WS_MESSAGE_BYTES = 1024 * 1024

_MARKDOWN = (
    MarkdownIt(
        "commonmark",
        {
            "html": False,
            "linkify": False,
        },
    )
    .enable("table")
    .enable("strikethrough")
)


def _render_markdown(text: str) -> str:
    """Render trusted Markdown output while keeping raw HTML disabled."""

    return _MARKDOWN.render(text)


_WEB_EXTRA_ERROR = (
    "Die Web-UI ist nicht installiert. Installiere cli-agent mit dem optionalen "
    "Extra 'web', z. B. mit: pipx install 'cli-agent[web]'"
)

_WEB_UI_COMMANDS: dict[str, tuple[int, str]] = {
    "tokens": (0, "tokens"),
    "clear_web_context": (0, "clear_web_context"),
    "add_web_context": (1, "add_web_context <URL>"),
    "enable": (1, "enable <SERVER>"),
    "disable": (1, "disable <SERVER>"),
}


def _web_ui_command_prompt(command: object, argument: object = None) -> str:
    """Build a validated local command without ever falling through to the LLM."""

    if not isinstance(command, str) or command not in _WEB_UI_COMMANDS:
        raise ValueError("Unbekannter Web-UI-Befehl.")

    argument_count, usage = _WEB_UI_COMMANDS[command]
    if argument_count == 0:
        if argument not in (None, ""):
            raise ValueError(f"Ungültige Syntax. Verwendung: {usage}")
        prompt = command
    else:
        if not isinstance(argument, str) or not argument.strip():
            raise ValueError(f"Argument fehlt. Verwendung: {usage}")
        prompt = f"{command} {argument.strip()}"

    parsed = classify_local_command(prompt)
    if (
        not parsed.is_local
        or parsed.error is not None
        or parsed.command != command
    ):
        raise ValueError(f"Ungültiges Argument. Verwendung: {usage}")
    return prompt


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
        self._delivery_lock = asyncio.Lock()
        self._agent_lock = asyncio.Lock()
        self._pending_events: list[dict[str, Any]] = []
        self._prompt_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=1)
        self._request_pending = False
        self.shutdown_callback: Callable[[], None] | None = None

    def _authorized(self, websocket: Any) -> bool:
        token = websocket.query_params.get("token")
        origin = websocket.headers.get("origin")
        return (
            isinstance(token, str)
            and secrets.compare_digest(token, self.token)
            and origin == self.expected_origin
        )

    async def _flush_pending_events(self) -> None:
        async with self._delivery_lock:
            sender = self._active_sender
            if sender is None:
                return
            while self._pending_events:
                try:
                    await sender(self._pending_events[0])
                except Exception:
                    return
                self._pending_events.pop(0)

    async def _send_event(self, payload: dict[str, Any]) -> None:
        async with self._delivery_lock:
            self._pending_events.append(payload)
            sender = self._active_sender
            if sender is None:
                return
            while self._pending_events:
                try:
                    await sender(self._pending_events[0])
                except Exception:
                    return
                self._pending_events.pop(0)

    def _busy(self) -> bool:
        return self._request_pending or self._agent_lock.locked()

    def _enqueue_prompt(self, prompt: str) -> None:
        if self._busy():
            raise RuntimeError("Es läuft bereits eine Anfrage.")
        self._request_pending = True
        try:
            self._prompt_queue.put_nowait(prompt)
        except BaseException:
            self._request_pending = False
            raise

    async def next_prompt(self) -> str:
        return await self._prompt_queue.get()

    def _working_context_payload(self) -> dict[str, Any]:
        contexts_snapshot = (
            self.agent.context_states_snapshot()
            if hasattr(self.agent, "context_states_snapshot")
            else []
        )
        okf_snapshot = (
            self.agent.okf_status_snapshot()
            if hasattr(self.agent, "okf_status_snapshot")
            else {"configured": False, "enabled": False, "available": False}
        )
        tool_states = (
            self.agent.tool_states_snapshot()
            if hasattr(self.agent, "tool_states_snapshot")
            else []
        )
        return {
            "type": "working_context",
            "messages": self.agent.working_messages_snapshot(),
            "tools": tool_states,
            "contexts": contexts_snapshot,
            "okf": okf_snapshot,
        }

    async def _handle_prompt(self, prompt: str) -> None:
        async with self._agent_lock:
            try:
                answer = await self.agent.ask(prompt)
                await self._send_event(
                    {
                        "type": "answer",
                        "content": answer,
                        "html": _render_markdown(answer),
                    }
                )
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
                        await self._send_event(
                            {"type": "error", "content": message}
                        )
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
                await self._send_event({"type": "error", "content": message})
            finally:
                self._request_pending = False
                await self._send_event({"type": "busy", "value": False})

    async def websocket(self, websocket: Any) -> None:
        if not self._authorized(websocket):
            await websocket.close(code=4403)
            return

        send_lock = asyncio.Lock()

        async def sender(payload: dict[str, Any]) -> None:
            async with send_lock:
                await websocket.send_json(payload)

        async with self._connection_lock:
            if self._active_sender is not None:
                await websocket.close(code=4409)
                return
            self._active_sender = sender

        try:
            await websocket.accept()
            self.approval_broker.attach(sender)
            await sender(
                {
                    "type": "session",
                    "workspace": str(self.workspace),
                    "model": self.model,
                    "mcp_servers": list(self.mcp_servers),
                    "initial_messages": list(self.initial_messages),
                }
            )
            await self._flush_pending_events()
            await sender({"type": "busy", "value": self._busy()})
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

                if kind == "working_context":
                    await sender(self._working_context_payload())
                    continue

                if kind == "reset_history":
                    if self._busy():
                        await sender(
                            {
                                "type": "error",
                                "content": "Die History kann während einer laufenden Anfrage nicht zurückgesetzt werden.",
                            }
                        )
                        continue
                    self.approval_broker.deny_all()
                    self.agent.reset_history()
                    await sender({"type": "history_reset"})
                    await sender(self._working_context_payload())
                    continue

                if kind == "tool_toggle":
                    if self._busy():
                        await sender(
                            {
                                "type": "error",
                                "content": "Tools können während einer laufenden Anfrage nicht geändert werden.",
                            }
                        )
                        continue
                    tool_name = payload.get("name")
                    enabled = payload.get("enabled")
                    if not isinstance(tool_name, str) or not isinstance(enabled, bool):
                        await sender({"type": "error", "content": "Ungültige Tool-Umschaltung."})
                        continue
                    try:
                        self.agent.set_tool_enabled(tool_name, enabled=enabled)
                    except ValueError as exc:
                        await sender({"type": "error", "content": str(exc)})
                        continue
                    await sender(self._working_context_payload())
                    continue

                if kind == "context_toggle":
                    if self._busy():
                        await sender(
                            {
                                "type": "error",
                                "content": "Context kann während einer laufenden Anfrage nicht geändert werden.",
                            }
                        )
                        continue
                    context_id = payload.get("id")
                    enabled = payload.get("enabled")
                    if not isinstance(context_id, str) or not isinstance(enabled, bool):
                        await sender({"type": "error", "content": "Ungültige Context-Umschaltung."})
                        continue
                    try:
                        self.agent.set_context_enabled(context_id, enabled=enabled)
                    except ValueError as exc:
                        await sender({"type": "error", "content": str(exc)})
                        continue
                    await sender(self._working_context_payload())
                    continue

                if kind == "okf_toggle":
                    if self._busy():
                        await sender(
                            {
                                "type": "error",
                                "content": "OKF kann während einer laufenden Anfrage nicht geändert werden.",
                            }
                        )
                        continue
                    enabled = payload.get("enabled")
                    if not isinstance(enabled, bool):
                        await sender({"type": "error", "content": "Ungültige OKF-Umschaltung."})
                        continue
                    try:
                        self.agent.set_okf_enabled(enabled=enabled)
                    except ValueError as exc:
                        await sender({"type": "error", "content": str(exc)})
                        continue
                    await sender(self._working_context_payload())
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

                if kind == "command":
                    try:
                        command_prompt = _web_ui_command_prompt(
                            payload.get("command"),
                            payload.get("argument"),
                        )
                    except ValueError as exc:
                        await sender(
                            {"type": "error", "content": str(exc)}
                        )
                        await sender({"type": "busy", "value": False})
                        continue
                    if len(command_prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
                        await sender(
                            {
                                "type": "error",
                                "content": (
                                    "Befehl überschreitet das Web-UI-Limit von "
                                    f"{MAX_PROMPT_BYTES} Bytes."
                                ),
                            }
                        )
                        await sender({"type": "busy", "value": False})
                        continue
                    if self._busy():
                        await sender(
                            {
                                "type": "error",
                                "content": (
                                    "Es läuft bereits eine Anfrage. "
                                    "Bitte warte auf deren Abschluss."
                                ),
                            }
                        )
                        await sender({"type": "busy", "value": False})
                        continue
                    await sender({"type": "busy", "value": True})
                    try:
                        self._enqueue_prompt(command_prompt)
                    except RuntimeError as exc:
                        await sender({"type": "error", "content": str(exc)})
                        await sender({"type": "busy", "value": False})
                    continue

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
                    await sender({"type": "busy", "value": False})
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
                try:
                    self._enqueue_prompt(prompt)
                except RuntimeError as exc:
                    await sender({"type": "error", "content": str(exc)})
                    await sender({"type": "busy", "value": False})
        finally:
            self.approval_broker.detach(sender)
            async with self._connection_lock:
                if self._active_sender is sender:
                    self._active_sender = None

    async def close(self) -> None:
        self.approval_broker.deny_all()


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
      <div class="header-actions">
        <button id="reset-history" class="secondary" type="button">History zurücksetzen</button>
        <button id="show-working-context" class="secondary" type="button">LLM Context</button>
        <button id="quit" class="secondary" type="button">Sitzung beenden</button>
      </div>
    </header>
    <section id="chat" class="chat" aria-live="polite"></section>
    <div id="commands" class="commands" aria-label="Lokale Befehle">
      <button class="secondary" data-command="tokens" type="button">Tokens</button>
      <button class="secondary" data-command="add_web_context" type="button">Web-Kontext hinzufügen</button>
      <button class="secondary" data-command="clear_web_context" type="button">Web-Kontext löschen</button>
      <button class="secondary" data-command="enable" type="button">MCP aktivieren</button>
      <button class="secondary" data-command="disable" type="button">MCP deaktivieren</button>
      <button id="cancel-command" class="secondary hidden" type="button">Abbrechen</button>
    </div>
    <form id="composer">
      <textarea id="prompt" rows="3" placeholder="Nachricht an cli-agent" required></textarea>
      <button id="send" type="submit">Senden</button>
    </form>
  </main>
  <dialog id="working-context-dialog" class="working-context-dialog">
    <div class="dialog-header">
      <div>
        <h2>LLM Context</h2>
        <div class="meta">Messages des letzten Main-Loops sowie aktuell wirksame Tools und Referenzkontexte.</div>
      </div>
      <div class="dialog-actions">
        <button id="refresh-working-context" class="secondary" type="button">Aktualisieren</button>
        <button id="close-working-context" class="secondary" type="button">Schließen</button>
      </div>
    </div>
    <div class="context-tabs" role="tablist" aria-label="LLM Context">
      <button id="context-tab-messages" class="secondary active" type="button" role="tab" aria-selected="true" aria-controls="context-panel-messages">Messages</button>
      <button id="context-tab-tools" class="secondary" type="button" role="tab" aria-selected="false" aria-controls="context-panel-tools">Tools</button>
      <button id="context-tab-contexts" class="secondary" type="button" role="tab" aria-selected="false" aria-controls="context-panel-contexts">Context</button>
    </div>
    <div id="context-panel-messages" class="context-panel" role="tabpanel" aria-labelledby="context-tab-messages">
      <pre id="working-messages-json">[]</pre>
    </div>
    <div id="context-panel-tools" class="context-panel hidden" role="tabpanel" aria-labelledby="context-tab-tools">
      <div id="working-tools-list" class="state-list"></div>
    </div>
    <div id="context-panel-contexts" class="context-panel hidden" role="tabpanel" aria-labelledby="context-tab-contexts">
      <div id="okf-state"></div>
      <div id="working-contexts-list" class="state-list"></div>
    </div>
  </dialog>
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
header { position: sticky; top: 0; z-index: 20; display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; border-bottom: 1px solid color-mix(in srgb, CanvasText 18%, transparent); padding: 14px 0; background: Canvas; }
.header-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
h1 { margin: 0 0 4px; font-size: 1.35rem; }
.meta { opacity: .72; font-size: .9rem; overflow-wrap: anywhere; }
.chat { flex: 1; display: flex; flex-direction: column; gap: 12px; min-height: 50vh; }
.message { max-width: 88%; padding: 11px 13px; border-radius: 12px; white-space: pre-wrap; overflow-wrap: anywhere; }
.message.user { align-self: flex-end; background: color-mix(in srgb, Highlight 18%, Canvas); }
.message.assistant, .message.system { align-self: flex-start; background: color-mix(in srgb, CanvasText 8%, Canvas); }
.message.assistant { white-space: normal; overflow-x: auto; }
.message.assistant > :first-child { margin-top: 0; }
.message.assistant > :last-child { margin-bottom: 0; }
.message.assistant h1 { font-size: 1.35rem; }
.message.assistant h2 { font-size: 1.2rem; }
.message.assistant h3 { font-size: 1.08rem; }
.message.assistant h1, .message.assistant h2, .message.assistant h3 { margin: 1em 0 .45em; }
.message.assistant ul, .message.assistant ol { padding-left: 1.5rem; }
.message.assistant li + li { margin-top: .2rem; }
.message.assistant code { font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace; }
.message.assistant :not(pre) > code { padding: .1em .3em; border-radius: 4px; background: color-mix(in srgb, CanvasText 9%, Canvas); }
.message.assistant pre { max-width: 100%; overflow: auto; white-space: pre; }
.message.assistant blockquote { margin: .8em 0; padding-left: .9em; border-left: 3px solid color-mix(in srgb, CanvasText 28%, transparent); opacity: .9; }
.message.assistant table { border-collapse: collapse; display: block; max-width: 100%; overflow-x: auto; }
.message.assistant th, .message.assistant td { border: 1px solid color-mix(in srgb, CanvasText 20%, transparent); padding: 6px 9px; text-align: left; }
.message.error { align-self: flex-start; border: 1px solid #b42318; }
.commands { display: flex; flex-wrap: wrap; gap: 8px; }
.commands button { padding: 7px 10px; font-size: .9rem; }
.hidden { display: none; }
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
.working-context-dialog { width: min(1100px, calc(100vw - 32px)); }
.working-context-dialog pre { min-height: 55vh; max-height: 72vh; }
.state-list { display: flex; flex-direction: column; gap: 10px; }
.state-row { display: grid; grid-template-columns: minmax(170px, 1fr) minmax(260px, 2fr) auto; gap: 12px; align-items: start; padding: 12px; border: 1px solid color-mix(in srgb, CanvasText 18%, transparent); border-radius: 9px; }
.state-name { font-weight: 650; overflow-wrap: anywhere; }
.state-description { opacity: .78; white-space: pre-wrap; overflow-wrap: anywhere; }
.state-source { opacity: .7; font-size: .85rem; overflow-wrap: anywhere; }
.state-content { max-height: 16rem; overflow: auto; white-space: pre-wrap; margin-top: 6px; padding: 8px; border-radius: 6px; background: color-mix(in srgb, CanvasText 6%, Canvas); }
.state-row button { min-width: 92px; padding: 7px 10px; }
#okf-state { margin-bottom: 12px; }
.context-tabs { display: flex; gap: 8px; margin-bottom: 12px; border-bottom: 1px solid color-mix(in srgb, CanvasText 18%, transparent); padding-bottom: 8px; }
.context-tabs button.active { background: Highlight; color: HighlightText; border-color: transparent; }
.context-panel.hidden { display: none; }
.dialog-header { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
.dialog-header h2 { margin: 0 0 4px; }
.dialog-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
@media (max-width: 640px) {
  .shell { padding: 14px; }
  form { grid-template-columns: 1fr; }
  .message { max-width: 96%; }
  .state-row { grid-template-columns: 1fr; }
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
  const resetHistory = document.getElementById("reset-history");
  const meta = document.getElementById("session-meta");
  const showWorkingContext = document.getElementById("show-working-context");
  const workingContextDialog = document.getElementById("working-context-dialog");
  const workingMessagesJson = document.getElementById("working-messages-json");
  const workingToolsList = document.getElementById("working-tools-list");
  const workingContextsList = document.getElementById("working-contexts-list");
  const okfState = document.getElementById("okf-state");
  const refreshWorkingContext = document.getElementById("refresh-working-context");
  const closeWorkingContext = document.getElementById("close-working-context");
  const contextTabMessages = document.getElementById("context-tab-messages");
  const contextTabTools = document.getElementById("context-tab-tools");
  const contextTabContexts = document.getElementById("context-tab-contexts");
  const contextPanelMessages = document.getElementById("context-panel-messages");
  const contextPanelTools = document.getElementById("context-panel-tools");
  const contextPanelContexts = document.getElementById("context-panel-contexts");
  const commandButtons = Array.from(
    document.querySelectorAll("button[data-command]")
  );
  const cancelCommand = document.getElementById("cancel-command");
  const approval = document.getElementById("approval");
  const approvalTool = document.getElementById("approval-tool");
  const approvalArguments = document.getElementById("approval-arguments");

  let socket = null;
  let pendingApprovalId = null;
  let pendingCommand = null;
  let busy = false;

  const COMMANDS = {
    tokens: {
      argument: false,
      display: "tokens"
    },
    clear_web_context: {
      argument: false,
      display: "clear_web_context"
    },
    add_web_context: {
      argument: true,
      display: "add_web_context",
      question: "Welche URL soll als Web-Kontext hinzugefügt werden?",
      placeholder: "https://…"
    },
    enable: {
      argument: true,
      display: "enable",
      question: "Welcher MCP-Server soll aktiviert werden?",
      placeholder: "Name des MCP-Servers"
    },
    disable: {
      argument: true,
      display: "disable",
      question: "Welcher MCP-Server soll deaktiviert werden?",
      placeholder: "Name des MCP-Servers"
    }
  };

  function addMessage(kind, content, renderedHtml = null) {
    const element = document.createElement("div");
    element.className = "message " + kind;
    if (kind === "assistant" && typeof renderedHtml === "string") {
      element.innerHTML = renderedHtml;
    } else {
      element.textContent = content;
    }
    chat.appendChild(element);
    element.scrollIntoView({ block: "end", behavior: "smooth" });
  }

  function setBusy(value) {
    busy = Boolean(value);
    const disconnected = !socket || socket.readyState !== WebSocket.OPEN;
    prompt.disabled = busy;
    send.disabled = busy || disconnected;
    showWorkingContext.disabled = disconnected;
    resetHistory.disabled = busy || disconnected;

  for (const button of commandButtons) {
      button.disabled = busy || disconnected;
    }
    cancelCommand.disabled = busy || disconnected;
    if (!busy) {
      prompt.focus();
    }
  }

  function resetPendingCommand() {
    pendingCommand = null;
    prompt.placeholder = "Nachricht an cli-agent";
    cancelCommand.classList.add("hidden");
  }

  function sendCommand(name, argument = null) {
    if (busy || !socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }
    socket.send(JSON.stringify({
      type: "command",
      command: name,
      argument
    }));
    setBusy(true);
  }

  function beginCommand(name) {
    const spec = COMMANDS[name];
    if (!spec) {
      return;
    }
    if (!spec.argument) {
      resetPendingCommand();
      addMessage("user", spec.display);
      sendCommand(name);
      return;
    }
    pendingCommand = name;
    prompt.placeholder = spec.placeholder;
    cancelCommand.classList.remove("hidden");
    addMessage("system", spec.question);
    prompt.focus();
  }

  function setContextTab(tab) {
    const showMessages = tab === "messages";
    const showTools = tab === "tools";
    const showContexts = tab === "contexts";
    contextTabMessages.classList.toggle("active", showMessages);
    contextTabTools.classList.toggle("active", showTools);
    contextTabContexts.classList.toggle("active", showContexts);
    contextTabMessages.setAttribute("aria-selected", String(showMessages));
    contextTabTools.setAttribute("aria-selected", String(showTools));
    contextTabContexts.setAttribute("aria-selected", String(showContexts));
    contextPanelMessages.classList.toggle("hidden", !showMessages);
    contextPanelTools.classList.toggle("hidden", !showTools);
    contextPanelContexts.classList.toggle("hidden", !showContexts);
  }

  function stateToggleButton(label, enabled, onClick, disabled = false) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = enabled ? "secondary" : "";
    button.textContent = enabled ? "Disable" : "Enable";
    button.title = label;
    button.disabled = disabled || busy;
    button.addEventListener("click", onClick);
    return button;
  }

  function renderTools(tools) {
    workingToolsList.replaceChildren();
    if (!tools.length) {
      const empty = document.createElement("div");
      empty.className = "meta";
      empty.textContent = "Keine MCP-Tools verfügbar.";
      workingToolsList.appendChild(empty);
      return;
    }
    for (const tool of tools) {
      const row = document.createElement("div");
      row.className = "state-row";
      const name = document.createElement("div");
      name.className = "state-name";
      name.textContent = String(tool.name || "");
      const description = document.createElement("div");
      description.className = "state-description";
      description.textContent = String(tool.description || "");
      const serverEnabled = tool.server_enabled !== false;
      const enabled = Boolean(tool.enabled);
      const button = stateToggleButton(
        name.textContent,
        enabled,
        () => socket.send(JSON.stringify({
          type: "tool_toggle",
          name: tool.name,
          enabled: !enabled
        })),
        !serverEnabled
      );
      if (!serverEnabled) {
        button.title = "MCP-Server ist deaktiviert.";
      }
      row.append(name, description, button);
      workingToolsList.appendChild(row);
    }
  }

  function renderContexts(contexts, okf) {
    workingContextsList.replaceChildren();
    okfState.replaceChildren();

    if (okf && okf.configured) {
      const row = document.createElement("div");
      row.className = "state-row";
      const name = document.createElement("div");
      name.className = "state-name";
      name.textContent = "OKF Knowledge";
      const description = document.createElement("div");
      description.className = "state-description";
      description.textContent = okf.available
        ? "Konfigurierter Knowledge-Lauf vor der Hauptanfrage."
        : "Konfiguriert, aber aktuell nicht verfügbar.";
      const enabled = Boolean(okf.enabled);
      const button = stateToggleButton(
        "OKF Knowledge",
        enabled,
        () => socket.send(JSON.stringify({
          type: "okf_toggle",
          enabled: !enabled
        })),
        !okf.available
      );
      row.append(name, description, button);
      okfState.appendChild(row);
    }

    if (!contexts.length) {
      const empty = document.createElement("div");
      empty.className = "meta";
      empty.textContent = "Keine Datei- oder Web-Kontexte vorhanden.";
      workingContextsList.appendChild(empty);
      return;
    }

    for (const context of contexts) {
      const row = document.createElement("div");
      row.className = "state-row";
      const heading = document.createElement("div");
      const name = document.createElement("div");
      name.className = "state-name";
      name.textContent = String(context.label || context.source || "");
      const source = document.createElement("div");
      source.className = "state-source";
      source.textContent = (context.kind === "file" ? "Datei: " : "Web: ") +
        String(context.source || "");
      heading.append(name, source);

      const body = document.createElement("div");
      body.className = "state-description";
      const content = document.createElement("div");
      content.className = "state-content";
      content.textContent = String(context.content || "");
      body.appendChild(content);

      const enabled = Boolean(context.enabled);
      const button = stateToggleButton(
        name.textContent,
        enabled,
        () => socket.send(JSON.stringify({
          type: "context_toggle",
          id: context.id,
          enabled: !enabled
        }))
      );
      row.append(heading, body, button);
      workingContextsList.appendChild(row);
    }
  }

  function requestWorkingContext() {
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      const message = "Web-UI-Verbindung ist nicht aktiv.";
      workingMessagesJson.textContent = message;
      workingToolsList.textContent = message;
      workingContextsList.textContent = message;
      return;
    }
    socket.send(JSON.stringify({ type: "working_context" }));
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
  const wsUrl =
    scheme + "://" + window.location.host + "/ws?token=" + encodeURIComponent(token);
  const maxReconnectAttempts = 12;
  const reconnectDelayMs = 250;
  let reconnectAttempts = 0;

  function connectSocket() {
    socket = new WebSocket(wsUrl);

    socket.addEventListener("open", () => {
      meta.textContent = "Web-UI-Verbindung wird hergestellt …";
    });

    socket.addEventListener("close", (event) => {
      setBusy(true);
      quit.disabled = true;
      if (approval.open) {
        approval.close();
      }
      if (event.code === 4409 && reconnectAttempts < maxReconnectAttempts) {
        reconnectAttempts += 1;
        meta.textContent = "Vorherige Web-UI-Verbindung wird beendet …";
        window.setTimeout(connectSocket, reconnectDelayMs);
        return;
      }
      meta.textContent =
        event.code === 4409
          ? "Eine andere Web-UI-Verbindung ist noch aktiv."
          : "Web-UI-Verbindung beendet.";
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
      reconnectAttempts = 0;
      quit.disabled = false;
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
      addMessage(
        "assistant",
        String(payload.content || ""),
        typeof payload.html === "string" ? payload.html : null
      );
      return;
    }
    if (payload.type === "error") {
      addMessage("error", String(payload.content || "Unbekannter Fehler."));
      return;
    }
    if (payload.type === "working_context") {
      const messages = Array.isArray(payload.messages) ? payload.messages : [];
      const tools = Array.isArray(payload.tools) ? payload.tools : [];
      const contexts = Array.isArray(payload.contexts) ? payload.contexts : [];
      workingMessagesJson.textContent = JSON.stringify(messages, null, 2);
      renderTools(tools);
      renderContexts(contexts, payload.okf || {});
      if (!workingContextDialog.open) {
        workingContextDialog.showModal();
      }
      return;
    }
    if (payload.type === "history_reset") {
      chat.replaceChildren();
      addMessage("system", "History wurde zurückgesetzt.");
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
  }

  setBusy(true);
  connectSocket();

  showWorkingContext.addEventListener("click", () => {
    workingMessagesJson.textContent = "Wird geladen …";
    workingToolsList.textContent = "Wird geladen …";
    workingContextsList.textContent = "Wird geladen …";
    okfState.replaceChildren();
    setContextTab("messages");
    if (!workingContextDialog.open) {
      workingContextDialog.showModal();
    }
    requestWorkingContext();
  });

  refreshWorkingContext.addEventListener("click", () => {
    workingMessagesJson.textContent = "Wird geladen …";
    workingToolsList.textContent = "Wird geladen …";
    workingContextsList.textContent = "Wird geladen …";
    okfState.replaceChildren();
    requestWorkingContext();
  });

  closeWorkingContext.addEventListener("click", () => {
    workingContextDialog.close();
  });

  contextTabMessages.addEventListener("click", () => setContextTab("messages"));
  contextTabTools.addEventListener("click", () => setContextTab("tools"));
  contextTabContexts.addEventListener("click", () => setContextTab("contexts"));

  resetHistory.addEventListener("click", () => {
    if (busy || !socket || socket.readyState !== WebSocket.OPEN) {
      return;
    }
    if (!window.confirm("Conversation-History wirklich zurücksetzen?")) {
      return;
    }
    socket.send(JSON.stringify({ type: "reset_history" }));
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
    prompt.value = "";
    if (pendingCommand) {
      const command = pendingCommand;
      resetPendingCommand();
      sendCommand(command, content);
      return;
    }
    socket.send(JSON.stringify({ type: "message", content }));
    setBusy(true);
  });

  prompt.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });

  for (const button of commandButtons) {
    button.addEventListener("click", () => {
      beginCommand(button.dataset.command);
    });
  }

  cancelCommand.addEventListener("click", () => {
    if (pendingCommand) {
      addMessage("system", "Befehl abgebrochen.");
    }
    resetPendingCommand();
    prompt.focus();
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
            while True:
                prompt_task = asyncio.create_task(session.next_prompt())
                done, _pending = await asyncio.wait(
                    {server_task, prompt_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if server_task in done:
                    prompt_task.cancel()
                    await asyncio.gather(prompt_task, return_exceptions=True)
                    await server_task
                    break

                prompt = prompt_task.result()
                # MCP client contexts are entered by the surrounding CLI task.
                # Run every agent interaction in this same task as well: AnyIO
                # cancel scopes used by MCP transports must be exited by the
                # task that entered them.
                await session._handle_prompt(prompt)
        finally:
            await session.close()
    finally:
        try:
            sock.close()
        except OSError:
            pass
