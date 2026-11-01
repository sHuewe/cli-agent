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

from cli_agent.interfaces.web_ui import WebUiApprovalBroker

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
    cancel_requested: bool = False
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
        self._closing = False
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
                    if run.cancel_requested:
                        self._finish_cancelled(run)
                        self._busy = False
                        continue
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
                            from cli_agent.context.file_context import is_local_agent_command
                            if not is_local_agent_command(run.prompt):
                                try:
                                    self.output_target.write_text(answer)
                                except Exception:
                                    finished.emit({"type": "CUSTOM", "name": "output_error",
                                              "value": {"message": "Ausgabedatei konnte nicht geschrieben werden."}})
                        finished.status = "completed"
                        finished.emit({"type": "RUN_FINISHED", "threadId": finished.thread_id, "runId": finished.id,
                                       **({"outcome": {"type": "success"}} if finished.agui else {})})
                    except asyncio.CancelledError:
                        if self._closing:
                            raise
                        if not run.cancel_requested and not (
                            self._run is not None and self._run.cancel_requested
                        ):
                            raise
                        # The MCP AsyncExitStack belongs to this long-lived task.
                        # Handle a run cancellation HERE rather than terminating
                        # the worker (which would close every MCP connection).
                        worker = asyncio.current_task()
                        if worker is not None:
                            while worker.cancelling():
                                worker.uncancel()
                        active = self._run if run.agui else run
                        self._finish_cancelled(active if active is not None else run)
                        self._pending_frontend = None
                        self._pending_frontend_thread_id = None
                    except Exception as exc:
                        from cli_agent.interfaces.ag_ui_frontend_tools import FrontendToolHandoff
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

    @staticmethod
    def _finish_cancelled(run: AgentRun) -> None:
        previous = run.status
        run.status = "cancelled"
        if previous == "interrupted":
            # The approval segment has already emitted RUN_FINISHED and its
            # SSE consumer has gone away. Do not emit a second terminal event.
            run.changed.set()
        elif run.agui:
            run.emit({
                "type": "RUN_FINISHED", "threadId": run.thread_id,
                "runId": run.id, "outcome": {"type": "cancelled"},
            })
        else:
            run.emit({"type": "RUN_ERROR", "message": "Agentenlauf abgebrochen."})

    def cancel_run(self, *, thread_id: str, run_id: str) -> None:
        """Cancel only the currently active run, never the session itself.

        A queued run is skipped by the worker. An executing run is interrupted
        at the next await; the worker catches CancelledError inside the agent
        context so that the session and its MCP transports remain available.
        """
        run = self._run
        if (
            self._closed or not self._busy or run is None
            or run.id != run_id or run.thread_id != thread_id
            or run.status not in {"queued", "running", "interrupted"}
        ):
            raise RuntimeError("Kein passender aktiver Agentenlauf.")
        if run.cancel_requested:
            return
        run.cancel_requested = True
        if run.status != "queued" and self._task is not None:
            self._task.cancel()

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
        self._closing = True
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



# Keep API-facing constants, types and helpers on a single lifecycle owner.
__all__ = [name for name in globals() if not name.startswith("__")]
