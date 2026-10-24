"""Local, authenticated agent API with per-session MCP lifetimes.

v1 transports an AG-UI event subset over SSE. It does not implement the
canonical AG-UI RunAgentInput/interrupt-resume contract yet.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .web_ui import WebUiApprovalBroker

API_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_SESSIONS = 8
MAX_PROMPT_BYTES = 512 * 1024
# Replay keeps a full supported run, including the optional initial knowledge
# phase and its two permitted re-entry searches. Five events per tool covers
# start, end, status, MCP user notice, and approval request.
MAX_EVENTS_PER_RUN = 8_192
REPLAY_EVENTS_PER_TOOL = 5
REPLAY_LIFECYCLE_ALLOWANCE = 32
MAX_JSON_BYTES = 1024 * 1024


@dataclass
class AgentRun:
    id: str
    thread_id: str
    prompt: str
    status: str = "queued"
    # Subscribers belong to this run, not the parent session. A slow
    # subscriber draining a previous run must never authorize a new run.
    listeners: int = 0
    answer: str | None = None
    error: str | None = None
    events: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    max_events: int = MAX_EVENTS_PER_RUN
    next_sequence: int = 1
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def done(self) -> bool:
        return self.status in {"completed", "failed", "cancelled"}

    def emit(self, event: dict[str, Any]) -> None:
        self.events.append((self.next_sequence, event))
        self.next_sequence += 1
        if len(self.events) > self.max_events:
            del self.events[: len(self.events) - self.max_events]
        self.changed.set()

    def snapshot(self) -> dict[str, Any]:
        return {
            "run_id": self.id, "status": self.status,
            "answer": self.answer if self.done else None,
            "error": self.error if self.done else None,
        }


class AgentSession:
    """The worker owns __aenter__ and __aexit__ on the SAME asyncio task.

    This is important for MCP transports which use task-bound AnyIO scopes.
    """

    def __init__(
        self,
        factory: Callable[["AgentSession"], Any],
        *,
        approval_workspace: Any = None,
        output_target: Any = None,
        initial_commands: tuple[str, ...] = (),
        debug_errors: bool = False,
    ) -> None:
        self.debug_errors = debug_errors
        self.initial_commands = initial_commands
        self.initial_messages: list[str] = []
        self.ui_messages: list[dict[str, str]] = []
        self.output_target = output_target
        self.id = uuid.uuid4().hex
        self._factory = factory
        # Only agent/MCP startup is subject to the 30-second startup deadline.
        # Initial web-context fetches run after startup and have their own
        # provider/network bounds rather than sharing that aggregate limit.
        self._startup_ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._ready: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._queue: asyncio.Queue[AgentRun | None] = asyncio.Queue(maxsize=1)
        self._task: asyncio.Task[None] | None = None
        self._closed = False
        self._busy = False
        self._run: AgentRun | None = None
        self.agent: Any = None
        self.broker = WebUiApprovalBroker(workspace=approval_workspace)
        self._approval_sender = self._send_approval
        self.broker.attach(self._approval_sender)

    async def start(self) -> None:
        self._task = asyncio.create_task(self._worker())
        try:
            await asyncio.wait_for(self._startup_ready, timeout=30)
            await self._ready
        except BaseException:
            await self.close()
            raise

    async def _worker(self) -> None:
        try:
            agent = self._factory(self)
            async with agent:
                self.agent = agent
                if not self._startup_ready.done():
                    self._startup_ready.set_result(None)
                for initial_command in self.initial_commands:
                    self.initial_messages.append(await agent.ask(initial_command))
                if not self._ready.done():
                    self._ready.set_result(None)
                while True:
                    run = await self._queue.get()
                    if run is None:
                        return
                    run.status = "running"
                    self._remember("user", run.prompt)
                    run.emit({"type": "RUN_STARTED", "threadId": self.id, "runId": run.id})
                    try:
                        answer = await agent.ask(run.prompt)
                        if not isinstance(answer, str):
                            raise TypeError("Agentenantwort muss Text sein.")
                        run.answer = answer
                        self._remember("assistant", answer)
                        message_id = uuid.uuid4().hex
                        run.emit({"type": "TEXT_MESSAGE_START", "messageId": message_id, "role": "assistant"})
                        run.emit({"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": answer})
                        run.emit({"type": "TEXT_MESSAGE_END", "messageId": message_id})
                        if self.output_target is not None:
                            from .file_context import is_local_agent_command
                            if not is_local_agent_command(run.prompt):
                                try:
                                    self.output_target.write_text(answer)
                                except Exception:
                                    run.emit({"type": "CUSTOM", "name": "output_error",
                                              "value": {"message": "Ausgabedatei konnte nicht geschrieben werden."}})
                        run.status = "completed"
                        run.emit({"type": "RUN_FINISHED", "threadId": self.id, "runId": run.id})
                    except Exception as exc:
                        run.status = "failed"
                        if self.debug_errors:
                            import traceback
                            run.error = "".join(traceback.format_exception(exc))
                        else:
                            run.error = f"{type(exc).__name__}: {exc}"
                        # Do not disclose internal exception details in stream events.
                        run.emit({"type": "RUN_ERROR", "message": "Agentenlauf fehlgeschlagen."})
                    finally:
                        self._busy = False
                        run.changed.set()
        except asyncio.CancelledError:
            if not self._startup_ready.done():
                self._startup_ready.cancel()
                self._ready.cancel()
            elif not self._ready.done():
                self._ready.cancel()
            if self._run is not None and not self._run.done:
                self._run.status = "cancelled"
                self._run.emit({"type": "RUN_ERROR", "message": "Agentenlauf abgebrochen."})
            raise
        except BaseException as exc:
            if not self._startup_ready.done():
                self._startup_ready.set_exception(exc)
                self._ready.cancel()
            elif not self._ready.done():
                self._ready.set_exception(exc)
            else:
                if self._run is not None and not self._run.done:
                    self._run.status = "failed"
                    self._run.error = "Agentenworker wurde beendet."
                    self._run.emit({"type": "RUN_ERROR", "message": "Agentenworker wurde beendet."})
        finally:
            self._closed = True
            self.broker.detach(self._approval_sender)
            if self._run is not None:
                self._run.changed.set()

    def _remember(self, role: str, content: str) -> None:
        # Bounded, in-memory chat display history. Never retain MCP payloads,
        # approval arguments or internal LLM context as browser transcript.
        self.ui_messages.append({"role": role, "content": content[:100_000]})
        if len(self.ui_messages) > 200:
            del self.ui_messages[:-200]

    def _replay_capacity(self) -> int:
        """Ensure a run's replay buffer covers configured tool-call budgets.

        A regular answer allows up to max_tool_calls in the main loop, and
        knowledge can run once up-front plus twice via re-entry. The buffer
        is not preallocated: only emitted events consume memory.
        """
        agent = self.agent
        main_limit = max(0, int(getattr(agent, "max_tool_calls", 200)))
        okf = getattr(agent, "_okf_options", None)
        vector = getattr(agent, "_vector_config", None)
        knowledge_limit = 0
        if okf is not None or vector is not None:
            knowledge_limit = max(0, int(getattr(okf, "max_tool_calls", 100)))
        tool_budget = main_limit + 3 * knowledge_limit
        calculated = tool_budget * REPLAY_EVENTS_PER_TOOL + REPLAY_LIFECYCLE_ALLOWANCE
        return max(MAX_EVENTS_PER_RUN, calculated)

    def submit(self, prompt: str) -> AgentRun:
        if self._closed:
            raise RuntimeError("Session ist geschlossen.")
        if self._busy:
            raise RuntimeError("Session bearbeitet bereits eine Anfrage.")
        self._busy = True
        run = AgentRun(
            id=uuid.uuid4().hex, thread_id=self.id, prompt=prompt,
            max_events=self._replay_capacity(),
        )
        self._run = run
        self._queue.put_nowait(run)
        return run

    def run(self, run_id: str) -> AgentRun | None:
        return self._run if self._run is not None and self._run.id == run_id else None

    async def on_agent_event(self, kind: str, payload: dict[str, Any]) -> None:
        run = self._run
        if run is None or run.done:
            return
        tool_id = str(payload.get("call_id") or uuid.uuid4().hex)
        name = str(payload.get("tool") or "")
        if kind == "tool_started":
            run.emit({"type": "TOOL_CALL_START", "toolCallId": tool_id, "toolCallName": name})
        elif kind == "tool_finished":
            run.emit({"type": "TOOL_CALL_END", "toolCallId": tool_id})
            # Keep tool results and arguments within the policy-controlled core.
            run.emit({"type": "CUSTOM", "name": "tool_status", "value": {
                "toolCallId": tool_id, "status": str(payload.get("status") or "finished")
            }})

    async def _send_user_message(self, payload: dict[str, Any]) -> None:
        if self._run is not None and not self._run.done:
            self._run.emit({"type": "CUSTOM", "name": "mcp_user_message", "value": payload})

    def subscribe(self, run: AgentRun) -> None:
        run.listeners += 1

    def unsubscribe(self, run: AgentRun) -> None:
        run.listeners -= 1
        if run.listeners < 0:
            raise RuntimeError("SSE-Listener-Zähler untergelaufen.")
        # Only losing the *current* run's final subscriber invalidates
        # pending approvals. Disconnects of earlier runs are irrelevant.
        if self._run is run and run.listeners == 0:
            self.broker.deny_all()

    async def _send_approval(self, payload: dict[str, Any]) -> None:
        run = self._run
        if run is None or run.done or run.listeners < 1:
            raise RuntimeError("Keine aktive UI-Verbindung für Tool-Freigabe.")
        # WebUiApprovalBroker prepares sanitized arguments and write previews.
        run.emit({"type": "CUSTOM", "name": "approval_required", "value": payload})

    async def approve(self, tool: str, arguments: dict[str, Any]) -> bool | str:
        run = self._run
        if run is None or run.done or run.listeners < 1:
            return False
        return await self.broker.approve_tool_call(tool, arguments)

    async def close(self) -> None:
        if self._closed and (self._task is None or self._task.done()):
            return
        self.broker.deny_all()
        task = self._task
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._closed = True


class AgentApiService:
    def __init__(self, factory: Callable[[AgentSession], Any], *, approval_workspace: Any = None, output_target: Any = None, initial_commands: tuple[str, ...] = (), debug_errors: bool = False) -> None:
        self.debug_errors = debug_errors
        self.initial_commands = initial_commands
        self.factory = factory
        self.output_target = output_target
        self.approval_workspace = approval_workspace
        # Slots are reserved before MCP startup (which may suspend indefinitely).
        self.sessions: dict[str, AgentSession] = {}
        self._lifecycle_lock = asyncio.Lock()
        self._shutting_down = False
        self.token = secrets.token_urlsafe(32)

    async def create(self) -> AgentSession:
        async with self._lifecycle_lock:
            if self._shutting_down:
                raise RuntimeError("API-Server wird beendet.")
            if len(self.sessions) >= MAX_SESSIONS:
                raise OverflowError("Maximale Anzahl von Sessions erreicht.")
            session = AgentSession(self.factory, approval_workspace=self.approval_workspace, output_target=self.output_target, initial_commands=self.initial_commands, debug_errors=self.debug_errors)
            self.sessions[session.id] = session

        try:
            await session.start()
        except BaseException:
            # Startup failures and request cancellations release capacity.
            async with self._lifecycle_lock:
                if self.sessions.get(session.id) is session:
                    del self.sessions[session.id]
            await session.close()
            raise

        async with self._lifecycle_lock:
            active = not self._shutting_down and self.sessions.get(session.id) is session
        if not active:
            await session.close()
            raise RuntimeError("Session wurde beim Start geschlossen.")
        return session

    async def remove(self, session_id: str) -> bool:
        async with self._lifecycle_lock:
            session = self.sessions.pop(session_id, None)
        if session is None:
            return False
        await session.close()
        return True

    async def shutdown(self) -> None:
        async with self._lifecycle_lock:
            self._shutting_down = True
            sessions = tuple(self.sessions.values())
            self.sessions.clear()
        # Include sessions whose start() is still in progress.
        for session in sessions:
            await session.close()


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

    async def create_session(_request: Request) -> Response:
        try:
            session = await service.create()
        except OverflowError:
            return error(429, "Zu viele Sessions.")
        except Exception:
            return error(503, "Agenten-Session konnte nicht gestartet werden.")
        return JSONResponse({"session_id": session.id}, status_code=201)

    async def delete_session(request: Request) -> Response:
        if not await service.remove(request.path_params["session_id"]):
            return error(404, "Session nicht gefunden.")
        return Response(status_code=204)

    async def start_run(request: Request) -> Response:
        try:
            async with session_from(request) as session:
                async with checked_json(request) as body:
                    if set(body) != {"prompt"} or not isinstance(body["prompt"], str):
                        return error(400, "Genau ein Textfeld 'prompt' wird benötigt.")
                    prompt = body["prompt"]
                    if not prompt.strip() or len(prompt.encode("utf-8")) > MAX_PROMPT_BYTES:
                        return error(400, "Prompt ist leer oder zu groß.")
                    try:
                        run = session.submit(prompt)
                    except RuntimeError:
                        return error(409, "Session ist beschäftigt oder geschlossen.")
                    return JSONResponse({"run_id": run.id, "status": run.status}, status_code=202)
        except KeyError:
            return error(404, "Session nicht gefunden.")
        except ValueError as exc:
            return error(400, str(exc))

    def fetch_run(request: Request) -> tuple[AgentSession | None, AgentRun | None]:
        session = service.sessions.get(request.path_params["session_id"])
        if session is None:
            return None, None
        return session, session.run(request.path_params["run_id"])

    async def run_status(request: Request) -> Response:
        _session, run = fetch_run(request)
        if run is None:
            return error(404, "Run nicht gefunden.")
        return JSONResponse(run.snapshot())

    async def run_events(request: Request) -> Response:
        session, run = fetch_run(request)
        if run is None or session is None:
            return error(404, "Run nicht gefunden.")
        try:
            last = int(request.headers.get("last-event-id", "0"))
        except ValueError:
            return error(400, "Ungültige Event-ID.")
        if last < 0 or last >= run.next_sequence:
            return error(400, "Ungültige Event-ID.")
        if run.events and last < run.events[0][0] - 1:
            return error(410, "Event-History ist nicht mehr vollständig.")

        browser_stream = (
            ui is not None and request.headers.get("x-cli-agent-browser") == "1"
        )
        browser_lease = request.headers.get("x-cli-agent-lease")

        async def stream():
            cursor = last
            session.subscribe(run)
            try:
                while True:
                    # A request authorized when established must not keep an SSE
                    # subscription after its tab's exclusive lease has expired.
                    if browser_stream and not ui["registry"].is_owner(
                        session.id, browser_lease, refresh=False
                    ):
                        return
                    if run.events and cursor < run.events[0][0] - 1:
                        return
                    for seq, event in tuple(run.events):
                        if seq > cursor:
                            cursor = seq
                            # Replayed approval requests must remain actionable.
                            # Resolved/denied requests are historical audit
                            # events, not interactive dialogs.
                            if (event.get("type") == "CUSTOM"
                                    and event.get("name") == "approval_required"):
                                request_id = str(event.get("value", {}).get("id", ""))
                                if not session.broker.is_pending(request_id):
                                    continue
                            yield f"id: {seq}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if run.done:
                        return
                    run.changed.clear()
                    # New events may have arrived while yield suspended us.
                    # Recheck after clear to avoid an indefinitely lost wakeup.
                    if run.next_sequence > cursor + 1 or run.done:
                        continue
                    try:
                        await asyncio.wait_for(run.changed.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        pass
            finally:
                session.unsubscribe(run)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    async def approve(request: Request) -> Response:
        session = service.sessions.get(request.path_params["session_id"])
        if session is None or session.run(request.path_params["run_id"]) is None:
            return error(404, "Run nicht gefunden.")
        if session._run is None or session._run.done:
            return error(409, "Run ist nicht aktiv.")
        try:
            async with checked_json(request) as body:
                if set(body) != {"decision"} or body["decision"] not in ("yes", "no", "session"):
                    return error(400, "Entscheidung muss yes, no oder session sein.")
                if (session._run is None or session._run.id != request.path_params["run_id"]
                        or session._run.done or session._closed):
                    return error(409, "Run ist nicht aktiv.")
                if not session.broker.resolve(request.path_params["approval_id"], body["decision"]):
                    return error(409, "Freigabe ist ungültig oder abgelaufen.")
                return JSONResponse({"accepted": True})
        except ValueError as exc:
            return error(400, str(exc))

    async def context(request: Request) -> Response:
        session = service.sessions.get(request.path_params["session_id"])
        if session is None:
            return error(404, "Session nicht gefunden.")
        agent = session.agent
        return JSONResponse({
            "tools": agent.tool_states_snapshot(),
            "contexts": agent.context_states_snapshot(),
            "okf": agent.okf_status_snapshot(),
        })

    async def update_context(request: Request) -> Response:
        session = service.sessions.get(request.path_params["session_id"])
        if session is None:
            return error(404, "Session nicht gefunden.")
        if session._busy:
            return error(409, "Änderungen während eines Runs sind nicht erlaubt.")
        try:
            async with checked_json(request) as body:
                if not body or set(body) - {"tools", "contexts", "okf"}:
                    return error(400, "Unbekannte Context-Felder.")
                tools = body.get("tools", {})
                contexts = body.get("contexts", {})
                if not isinstance(tools, dict) or not isinstance(contexts, dict):
                    return error(400, "Tools und Contexts müssen Objekte sein.")
                if any(not isinstance(v, bool) for v in [*tools.values(), *contexts.values()]):
                    return error(400, "Aktivierungszustände müssen boolesch sein.")
                agent = session.agent
                available_tools = {item["name"] for item in agent.tool_states_snapshot()}
                available_contexts = {item["id"] for item in agent.context_states_snapshot()}
                if set(tools) - available_tools or set(contexts) - available_contexts:
                    return error(400, "Unbekanntes Tool oder Context.")
                okf = body.get("okf")
                if "okf" in body and (not isinstance(okf, bool) or not agent.okf_status_snapshot()["configured"]):
                    return error(400, "OKF ist nicht konfiguriert oder Zustand ungültig.")
                # checked_json() awaits the request body. A run may have
                # started since the initial busy check above. No awaits between
                # this check and the synchronous state mutations.
                if session._busy or session._closed:
                    return error(409, "Änderungen während eines Runs sind nicht erlaubt.")
                for name, enabled in tools.items():
                    agent.set_tool_enabled(name, enabled=enabled)
                for context_id, enabled in contexts.items():
                    agent.set_context_enabled(context_id, enabled=enabled)
                if "okf" in body:
                    agent.set_okf_enabled(enabled=okf)
                return await context(request)
        except ValueError as exc:
            return error(400, str(exc))

    async def working_context(request: Request) -> Response:
        session = service.sessions.get(request.path_params["session_id"])
        if session is None:
            return error(404, "Session nicht gefunden.")
        agent = session.agent
        return JSONResponse({
            "messages": agent.working_messages_snapshot(),
            "tools": agent.tool_states_snapshot(),
            "contexts": agent.context_states_snapshot(),
            "okf": agent.okf_status_snapshot(),
        })

    async def reset_history(request: Request) -> Response:
        session = service.sessions.get(request.path_params["session_id"])
        if session is None:
            return error(404, "Session nicht gefunden.")
        if session._busy or session._closed:
            return error(409, "Session ist beschäftigt oder geschlossen.")
        session.broker.deny_all()
        session.agent.reset_history()
        session.ui_messages.clear()
        return JSONResponse({"reset": True})

    async def usage(request: Request) -> Response:
        session = service.sessions.get(request.path_params["session_id"])
        if session is None:
            return error(404, "Session nicht gefunden.")
        return JSONResponse({"text": session.agent.token_usage_text()})

    def browser_security_headers(response: Response) -> Response:
        from .web_ui import _security_headers
        response = _security_headers(response)
        # The API-backed browser makes same-origin fetch requests only.
        response.headers["Content-Security-Policy"] = (
            response.headers["Content-Security-Policy"].replace(" ws://127.0.0.1:*", "")
        )
        return response

    async def ui_index(_request: Request) -> Response:
        from .web_ui import INDEX_HTML
        return browser_security_headers(HTMLResponse(INDEX_HTML))

    async def ui_css(_request: Request) -> Response:
        from .web_ui import APP_CSS
        return browser_security_headers(PlainTextResponse(APP_CSS, media_type="text/css"))

    async def ui_js(_request: Request) -> Response:
        from .web_ui_api import API_APP_JS
        return browser_security_headers(PlainTextResponse(API_APP_JS, media_type="application/javascript"))

    async def ui_bootstrap(request: Request) -> Response:
        assert ui is not None
        from .web_ui_sessions import SessionOccupiedError
        registry = ui["registry"]
        try:
            async with checked_json(request) as body:
                if set(body) - {"session_id", "ticket"}:
                    return error(400, "Unbekannte Session-Parameter.")
                ticket = body.get("ticket")
                if ticket is not None:
                    if not isinstance(ticket, str):
                        return error(400, "Ungültiges Session-Ticket.")
                    session_id, lease = registry.redeem(ticket)
                else:
                    session_id = body.get("session_id", ui["session_id"])
                    if not isinstance(session_id, str):
                        return error(400, "Ungültige Session-ID.")
                    lease = registry.claim(session_id)
            session = service.sessions[session_id]
            return JSONResponse({
                "session_id": session_id, "lease": lease,
                **ui["info"], "initial_messages": session.initial_messages,
                "history": session.ui_messages,
                "active_run": (session._run.id if session._run is not None
                               and not session._run.done else None),
            })
        except SessionOccupiedError as exc:
            return error(409, str(exc))
        except KeyError:
            return error(404, "Session nicht gefunden.")
        except ValueError as exc:
            return error(400, str(exc))

    async def ui_sessions(_request: Request) -> Response:
        assert ui is not None
        return JSONResponse({"sessions": ui["registry"].list_sessions()})

    async def ui_session_new(_request: Request) -> Response:
        assert ui is not None
        from .web_ui_sessions import SessionOccupiedError
        registry = ui["registry"]
        try:
            session = await service.create()
        except OverflowError:
            return error(429, "Maximale Anzahl von Sessions erreicht.")
        except Exception:
            return error(503, "Session konnte nicht gestartet werden.")
        registry.register(session.id)
        try:
            ticket = registry.invitation(session.id)
        except (SessionOccupiedError, KeyError):
            registry.forget(session.id)
            await service.remove(session.id)
            return error(409, "Session konnte nicht geöffnet werden.")
        return JSONResponse({"session_id": session.id, "ticket": ticket}, status_code=201)

    async def ui_session_ticket(request: Request) -> Response:
        assert ui is not None
        from .web_ui_sessions import SessionOccupiedError
        try:
            ticket = ui["registry"].invitation(request.path_params["session_id"])
        except KeyError:
            return error(404, "Session nicht gefunden.")
        except SessionOccupiedError as exc:
            return error(409, str(exc))
        return JSONResponse({"ticket": ticket})

    async def ui_session_delete(request: Request) -> Response:
        assert ui is not None
        registry = ui["registry"]
        session_id = request.path_params["session_id"]
        try:
            owner = registry.is_owner(
                session_id, request.headers.get("x-cli-agent-lease"), refresh=False
            )
            if owner:
                if service.sessions[session_id]._busy:
                    return error(409, "Session ist beschäftigt.")
            elif not registry.can_close(session_id):
                return error(409, "Session ist in einem anderen Tab geöffnet oder beschäftigt.")
        except KeyError:
            return error(404, "Session nicht gefunden.")
        registry.forget(session_id)
        if not await service.remove(session_id):
            return error(404, "Session nicht gefunden.")
        return Response(status_code=204)

    async def ui_release(request: Request) -> Response:
        assert ui is not None
        try:
            async with checked_json(request) as body:
                if set(body) != {"session_id"} or not isinstance(body["session_id"], str):
                    return error(400, "Session-ID erforderlich.")
                if not ui["registry"].release(
                    body["session_id"], request.headers.get("x-cli-agent-lease")
                ):
                    return error(409, "Keine aktive Session-Bindung.")
                return Response(status_code=204)
        except ValueError as exc:
            return error(400, str(exc))

    async def ui_heartbeat(request: Request) -> Response:
        assert ui is not None
        try:
            async with checked_json(request) as body:
                if set(body) != {"session_id"} or not isinstance(body["session_id"], str):
                    return error(400, "Session-ID erforderlich.")
                if not ui["registry"].is_owner(
                    body["session_id"], request.headers.get("x-cli-agent-lease")
                ):
                    return error(409, "Session-Bindung ist abgelaufen.")
                return Response(status_code=204)
        except ValueError as exc:
            return error(400, str(exc))

    async def ui_render(request: Request) -> Response:
        from .web_ui import _render_markdown
        try:
            async with checked_json(request) as body:
                if set(body) != {"text"} or not isinstance(body["text"], str):
                    return error(400, "Text erwartet.")
                if len(body["text"].encode("utf-8")) > MAX_PROMPT_BYTES:
                    return error(400, "Text zu groß.")
                return JSONResponse({"html": _render_markdown(body["text"])})
        except ValueError as exc:
            return error(400, str(exc))

    async def ui_shutdown(_request: Request) -> Response:
        assert ui is not None
        # Closing the local application terminates every UI session.
        for session in tuple(service.sessions.values()):
            session.broker.deny_all()
        ui["shutdown"]()
        return JSONResponse({"shutdown": True})

    async def health(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def authenticate(request: Request, call_next: Any) -> Response:
        # Reject rebinding / proxies even for public static assets.
        host = request.headers.get("host", "").split(":")[0].lower()
        if host != API_HOST or request.headers.get("x-forwarded-host") or request.headers.get("forwarded"):
            return error(403, "Unzulässiger Host.")
        path = request.url.path
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
            and (path.startswith("/ui/") or path.startswith("/sessions/"))
        )
        if browser:
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
            Route("/sessions", create_session, methods=["POST"]),
            Route("/sessions/{session_id}", delete_session, methods=["DELETE"]),
            Route("/sessions/{session_id}/context", context, methods=["GET"]),
            Route("/sessions/{session_id}/working-context", working_context, methods=["GET"]),
            Route("/sessions/{session_id}/reset", reset_history, methods=["POST"]),
            Route("/sessions/{session_id}/usage", usage, methods=["GET"]),
            Route("/sessions/{session_id}/context", update_context, methods=["PATCH"]),
            Route("/sessions/{session_id}/runs", start_run, methods=["POST"]),
            Route("/sessions/{session_id}/runs/{run_id}", run_status, methods=["GET"]),
            Route("/sessions/{session_id}/runs/{run_id}/events", run_events, methods=["GET"]),
            Route("/sessions/{session_id}/runs/{run_id}/approvals/{approval_id}", approve, methods=["POST"]),
        ] + ([
            Route("/", ui_index, methods=["GET"]),
            Route("/assets/app.css", ui_css, methods=["GET"]),
            Route("/assets/app.js", ui_js, methods=["GET"]),
            Route("/ui/bootstrap", ui_bootstrap, methods=["POST"]),
            Route("/ui/sessions", ui_sessions, methods=["GET"]),
            Route("/ui/sessions", ui_session_new, methods=["POST"]),
            Route("/ui/sessions/{session_id}/ticket", ui_session_ticket, methods=["POST"]),
            Route("/ui/sessions/{session_id}", ui_session_delete, methods=["DELETE"]),
            Route("/ui/release", ui_release, methods=["POST"]),
            Route("/ui/heartbeat", ui_heartbeat, methods=["POST"]),
            Route("/ui/render", ui_render, methods=["POST"]),
            Route("/ui/shutdown", ui_shutdown, methods=["POST"]),
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
    return parser


def build_service(args: argparse.Namespace) -> AgentApiService:
    from .admin_config import load_admin_config
    from .config import McpServerConfig, default_config_file, load_config
    from .execution import (
        apply_workspace_access_override, apply_data_override,
        apply_named_mcp_overrides, resolve_excluded_paths,
    )
    from .file_context import ContextFileCliAgent
    from .model_factory import create_model_client
    from .os_operations import Workspace
    from .logging_setup import configure_logging

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
            config_file=config_file,
            # API sessions never write LLM context dumps, even when enabled
            # in config.toml for CLI or flow execution.
            dump_llm_context=False,
            network=admin.network,
            web_providers=admin.web.providers,
            mcp_policy=admin.mcp,
            approval_callback=session.approve,
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
