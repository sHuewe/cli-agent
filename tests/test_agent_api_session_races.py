"""Deterministic race tests for the API session and AG-UI lifecycle."""
from __future__ import annotations

import asyncio

import pytest

from cli_agent.interfaces.agent_api import AgentApiService, AgentSession


class ControlledAgent:
    def __init__(self, session, started, release):
        self.session = session
        self.started = started
        self.release = release
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        self.closed = True

    async def ask(self, prompt):
        if prompt == "hold":
            self.started.set()
            await self.release.wait()
        return "ok:" + prompt


async def _until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancel_inflight_run_keeps_worker_and_next_run_usable():
    started, release = asyncio.Event(), asyncio.Event()
    agents = []
    def factory(session):
        agent = ControlledAgent(session, started, release)
        agents.append(agent)
        return agent
    service = AgentApiService(factory)
    session = await service.create()
    try:
        current = session.submit("hold")
        await asyncio.wait_for(started.wait(), timeout=5)
        with pytest.raises(RuntimeError, match="Kein passender"):
            session.cancel_run(thread_id=session.id, run_id="stale-run")
        assert not current.cancel_requested
        session.cancel_run(thread_id=session.id, run_id=current.id)
        session.cancel_run(thread_id=session.id, run_id=current.id)
        await _until(lambda: current.done and not session._busy)
        assert current.status == "cancelled"
        assert not agents[0].closed
        next_run = session.submit("next")
        await _until(lambda: next_run.done)
        assert next_run.answer == "ok:next"
        assert next_run.status == "completed"
        assert current.status == "cancelled"
    finally:
        release.set()
        await service.shutdown()
    assert agents[0].closed
    assert not service.sessions


@pytest.mark.asyncio
async def test_shutdown_during_inflight_run_closes_worker_and_rejects_new_sessions():
    started, release = asyncio.Event(), asyncio.Event()
    agents = []
    def factory(session):
        agent = ControlledAgent(session, started, release)
        agents.append(agent)
        return agent
    service = AgentApiService(factory)
    session = await service.create()
    active = session.submit("hold")
    await asyncio.wait_for(started.wait(), timeout=5)
    try:
        await asyncio.wait_for(service.shutdown(), timeout=5)
        assert active.done
        assert active.status == "cancelled"
        assert agents[0].closed
        assert not service.sessions
        with pytest.raises(RuntimeError, match="beendet"):
            await service.create()
    finally:
        release.set()
        await service.shutdown()


@pytest.mark.asyncio
async def test_concurrent_same_thread_resolution_survives_waiter_cancellation(monkeypatch):
    original_start = AgentSession.start
    entering = asyncio.Event()
    release = asyncio.Event()
    created = []

    async def gated_start(self):
        entering.set()
        await release.wait()
        return await original_start(self)

    def factory(session):
        agent = ControlledAgent(session, asyncio.Event(), asyncio.Event())
        created.append(agent)
        return agent

    monkeypatch.setattr(AgentSession, "start", gated_start)
    service = AgentApiService(factory)
    first = asyncio.create_task(service.resolve_agui_thread("shared-thread"))
    try:
        await asyncio.wait_for(entering.wait(), timeout=5)
        second = asyncio.create_task(service.resolve_agui_thread("shared-thread"))
        await _until(lambda: len(service._agui_thread_inits) == 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        resolved = await asyncio.wait_for(second, timeout=5)
        assert service._agui_threads["shared-thread"] == resolved.id
        assert len(service.sessions) == 1
        assert len(created) == 1
        assert await service.resolve_agui_thread("shared-thread") is resolved
        assert not service._agui_thread_inits
    finally:
        release.set()
        await service.shutdown()
    assert created[0].closed
