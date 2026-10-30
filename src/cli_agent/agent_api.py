"""Local, authenticated AG-UI 1.0 API with per-session MCP lifetimes.

The canonical transport accepts RunAgentInput via HTTP POST and responds
with SSE, including standard client-tool results and interrupt/resume. Local
cli-agent configuration remains separate from the AG-UI run contract.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
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
# Browsers commonly permit six HTTP/1.1 connections per origin. Keep at
# least two free for approval POSTs, session heartbeat and UI controls.
MAX_CONCURRENT_BROWSER_RUNS = 4
MAX_PROMPT_BYTES = 512 * 1024
# Replay keeps a full supported run, including the optional initial knowledge
# phase and its two permitted re-entry searches. Five events per tool covers
# start, end, status, MCP user notice, and approval request.
MAX_EVENTS_PER_RUN = 8_192
REPLAY_EVENTS_PER_TOOL = 5
REPLAY_LIFECYCLE_ALLOWANCE = 32
MAX_JSON_BYTES = 1024 * 1024
# A UI run must not execute before its SSE consumer is attached.
# A bounded wait prevents abandoned/browser-blocked requests from hanging a session.
BROWSER_SSE_CONNECT_TIMEOUT = 15.0


def _artifact_thread_token(thread_id: str) -> str:
    """Opaque URL-safe segment for any accepted AG-UI threadId.

    Percent encoding is insufficient: browsers normalize dot segments and
    servers may decode %2F before routing. Base64url avoids both problems.
    """
    return "~" + base64.urlsafe_b64encode(thread_id.encode("utf-8")).decode("ascii").rstrip("=")


def _artifact_thread_from_token(token: str) -> str | None:
    """Decode only canonical tokens for valid AG-UI thread IDs."""
    if not token.startswith("~") or len(token) > 1024:
        return None
    encoded = token[1:]
    try:
        thread_id = base64.urlsafe_b64decode(
            encoded + "=" * (-len(encoded) % 4)
        ).decode("utf-8")
    except (ValueError, UnicodeError):
        return None
    if not 0 < len(thread_id) <= 128 or _artifact_thread_token(thread_id) != token:
        return None
    return thread_id


@dataclass
class AgentRun:
    id: str
    thread_id: str
    prompt: str
    status: str = "queued"
    # Subscribers belong to this run, not the parent session. A slow
    # subscriber draining a previous run must never authorize a new run.
    listeners: int = 0
    # Only embedded-browser runs require an SSE subscriber before execution.
    # Headless API runs retain their existing fire-and-poll semantics.
    require_listener: bool = False
    agui: bool = False
    resume_approval: tuple[str, str] | None = None
    frontend_results: list[dict[str, Any]] | None = None
    subscriber_ready: asyncio.Event = field(default_factory=asyncio.Event)
    answer: str | None = None
    error: str | None = None
    events: list[tuple[int, dict[str, Any]]] = field(default_factory=list)
    max_events: int = MAX_EVENTS_PER_RUN
    next_sequence: int = 1
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    @property
    def done(self) -> bool:
        return self.status in {"completed", "failed", "cancelled", "interrupted"}

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
        self.agui_messages: list[dict[str, Any]] = []
        self.agui_state: Any = {}
        self.agui_context: list[dict[str, Any]] = []
        self._pending_interrupt: dict[str, Any] | None = None
        self._pending_frontend: tuple[Any, ...] | None = None
        self.published_artifacts: dict[str, dict[str, str]] = {}
        self._published_tool_calls: dict[str, str] = {}
        self._pending_frontend_thread_id: str | None = None
        self._pending_tool_id: str | None = None
        self._interrupt_tool_ids: set[str] = set()
        self._agui_seen_run_ids: set[str] = set()
        self._opened_tool_calls: set[str] = set()
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
                    run.emit({"type": "RUN_STARTED", "threadId": run.thread_id, "runId": run.id,
                              **({"protocolVersion": "1.0"} if run.agui else {})})
                    try:
                        if run.require_listener:
                            await self._await_run_subscriber(run)
                        if run.frontend_results is not None:
                            answer = await agent.continue_after_frontend_tools(
                                run.frontend_results
                            )
                        else:
                            # Each new question expires downloads from the prior
                            # question, matching MCP artifact cleanup semantics.
                            self.published_artifacts.clear()
                            self._published_tool_calls.clear()
                            self._remember("user", run.prompt)
                            answer = await agent.ask(run.prompt)
                        if not isinstance(answer, str):
                            raise TypeError("Agentenantwort muss Text sein.")
                        # An AG-UI approval may have ended one or more earlier
                        # run segments. The coroutine itself is still running
                        # on this worker; its output belongs to the latest
                        # resumed segment, not to the interrupted original.
                        finished = self._run if run.agui else run
                        if finished is None:
                            raise RuntimeError("Run wurde beendet.")
                        finished.answer = answer
                        self._remember("assistant", answer)
                        message_id = uuid.uuid4().hex
                        finished.emit({"type": "TEXT_MESSAGE_START", "messageId": message_id, "role": "assistant"})
                        finished.emit({"type": "TEXT_MESSAGE_CONTENT", "messageId": message_id, "delta": answer})
                        finished.emit({"type": "TEXT_MESSAGE_END", "messageId": message_id})
                        if finished.agui:
                            self.agui_messages.append({"id": message_id, "role": "assistant", "content": answer})
                        if self.output_target is not None:
                            from .file_context import is_local_agent_command
                            if not is_local_agent_command(run.prompt):
                                try:
                                    self.output_target.write_text(answer)
                                except Exception:
                                    finished.emit({"type": "CUSTOM", "name": "output_error",
                                              "value": {"message": "Ausgabedatei konnte nicht geschrieben werden."}})
                        finished.status = "completed"
                        finished.emit({"type": "RUN_FINISHED", "threadId": finished.thread_id, "runId": finished.id,
                                       **({"outcome": {"type": "success"}} if finished.agui else {})})
                    except Exception as exc:
                        from .ag_ui_frontend_tools import FrontendToolHandoff
                        if isinstance(exc, FrontendToolHandoff) and run.agui:
                            yielded = self._run
                            if yielded is None or yielded.done:
                                run.status = "failed"
                                run.emit({"type": "RUN_ERROR",
                                          "message": "Frontend-Tool-Run ist nicht verfügbar."})
                                continue
                            pending = tuple(exc.calls)
                            if not pending:
                                yielded.status = "failed"
                                yielded.emit({"type": "RUN_ERROR",
                                              "message": "Leerer Frontend-Tool-Handoff."})
                                continue
                            existing_ids = {
                                event.get("toolCallId")
                                for _, event in yielded.events
                                if event.get("type") == "TOOL_CALL_START"
                            }
                            offered_ids = [call.id for call in pending]
                            if (len(set(offered_ids)) != len(offered_ids)
                                    or existing_ids.intersection(offered_ids)):
                                yielded.status = "failed"
                                yielded.emit({"type": "RUN_ERROR",
                                              "message": "Doppelte Tool-Call-ID im Frontend-Handoff."})
                                continue
                            self._pending_frontend = pending
                            self._pending_frontend_thread_id = yielded.thread_id
                            calls = []
                            handoff_text = str(exc.assistant.get("content") or "")
                            message_id = uuid.uuid4().hex
                            if handoff_text:
                                yielded.emit({
                                    "type": "TEXT_MESSAGE_START", "messageId": message_id,
                                    "role": "assistant",
                                })
                                yielded.emit({
                                    "type": "TEXT_MESSAGE_CONTENT", "messageId": message_id,
                                    "delta": handoff_text,
                                })
                                yielded.emit({
                                    "type": "TEXT_MESSAGE_END", "messageId": message_id,
                                })
                            for call in pending:
                                yielded.emit({
                                    "type": "TOOL_CALL_START", "toolCallId": call.id,
                                    "toolCallName": call.name,
                                })
                                yielded.emit({
                                    "type": "TOOL_CALL_ARGS", "toolCallId": call.id,
                                    "delta": call.arguments,
                                })
                                yielded.emit({"type": "TOOL_CALL_END", "toolCallId": call.id})
                                calls.append({
                                    "id": call.id, "type": "function",
                                    "function": {
                                        "name": call.name, "arguments": call.arguments,
                                    },
                                })
                            self.agui_messages.append({
                                "id": message_id, "role": "assistant",
                                "content": handoff_text, "toolCalls": calls,
                            })
                            yielded.status = "completed"
                            yielded.emit({
                                "type": "RUN_FINISHED", "threadId": yielded.thread_id,
                                "runId": yielded.id,
                                "outcome": {
                                    "type": "success",
                                    "pendingToolCallIds": [call.id for call in pending],
                                },
                            })
                        else:
                            failed = self._run if run.agui else run
                            if failed is not None:
                                failed.status = "failed"
                                if self.debug_errors:
                                    import traceback
                                    failed.error = "".join(traceback.format_exception(exc))
                                else:
                                    failed.error = f"{type(exc).__name__}: {exc}"
                                failed.emit({"type": "RUN_ERROR", "message": "Agentenlauf fehlgeschlagen."})
                        # The exception is now handled; no frontend tool is
                        # ever executed by the MCP dispatcher.
                    finally:
                        self._busy = False
                        self._pending_interrupt = None
                        self._opened_tool_calls.clear()
                        self._interrupt_tool_ids.clear()
                        run.changed.set()
                        if self._run is not None:
                            self._run.changed.set()
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

    async def _await_run_subscriber(self, run: AgentRun) -> None:
        """Wait until an actual SSE listener exists, before executing a UI run.

        Do not confuse a previous run's listener or a stale/disconnected
        subscriber with an active consumer of *this* run.
        """
        deadline = asyncio.get_running_loop().time() + BROWSER_SSE_CONNECT_TIMEOUT
        while run.listeners == 0:
            run.subscriber_ready.clear()
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError("Kein SSE-Client für den Agentenlauf verbunden.")
            try:
                await asyncio.wait_for(run.subscriber_ready.wait(), timeout=remaining)
            except asyncio.TimeoutError as exc:
                raise TimeoutError(
                    "Kein SSE-Client für den Agentenlauf verbunden."
                ) from exc

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

    def submit(self, prompt: str, *, require_listener: bool = False,
               agui: bool = False, thread_id: str | None = None,
               run_id: str | None = None,
               frontend_results: list[dict[str, Any]] | None = None) -> AgentRun:
        if self._closed:
            raise RuntimeError("Session ist geschlossen.")
        if self._busy:
            raise RuntimeError("Session bearbeitet bereits eine Anfrage.")
        self._busy = True
        run = AgentRun(
            id=run_id or uuid.uuid4().hex, thread_id=thread_id or self.id, prompt=prompt,
            max_events=self._replay_capacity(),
            require_listener=require_listener, agui=agui,
            frontend_results=frontend_results,
        )
        if agui:
            if run.id in self._agui_seen_run_ids:
                self._busy = False
                raise ValueError("runId wurde bereits verwendet.")
            self._agui_seen_run_ids.add(run.id)
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
            self._pending_tool_id = tool_id
            if run.agui and tool_id in self._interrupt_tool_ids:
                # The interrupt's preceding run already emitted the complete
                # start/arguments/end trio for this call ID.
                return
            self._opened_tool_calls.add(tool_id)
            run.emit({"type": "TOOL_CALL_START", "toolCallId": tool_id, "toolCallName": name})
            if run.agui:
                # Arguments may be unavailable at start; they are supplied by
                # the approval broker (which sanitizes them) if approval is
                # required, or closed on finish for non-approval calls.
                return
        elif kind == "artifact_published":
            # An AG-UI CUSTOM event offers only an ID and presentation metadata.
            # Do not expose local paths or the API credential to browser clients.
            artifact_id = payload["id"]
            self._published_tool_calls[tool_id] = artifact_id
            self.published_artifacts[artifact_id] = {
                "path": payload["path"], "filename": payload["filename"],
                "mime_type": payload["mime_type"],
            }
        elif kind == "tool_finished":
            if run.agui:
                self._interrupt_tool_ids.discard(tool_id)
                if tool_id in self._opened_tool_calls:
                    run.emit({"type": "TOOL_CALL_END", "toolCallId": tool_id})
                    self._opened_tool_calls.discard(tool_id)
                if self._pending_tool_id == tool_id:
                    self._pending_tool_id = None
                # Only publish safe status, never arbitrary MCP payload data.
                published_id = self._published_tool_calls.pop(tool_id, None)
                if published_id is not None and payload.get("status") == "success":
                    document = self.published_artifacts[published_id]
                    run.emit({"type": "TOOL_CALL_RESULT",
                              "toolCallId": tool_id, "messageId": uuid.uuid4().hex,
                              "content": [
                                  {"type": "text", "text": "Datei zum Download bereit."},
                                  {"type": "document", "source": {
                                      "type": "url",
                                      "value": "/threads/" + _artifact_thread_token(run.thread_id) + "/artifacts/" + published_id,
                                      "mimeType": document["mime_type"],
                                  }, "metadata": {
                                      "filename": document["filename"],
                                      "artifactId": published_id,
                                  }},
                              ]})
                else:
                    run.emit({"type": "TOOL_CALL_RESULT",
                              "toolCallId": tool_id, "messageId": uuid.uuid4().hex,
                              "content": str(payload.get("status") or "finished")})
            else:
                run.emit({"type": "TOOL_CALL_END", "toolCallId": tool_id})
                run.emit({"type": "CUSTOM", "name": "tool_status", "value": {
                    "toolCallId": tool_id, "status": str(payload.get("status") or "finished")
                }})

    async def _send_user_message(self, payload: dict[str, Any]) -> None:
        if self._run is not None and not self._run.done:
            self._run.emit({"type": "CUSTOM", "name": "mcp_user_message", "value": payload})

    def subscribe(self, run: AgentRun) -> None:
        run.listeners += 1
        run.subscriber_ready.set()

    def unsubscribe(self, run: AgentRun) -> None:
        run.listeners -= 1
        if run.listeners < 0:
            raise RuntimeError("SSE-Listener-Zähler untergelaufen.")
        # Only losing the *current* run's final subscriber invalidates
        # pending approvals. Disconnects of earlier runs are irrelevant.
        if run.listeners == 0:
            run.subscriber_ready.clear()
            if self._run is run and run.status != "interrupted":
                self.broker.deny_all()

    async def _send_approval(self, payload: dict[str, Any]) -> None:
        run = self._run
        if run is None or run.done or run.listeners < 1:
            raise RuntimeError("Keine aktive UI-Verbindung für Tool-Freigabe.")
        # WebUiApprovalBroker prepares sanitized arguments and write previews.
        if run.agui:
            self._interrupt_for_approval(run, payload)
        else:
            run.emit({"type": "CUSTOM", "name": "approval_required", "value": payload})

    async def approve(self, tool: str, arguments: dict[str, Any]) -> bool | str:
        run = self._run
        if run is None or run.done or run.listeners < 1:
            return False
        return await self.broker.approve_tool_call(tool, arguments)

    def _interrupt_for_approval(self, run: AgentRun, payload: dict[str, Any]) -> None:
        """Terminate AG-UI run with a human-in-the-loop interrupt.

        The already-running agent coroutine remains suspended inside the
        policy-controlled approval broker until a *new* AG-UI run resumes it.
        """
        call_id = getattr(self.agent, "_agui_approval_call_id", None)
        if not isinstance(call_id, str) or not call_id:
            call_id = self._pending_tool_id or uuid.uuid4().hex
        self._interrupt_tool_ids.add(call_id)
        if call_id not in self._opened_tool_calls:
            run.emit({"type": "TOOL_CALL_START", "toolCallId": call_id,
                      "toolCallName": payload.get("tool", "")})
        argument_text = payload.get("arguments", {})
        run.emit({"type": "TOOL_CALL_ARGS", "toolCallId": call_id,
                  "delta": argument_text if isinstance(argument_text, str)
                  else json.dumps(argument_text, ensure_ascii=False)})
        run.emit({"type": "TOOL_CALL_END", "toolCallId": call_id})
        self._opened_tool_calls.discard(call_id)
        self._pending_tool_id = call_id
        interrupt = {
            "id": payload["id"],
            "reason": "tool_call",
            "toolCallId": call_id,
            "message": f"Freigabe für {payload.get('tool', 'Tool')} erforderlich.",
            "responseSchema": {
                "type": "object",
                "properties": {
                    "approved": {"type": "boolean"},
                    "rememberForSession": {"type": "boolean"},
                },
                "required": ["approved"],
            },
            "metadata": {"cli-agent": {
                key: value for key, value in payload.items()
                if key not in {"id", "tool"}
            }},
        }
        self._pending_interrupt = interrupt
        run.status = "interrupted"
        run.emit({"type": "RUN_FINISHED", "threadId": run.thread_id,
                  "runId": run.id, "outcome": {
                      "type": "interrupt", "interrupts": [interrupt],
                  }})

    def resume_agui(self, *, thread_id: str, run_id: str,
                    response: dict[str, Any]) -> AgentRun:
        """Reserve the next segment; approval is not applied until SSE attaches."""
        pending = self._pending_interrupt
        if not self._busy or pending is None or self._run is None:
            raise RuntimeError("Kein fortsetzbarer Interrupt vorhanden.")
        if self._run.status != "interrupted" or self._run.thread_id != thread_id:
            raise RuntimeError("Die vorherige Ausführung ist nicht unterbrochen.")
        if response.get("interruptId") != pending["id"]:
            raise ValueError("Die Interrupt-ID stimmt nicht überein.")
        if response.get("status") == "resolved":
            payload = response.get("payload")
            if not isinstance(payload, dict) or type(payload.get("approved")) is not bool:
                raise ValueError("Ein boolesches approved-Feld wird benötigt.")
            decision = (
                "session" if payload["approved"] and payload.get("rememberForSession") is True
                else "yes" if payload["approved"] else "no"
            )
        elif response.get("status") == "cancelled":
            decision = "no"
        else:
            raise ValueError("Ungültiger Resume-Status.")
        next_run = AgentRun(
            id=run_id, thread_id=thread_id, prompt="",
            max_events=self._replay_capacity(), agui=True,
            require_listener=True,
            status="running",
            resume_approval=(pending["id"], decision),
        )
        if run_id in self._agui_seen_run_ids:
            raise ValueError("runId wurde bereits verwendet.")
        self._agui_seen_run_ids.add(run_id)
        self._run = next_run
        next_run.emit({"type": "RUN_STARTED", "threadId": thread_id,
                       "runId": run_id, "protocolVersion": "1.0"})
        self._pending_interrupt = None
        return next_run

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
        self._agui_thread_lock = asyncio.Lock()
        self._agui_threads: dict[str, str] = {}
        # One shared startup task per external thread; unrelated threads
        # must never wait for each other's MCP initialization.
        self._agui_thread_inits: dict[str, asyncio.Task[AgentSession]] = {}
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

    async def create_standalone_session(self) -> AgentSession:
        """Create a managed API session whose ID is also its AG-UI threadId.

        Only the standalone /sessions endpoint registers this alias. Browser
        sessions, created independently by the UI registry, remain isolated.
        """
        session = await self.create()
        async with self._agui_thread_lock:
            if (not self._shutting_down
                    and self.sessions.get(session.id) is session):
                self._agui_threads[session.id] = session.id
                return session
        # Shutdown/removal raced with startup; never return an orphan ID.
        await self.remove(session.id)
        raise RuntimeError("Session wurde beim Start geschlossen.")

    async def _start_agui_thread(self, thread_id: str) -> AgentSession:
        """Start one external thread, publishing it only while still reserved."""
        session: AgentSession | None = None
        try:
            session = await self.create()
            async with self._agui_thread_lock:
                if (self._agui_thread_inits.get(thread_id) is not asyncio.current_task()
                        or self._shutting_down
                        or self.sessions.get(session.id) is not session):
                    raise RuntimeError("AG-UI-Thread wurde beim Start geschlossen.")
                self._agui_threads[thread_id] = session.id
            return session
        except BaseException:
            # A concurrent delete/shutdown must not leave a started, unowned
            # MCP worker behind even if startup completed just beforehand.
            if session is not None:
                await self.remove(session.id)
            raise
        finally:
            async with self._agui_thread_lock:
                if self._agui_thread_inits.get(thread_id) is asyncio.current_task():
                    del self._agui_thread_inits[thread_id]

    async def resolve_agui_thread(self, thread_id: str) -> AgentSession:
        """Map external thread IDs to sessions without serializing MCP startup.

        Concurrent requests for the same thread await the same initialization.
        Shielding prevents cancellation of one HTTP client from aborting a
        startup still needed by other clients.
        """
        async with self._agui_thread_lock:
            if self._shutting_down:
                raise RuntimeError("API-Server wird beendet.")
            current_id = self._agui_threads.get(thread_id)
            current = self.sessions.get(current_id) if current_id is not None else None
            if current is not None:
                return current
            self._agui_threads.pop(thread_id, None)
            pending = self._agui_thread_inits.get(thread_id)
            if pending is None:
                pending = asyncio.create_task(self._start_agui_thread(thread_id))
                # Retrieve exceptions even if the only requesting client
                # disconnects before the shared startup finishes.
                pending.add_done_callback(
                    lambda completed: completed.exception() if not completed.cancelled() else None
                )
                self._agui_thread_inits[thread_id] = pending
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            if pending.cancelled():
                raise RuntimeError("AG-UI-Thread wurde beim Start geschlossen.") from None
            raise

    async def _remove_session_locked(self, session_id: str) -> bool:
        """Delete a session and every external thread alias under thread lock."""
        async with self._lifecycle_lock:
            session = self.sessions.pop(session_id, None)
            for thread_id, mapped_id in tuple(self._agui_threads.items()):
                if mapped_id == session_id:
                    del self._agui_threads[thread_id]
        if session is None:
            return False
        # A paused approval or frontend handoff may own live MCP resources.
        # Close the worker before another request can recreate this thread.
        await session.close()
        return True

    async def remove_agui_thread(self, thread_id: str) -> bool:
        """Close a standalone thread, including an unfinished startup."""
        async with self._agui_thread_lock:
            pending = self._agui_thread_inits.pop(thread_id, None)
            session_id = self._agui_threads.get(thread_id)
            removed = (
                await self._remove_session_locked(session_id)
                if session_id is not None else False
            )
            if pending is not None:
                pending.cancel()
        if pending is not None:
            await asyncio.gather(pending, return_exceptions=True)
        return removed or pending is not None

    async def remove(self, session_id: str) -> bool:
        async with self._agui_thread_lock:
            return await self._remove_session_locked(session_id)

    async def shutdown(self) -> None:
        async with self._agui_thread_lock:
            async with self._lifecycle_lock:
                self._shutting_down = True
                pending = tuple(self._agui_thread_inits.values())
                self._agui_thread_inits.clear()
                sessions = tuple(self.sessions.values())
                self.sessions.clear()
                self._agui_threads.clear()
            for task in pending:
                task.cancel()
            # Include sessions whose start() is still in progress.
            for session in sessions:
                await session.close()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


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
            session = await service.create_standalone_session()
        except OverflowError:
            return error(429, "Zu viele Sessions.")
        except Exception:
            return error(503, "Agenten-Session konnte nicht gestartet werden.")
        return JSONResponse({"session_id": session.id}, status_code=201)

    async def delete_session(request: Request) -> Response:
        if not await service.remove(request.path_params["session_id"]):
            return error(404, "Session nicht gefunden.")
        return Response(status_code=204)

    async def delete_agui_thread(request: Request) -> Response:
        """Standalone AG-UI management extension (not part of AG-UI 1.0).

        A standalone session created via POST /sessions uses its own ID as
        threadId. Auto-created AG-UI threads retain their client's threadId
        and expose the internal ID via an authenticated response header.
        Browser sessions are not addressable through this endpoint.
        """
        thread_id = request.path_params["thread_id"]
        if not isinstance(thread_id, str) or not 0 < len(thread_id) <= 128:
            return error(400, "Ungültige AG-UI-threadId.")
        if not await service.remove_agui_thread(thread_id):
            return error(404, "AG-UI-Thread nicht gefunden.")
        return Response(status_code=204)

    async def download_artifact(request: Request) -> Response:
        """Authenticated localhost-only artifact access (outside AG-UI transport)."""
        thread_id = _artifact_thread_from_token(request.path_params["thread_id"])
        if thread_id is None:
            return error(404, "Download nicht gefunden.")
        artifact_id = request.path_params["artifact_id"]
        browser = ui is not None and request.headers.get("x-cli-agent-browser") == "1"
        if browser:
            assert ui is not None
            if not ui["registry"].is_owner(
                thread_id, request.headers.get("x-cli-agent-lease")
            ):
                return error(403, "Keine gültige Browser-Bindung.")
            session = service.sessions.get(thread_id)
        else:
            session_id = service._agui_threads.get(thread_id)
            session = service.sessions.get(session_id) if session_id else None
        if session is None or session.agent is None:
            return error(404, "Download nicht gefunden.")
        descriptor = session.published_artifacts.get(artifact_id)
        if descriptor is None:
            return error(404, "Download nicht gefunden.")
        try:
            payload = session.agent._result_artifacts.read_registered(descriptor["path"])
        except (ValueError, RuntimeError, OSError):
            return error(404, "Download nicht mehr verfügbar.")
        from urllib.parse import quote
        filename = descriptor["filename"]
        return Response(
            payload,
            media_type=descriptor["mime_type"],
            headers={
                "Content-Disposition": "attachment; filename*=UTF-8''" + quote(filename),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )

    async def ag_ui_capabilities(_request: Request) -> Response:
        """Optional application-specific discovery using AG-UI's standard shape.

        The AG-UI 1.0 specification defines AgentCapabilities but intentionally
        does not prescribe a discovery URL. Never advertise unsupported
        frontend tools, multimodal input or stream resumption.
        """
        return JSONResponse({
            "identity": {"name": "cli-agent", "type": "cli-agent", "version": "1.0.0"},
            "transport": {
                "streaming": True, "websocket": False,
                "httpBinary": False, "resumable": False,
            },
            "tools": {
                "supported": True, "clientProvided": True,
                "parallelCalls": False,
            },
            "output": {"supportedMimeTypes": ["text/plain"]},
            "state": {
                "snapshots": False, "deltas": False,
                "persistentState": True,
            },
            "humanInTheLoop": {
                "supported": True, "approvals": True,
                "interrupts": True, "approveWithEdits": False,
            },
        })

    async def ag_ui_run(request: Request) -> Response:
        """AG-UI 1.0 HTTP/SSE binding: one POST and one streamed response.

        Optional cli-agent configuration is deliberately kept on separate
        endpoints. AG-UI peers need no cli-agent-specific run or approval API.
        """
        from .ag_ui_protocol import parse_run_input, encode_sse
        accept = request.headers.get("accept", "")
        if "text/event-stream" not in accept and "*/*" not in accept:
            return error(406, "Accept: text/event-stream erforderlich.")
        try:
            async with checked_json(request) as body:
                incoming = parse_run_input(body)
        except ValueError as exc:
            return error(400, str(exc))

        browser = (
            ui is not None and request.headers.get("x-cli-agent-browser") == "1"
        )
        if browser:
            assert ui is not None
            if not ui["registry"].is_owner(
                incoming.thread_id, request.headers.get("x-cli-agent-lease")
            ):
                return error(403, "Keine gültige Browser-Bindung für diesen Thread.")
            session = service.sessions.get(incoming.thread_id)
            if session is None:
                return error(404, "Session nicht gefunden.")
        else:
            try:
                session = await service.resolve_agui_thread(incoming.thread_id)
            except OverflowError:
                return error(429, "Zu viele AG-UI-Threads.")
            except Exception:
                return error(503, "Thread konnte nicht erstellt werden.")

        # Interrupted AG-UI runs have already CLOSED their SSE stream;
        # they must not consume one of the browser's HTTP/1.1 connection
        # reservations. A resume reserves a new streaming slot just like
        # an ordinary run, however, and must not bypass the limit.
        if browser and sum(
            other._busy and other._run is not None and other._run.agui
            and other._run.status != "interrupted"
            for other in service.sessions.values()
        ) >= MAX_CONCURRENT_BROWSER_RUNS:
            return error(429, "Maximal vier parallele Web-UI-Läufe erlaubt.")

        from .ag_ui_frontend_tools import (
            normalize_frontend_tools, checked_frontend_results,
            history_for_model,
        )
        from .evidence_tools import PLAN_TOOL_NAME, ADD_EVIDENCE_TOOL_NAME, UPDATE_EVIDENCE_TOOL_NAME
        from .artifact_publication import PUBLISH_TOOL_NAME
        # Internal agent tools are not routed MCP tools and are deliberately
        # omitted from tool_states_snapshot(). Frontend clients must never
        # shadow them, even before any external tool has been enabled.
        reserved_names = {PLAN_TOOL_NAME, ADD_EVIDENCE_TOOL_NAME, UPDATE_EVIDENCE_TOOL_NAME, PUBLISH_TOOL_NAME}
        reserved_names.update(getattr(session.agent, "_tool_routes", {}))
        if hasattr(session.agent, "tool_states_snapshot"):
            reserved_names.update(
                item["name"] for item in session.agent.tool_states_snapshot()
            )
        try:
            frontend_tools = normalize_frontend_tools(
                incoming.tools, server_names=reserved_names
            )
        except ValueError as exc:
            return error(400, str(exc))

        # Only plain user/assistant messages are submitted to the model's
        # conversation history. AG-UI system/developer messages have no power
        # to modify cli-agent's own system prompt or configured policy.
        if incoming.resume:
            if len(incoming.resume) != 1:
                return error(400, "Alle offenen Interrupts müssen beantwortet werden.")
            try:
                run = session.resume_agui(
                    thread_id=incoming.thread_id,
                    run_id=incoming.run_id,
                    response=incoming.resume[0],
                )
            except (ValueError, RuntimeError) as exc:
                return error(409, str(exc))
        else:
            if session._busy or session._pending_interrupt is not None:
                return error(409, "Thread ist beschäftigt oder wartet auf Resume.")
            pending = session._pending_frontend
            frontend_results = None
            if pending:
                if session._pending_frontend_thread_id != incoming.thread_id:
                    return error(409, "Frontend-Tool-Ergebnis gehört zu anderem Thread.")
                try:
                    frontend_results = checked_frontend_results(incoming.messages, pending)
                except ValueError as exc:
                    return error(409, str(exc))
                # The model resumes a *server-owned* checkpoint. Additional
                # user instructions in the tool-result request would not be
                # visible to that continuation. Reject them explicitly rather
                # than silently claiming they were accepted.
                accepted_users = {
                    message["id"]: message["content"]
                    for message in session.agui_messages
                    if message.get("role") == "user"
                }
                if any(
                    message.get("role") == "user" and (
                        message["id"] not in accepted_users
                        or message["content"] != accepted_users[message["id"]]
                    )
                    for message in incoming.messages
                ):
                    return error(
                        409,
                        "Neue oder geänderte Benutzernachrichten sind während "
                        "eines Frontend-Tool-Resumes nicht erlaubt.",
                    )
            elif not incoming.prompt.strip():
                return error(400, "Eine Benutzer-Nachricht ist erforderlich.")
            if incoming.prompt.strip().casefold() in {"exit", "quit"} and browser and not pending:
                return error(400, "Lokale Shutdown-Befehle gehören nicht zum AG-UI-Run.")
            history = history_for_model(incoming.messages)
            if not pending and history and history[-1] == {
                "role": "user", "content": incoming.prompt
            }:
                history.pop()
            if not pending and hasattr(session.agent, "history"):
                session.agent.history[:] = history
            try:
                run = session.submit(
                    incoming.prompt if not pending else "",
                    require_listener=True, agui=True,
                    thread_id=incoming.thread_id, run_id=incoming.run_id,
                    frontend_results=frontend_results,
                )
            except ValueError as exc:
                return error(409, str(exc))
            except RuntimeError:
                return error(409, "Thread ist beschäftigt.")
            session.agent._agui_frontend_tools = frontend_tools
            session.agui_messages = list(incoming.messages)
            session.agui_state = incoming.state
            session.agui_context = list(incoming.context)
            session.agent._agui_external_context = list(incoming.context)
            if pending:
                session._pending_frontend = None
                session._pending_frontend_thread_id = None
        lease = request.headers.get("x-cli-agent-lease")

        async def stream():
            cursor = 0
            session.subscribe(run)
            try:
                if run.resume_approval is not None:
                    # An active SSE listener must exist BEFORE approving.
                    # The broker validates the original write preview again
                    # against the current workspace (TOCTOU defense).
                    approval_id, decision = run.resume_approval
                    if not session.broker.resolve(approval_id, decision):
                        run.status = "failed"
                        run.emit({"type": "RUN_ERROR", "message": "Freigabe ist abgelaufen."})
                while True:
                    if browser and not ui["registry"].is_owner(
                        session.id, lease, refresh=False
                    ):
                        return
                    for sequence, event in tuple(run.events):
                        if sequence > cursor:
                            cursor = sequence
                            yield encode_sse(event)
                    if run.done and cursor >= run.next_sequence - 1:
                        return
                    run.changed.clear()
                    if run.next_sequence > cursor + 1 or run.done:
                        continue
                    try:
                        await asyncio.wait_for(run.changed.wait(), timeout=5)
                    except asyncio.TimeoutError:
                        # Keep browser intermediaries from silently closing
                        # quiet runs without generating protocol events.
                        yield ": keep-alive\n\n"
            finally:
                session.unsubscribe(run)

        return StreamingResponse(
            stream(), media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
                # Nonstandard management metadata for authenticated external
                # API clients only. Keep AG-UI event payloads interoperable.
                **({} if browser else {"X-Cli-Agent-Session-Id": session.id}),
            },
        )

    async def ui_reconnect_run(request: Request) -> Response:
        """Browser-only replay of an already-active run after tab reload.

        AG-UI 1.0 does not support SSE stream resumption; keep this
        optional, access-controlled browser extension, not a public run API.
        """
        assert ui is not None
        sid = request.path_params["session_id"]
        if not ui["registry"].is_owner(
            sid, request.headers.get("x-cli-agent-lease"),
        ):
            return error(403, "Keine gültige Session-Bindung.")
        session = service.sessions.get(sid)
        run = session.run(request.path_params["run_id"]) if session is not None else None
        if run is None or not run.agui:
            return error(404, "AG-UI-Run nicht gefunden.")
        try:
            last = int(request.headers.get("last-event-id", "0"))
        except ValueError:
            return error(400, "Ungültige Event-ID.")
        if last < 0 or last >= run.next_sequence:
            return error(400, "Ungültige Event-ID.")
        if run.events and last < run.events[0][0] - 1:
            return error(410, "Event-History ist nicht mehr vollständig.")

        browser_lease = request.headers.get("x-cli-agent-lease")

        async def stream():
            cursor = last
            session.subscribe(run)
            try:
                while True:
                    # A request authorized when established must not keep an SSE
                    # subscription after its tab's exclusive lease has expired.
                    if not ui["registry"].is_owner(
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
                    # A fast agent can finish while the SSE generator is
                    # yielding its first event. Do not terminate until EVERY
                    # event emitted after the snapshot has been drained.
                    if run.done and cursor >= run.next_sequence - 1:
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
            "sub_runs": agent.sub_run_snapshots() if callable(getattr(agent, "sub_run_snapshots", None)) else [],
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
        session.agui_messages.clear()
        session._pending_frontend = None
        session._pending_frontend_thread_id = None
        if hasattr(session.agent, "_agui_pending_working_messages"):
            session.agent._agui_pending_working_messages = None
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
                "agui_messages": session.agui_messages,
                "pending_interrupt": session._pending_interrupt,
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
                target = service.sessions[session_id]
                if target._busy and target._pending_interrupt is None:
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

    async def ui_command(request: Request) -> Response:
        """CLI-only commands are intentionally outside the AG-UI contract."""
        assert ui is not None
        from .web_ui import _web_ui_command_prompt
        try:
            async with checked_json(request) as body:
                if (set(body) - {"session_id", "command", "argument"}
                        or not isinstance(body.get("session_id"), str)):
                    return error(400, "Ungültiger Web-UI-Befehl.")
                sid = body["session_id"]
                if not ui["registry"].is_owner(
                    sid, request.headers.get("x-cli-agent-lease")
                ):
                    return error(403, "Session gehört nicht diesem Tab.")
                prompt = _web_ui_command_prompt(
                    body.get("command"), body.get("argument")
                )
            session = service.sessions[sid]
            if session._pending_frontend:
                return error(409, "Offene Frontend-Tool-Aufrufe müssen zuerst beantwortet werden.")
            # Local commands still run on the same MCP-owning worker.
            run = session.submit(prompt, require_listener=False)
            while not run.done:
                run.changed.clear()
                if not run.done:
                    await run.changed.wait()
            if run.status != "completed":
                return error(503, run.error or "Befehl fehlgeschlagen.")
            return JSONResponse({"answer": run.answer or ""})
        except KeyError:
            return error(404, "Session nicht gefunden.")
        except RuntimeError:
            return error(409, "Session ist beschäftigt.")
        except ValueError as exc:
            return error(400, str(exc))

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
            Route("/ag-ui", ag_ui_run, methods=["POST"]),
            Route("/ag-ui/capabilities", ag_ui_capabilities, methods=["GET"]),
            Route("/threads/{thread_id}/artifacts/{artifact_id}", download_artifact, methods=["GET"]),
            Route("/threads/{thread_id:path}", delete_agui_thread, methods=["DELETE"]),
            Route("/sessions", create_session, methods=["POST"]),
            Route("/sessions/{session_id}", delete_session, methods=["DELETE"]),
            Route("/sessions/{session_id}/context", context, methods=["GET"]),
            Route("/sessions/{session_id}/working-context", working_context, methods=["GET"]),
            Route("/sessions/{session_id}/reset", reset_history, methods=["POST"]),
            Route("/sessions/{session_id}/usage", usage, methods=["GET"]),
            Route("/sessions/{session_id}/context", update_context, methods=["PATCH"]),
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
            Route("/ui/reconnect/{session_id}/{run_id}", ui_reconnect_run, methods=["GET"]),
            Route("/ui/render", ui_render, methods=["POST"]),
            Route("/ui/command", ui_command, methods=["POST"]),
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
    from .admin_config import load_admin_config
    from .config import McpServerConfig, default_config_file, load_config
    from .execution import (
        apply_workspace_access_override, apply_data_override,
        apply_named_mcp_overrides, build_preapproval_callback,
        resolve_excluded_paths,
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
