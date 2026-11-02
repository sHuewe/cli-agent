"""Persistent notebook-friendly agent; owns one task for MCP async lifecycle."""
from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from time import monotonic
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli_agent.platform.execution import OneShotRunOptions, prepare_agent
from cli_agent.settings.config import AppConfig
from cli_agent.settings.admin_config import AdminConfig
from .events import AgentEvent, ToolCallRecord


@dataclass(frozen=True)
class AgentResult:
    answer: str
    tool_calls: tuple[ToolCallRecord, ...] = ()
    events: tuple[AgentEvent, ...] = ()


class Agent:
    def __init__(
        self, *, project_config: AppConfig, admin_config: AdminConfig,
        project_path: Path | None, run_options: dict[str, Any],
        event_callback: Any = None,
    ) -> None:
        self._config = project_config
        self._policy = admin_config
        self._config_path = project_path
        self._options = dict(run_options)
        self._event_callback = event_callback
        self._event_queue: asyncio.Queue[AgentEvent | None] | None = None
        self._event_worker: asyncio.Task[None] | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[tuple[str, asyncio.Future[AgentResult]] | None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._closed = False
        # Identifies the only request currently executing on the MCP-owning
        # worker. Cancelling a queued request must never cancel another run.
        self._active_future: asyncio.Future[AgentResult] | None = None

    async def _consume_events(self) -> None:
        assert self._event_queue is not None
        while True:
            event = await self._event_queue.get()
            if event is None:
                return
            try:
                value = self._event_callback(event)
                if inspect.isawaitable(value):
                    await value
            except Exception:
                logging.getLogger(__name__).exception("SDK event callback failed")

    async def _run_with_events(self, runtime: Any, prompt: str) -> AgentResult:
        run_id = uuid.uuid4().hex
        events: list[AgentEvent] = []
        starts: dict[str, float] = {}
        open_tools: dict[str, str] = {}
        calls: list[ToolCallRecord] = []

        async def emit(kind: str, payload: dict[str, Any]) -> None:
            # Never expose raw MCP results, tool arguments or model payloads.
            call_id = str(payload.get("call_id") or "") or None
            duration_ms = None
            if call_id is not None:
                if kind in {"tool_started", "model_started", "sub_run_started"}:
                    starts[call_id] = monotonic()
                if kind in {"tool_finished", "model_finished", "sub_run_finished"}:
                    started = starts.pop(call_id, None)
                    if started is not None:
                        duration_ms = (monotonic() - started) * 1000
            event = AgentEvent(
                type=kind, run_id=run_id, tool=payload.get("tool"),
                call_id=call_id, status=payload.get("status"),
                phase=payload.get("phase"), duration_ms=duration_ms,
            )
            if len(events) < 8192:
                events.append(event)
            if kind == "tool_started" and call_id:
                open_tools[call_id] = event.tool or ""
            elif kind == "tool_finished" and call_id:
                calls.append(ToolCallRecord(
                    name=open_tools.pop(call_id, event.tool or ""), call_id=call_id,
                    status=event.status or "finished", duration_ms=duration_ms,
                ))
            if self._event_queue is not None and self._event_callback is not None:
                try:
                    self._event_queue.put_nowait(event)
                except asyncio.QueueFull:
                    pass

        old_callback = getattr(runtime, "event_callback", None)
        runtime.event_callback = emit
        runtime._sdk_event_reporting = True
        await emit("run_started", {})
        status = "success"
        try:
            answer = await runtime.ask(prompt)
        except BaseException:
            status = "error"
            raise
        finally:
            await emit("run_finished", {"status": status})
            runtime.event_callback = old_callback
            runtime._sdk_event_reporting = False
        for call_id, name in open_tools.items():
            calls.append(ToolCallRecord(name=name, call_id=call_id, status="interrupted"))
        return AgentResult(answer=answer, tool_calls=tuple(calls), events=tuple(events))

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Agent wurde bereits geschlossen.")
        if self._worker_task is not None:
            raise RuntimeError("Agent läuft bereits.")
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._ready = loop.create_future()
        if self._event_callback is not None:
            self._event_queue = asyncio.Queue(maxsize=1024)
            self._event_worker = loop.create_task(self._consume_events())
        self._worker_task = loop.create_task(self._worker())
        try:
            await self._ready
        except asyncio.CancelledError:
            # A cancelled notebook cell / wait_for timeout must interrupt
            # initialization itself, not just the Future awaiting it.
            # The MCP transport stack is cleaned up on its owning worker task.
            if self._worker_task is not None and not self._worker_task.done():
                self._worker_task.cancel()
            await self.close()
            raise
        except BaseException:
            await self.close()
            raise

    async def _worker(self) -> None:
        assert self._ready is not None and self._queue is not None
        try:
            opts = OneShotRunOptions(
                workspace=Path(self._options.get("workspace", Path.cwd())),
                prompt="SDK initialization",
                config_file=self._config_path,
                **{k: v for k, v in self._options.items() if k != "workspace"},
            )
            prepared = prepare_agent(
                opts, project_config=self._config, admin_config=self._policy,
            )
            async with prepared.agent as runtime:
                for url in opts.add_web_context:
                    await runtime.ask(f"add_web_context {url}")
                if not self._ready.done():
                    self._ready.set_result(None)
                while True:
                    item = await self._queue.get()
                    if item is None:
                        return
                    prompt, future = item
                    if future.cancelled() or self._closed:
                        if not future.done():
                            future.set_exception(RuntimeError("Agent beendet."))
                        continue
                    self._active_future = future
                    try:
                        # The worker owns the MCP AnyIO scopes, so the running
                        # ask must execute and be cancelled on this same task.
                        result = await self._run_with_events(runtime, prompt)
                    except asyncio.CancelledError:
                        if self._closed:
                            if not future.done():
                                future.cancel()
                            raise
                        if not future.cancelled():
                            # Unexpected worker cancellation: do not pretend
                            # that this was a user-cancelled request.
                            raise
                        # Match the API worker: keep MCP sessions alive after
                        # interrupting only this request.
                        worker = asyncio.current_task()
                        if worker is not None:
                            while worker.cancelling():
                                worker.uncancel()
                    except Exception as exc:
                        if not future.done():
                            future.set_exception(exc)
                    else:
                        if not future.done():
                            future.set_result(result)
                    finally:
                        self._active_future = None
        except asyncio.CancelledError:
            if not self._ready.done():
                # This is an intentional startup abort, not a startup error.
                # Cancel instead of setting an unobserved exception.
                self._ready.cancel()
            raise
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            raise
        finally:
            if self._queue is not None:
                while not self._queue.empty():
                    item = self._queue.get_nowait()
                    if item is not None and not item[1].done():
                        item[1].set_exception(RuntimeError("Agent beendet."))

    async def ask(self, prompt: str) -> AgentResult:
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("Prompt darf nicht leer sein.")
        if self._closed or self._worker_task is None or self._queue is None:
            raise RuntimeError("Agent ist nicht gestartet.")
        if self._worker_task.done():
            raise RuntimeError("Agenten-Worker ist beendet.")
        future: asyncio.Future[AgentResult] = asyncio.get_running_loop().create_future()
        await self._queue.put((prompt, future))
        try:
            return await future
        except asyncio.CancelledError:
            # asyncio.wait_for() and cancelled notebook cells cancel their
            # waiting Future, not the worker that runs the LLM/tool loop.
            # Cancel only when this request is currently active.
            future.cancel()
            if (
                self._active_future is future
                and self._worker_task is not None
                and not self._worker_task.done()
            ):
                self._worker_task.cancel()
            raise

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._worker_task is None:
            return
        assert self._queue is not None
        starting = self._ready is not None and (
            not self._ready.done() or self._ready.cancelled()
        )
        if starting and self._ready is not None and not self._ready.done():
            # Cancelling a task before its first execution bypasses _worker's
            # try/finally. Explicitly settle the startup waiter here rather
            # than relying on the worker to report its own cancellation.
            self._ready.cancel()
        if not self._worker_task.done():
            active = self._active_future
            if starting or active is not None:
                # Startup and active model/tool calls must be interrupted.
                # The worker still closes its own MCP/AnyIO scopes.
                if active is not None:
                    active.cancel()
                self._worker_task.cancel()
            else:
                await self._queue.put(None)
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        finally:
            self._worker_task = None
            if self._event_worker is not None:
                self._event_worker.cancel()
                try:
                    await self._event_worker
                except asyncio.CancelledError:
                    pass
                self._event_worker = None
                self._event_queue = None

    async def __aenter__(self) -> Agent:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
