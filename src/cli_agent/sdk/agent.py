"""Persistent notebook-friendly agent; owns one task for MCP async lifecycle."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cli_agent.platform.execution import OneShotRunOptions, prepare_agent
from cli_agent.settings.config import AppConfig
from cli_agent.settings.admin_config import AdminConfig


@dataclass(frozen=True)
class AgentResult:
    answer: str


class Agent:
    def __init__(
        self, *, project_config: AppConfig, admin_config: AdminConfig,
        project_path: Path | None, run_options: dict[str, Any],
    ) -> None:
        self._config = project_config
        self._policy = admin_config
        self._config_path = project_path
        self._options = dict(run_options)
        self._worker_task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[tuple[str, asyncio.Future[AgentResult]] | None] | None = None
        self._ready: asyncio.Future[None] | None = None
        self._closed = False

    async def start(self) -> None:
        if self._closed:
            raise RuntimeError("Agent wurde bereits geschlossen.")
        if self._worker_task is not None:
            raise RuntimeError("Agent läuft bereits.")
        loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._ready = loop.create_future()
        self._worker_task = loop.create_task(self._worker())
        try:
            await self._ready
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
                self._ready.set_result(None)
                while True:
                    item = await self._queue.get()
                    if item is None:
                        return
                    prompt, future = item
                    if future.cancelled():
                        continue
                    try:
                        answer = await runtime.ask(prompt)
                    except Exception as exc:
                        if not future.done():
                            future.set_exception(exc)
                    else:
                        if not future.done():
                            future.set_result(AgentResult(answer=answer))
        except asyncio.CancelledError:
            if not self._ready.done():
                self._ready.set_exception(RuntimeError("Agentenstart abgebrochen."))
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
            raise RuntimeError("Agenten-Worker ist beendet.") from self._worker_task.exception()
        future: asyncio.Future[AgentResult] = asyncio.get_running_loop().create_future()
        await self._queue.put((prompt, future))
        return await future

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._worker_task is None:
            return
        assert self._queue is not None
        await self._queue.put(None)
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass
        finally:
            self._worker_task = None

    async def __aenter__(self) -> Agent:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.close()
