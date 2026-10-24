"""Embedded browser session isolation, one-use tickets and server-side leases."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from cli_agent.agent_api import AgentApiService, create_app
from cli_agent.web_ui_sessions import (
    BrowserSessionRegistry, LEASE_TTL_SECONDS, SessionOccupiedError,
)


class _Agent:
    def __init__(self, session):
        self.session = session
        self.history = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def ask(self, prompt):
        self.history.append(prompt)
        return "reply " + prompt

    def working_messages_snapshot(self):
        return [{"role": "user", "content": x} for x in self.history]

    def tool_states_snapshot(self):
        return []

    def context_states_snapshot(self):
        return []

    def okf_status_snapshot(self):
        return {"configured": False, "enabled": False}

    def token_usage_text(self):
        return "0 tokens"

    def reset_history(self):
        self.history.clear()


def _headers(lease=None, *, origin=None):
    result = {
        "Authorization": "Bearer browser-secret",
        "X-Cli-Agent-Browser": "1",
    }
    if lease is not None:
        result["X-Cli-Agent-Lease"] = lease
    if origin is not None:
        result["Origin"] = origin
    return result


@pytest.mark.asyncio
async def test_two_independent_sessions_reopen_without_shared_access():
    agents = []
    def factory(session):
        agent = _Agent(session)
        agents.append(agent)
        return agent

    service = AgentApiService(factory)
    initial = await service.create()
    registry = BrowserSessionRegistry(service, initial.id)
    origin = "http://127.0.0.1:9191"
    ui = {
        "token": "browser-secret",
        "origin": origin,
        "session_id": initial.id,
        "registry": registry,
        "info": {"model": "test", "workspace": ".", "mcp_servers": []},
        "shutdown": lambda: None,
    }
    app = create_app(service, ui=ui)

    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=origin
        ) as client:
            a = (await client.post(
                "/ui/bootstrap", headers=_headers(origin=origin), json={}
            )).json()
            assert a["session_id"] == initial.id
            lease_a = a["lease"]
            assert (await client.post(
                "/ui/bootstrap", headers=_headers(origin=origin), json={}
            )).status_code == 409

            created = await client.post(
                "/ui/sessions", headers=_headers(origin=origin), json={}
            )
            assert created.status_code == 201
            b_id = created.json()["session_id"]
            assert b_id != initial.id
            ticket = created.json()["ticket"]
            b = await client.post(
                "/ui/bootstrap", headers=_headers(origin=origin), json={"ticket": ticket}
            )
            assert b.status_code == 200
            lease_b = b.json()["lease"]
            assert (await client.post(
                "/ui/bootstrap", headers=_headers(origin=origin),
                json={"ticket": ticket},
            )).status_code == 400

            # A browser credential alone is insufficient for session APIs.
            assert (await client.get(
                f"/sessions/{initial.id}/working-context", headers=_headers()
            )).status_code == 403
            assert (await client.get(
                f"/sessions/{initial.id}/working-context", headers=_headers(lease_b)
            )).status_code == 403
            assert (await client.get(
                f"/sessions/{b_id}/working-context", headers=_headers(lease_a)
            )).status_code == 403

            run_a = await client.post(
                f"/sessions/{initial.id}/runs",
                headers=_headers(lease_a, origin=origin), json={"prompt": "Alpha"},
            )
            run_b = await client.post(
                f"/sessions/{b_id}/runs",
                headers=_headers(lease_b, origin=origin), json={"prompt": "Beta"},
            )
            assert run_a.status_code == 202
            assert run_b.status_code == 202
            for _ in range(100):
                if agents[0].history and agents[1].history:
                    break
                await asyncio.sleep(0.005)
            assert agents[0].history == ["Alpha"]
            assert agents[1].history == ["Beta"]

            listed = (await client.get("/ui/sessions", headers=_headers())).json()["sessions"]
            assert len(listed) == 2
            assert all(item["occupied"] for item in listed)
            assert (await client.post(
                f"/ui/sessions/{initial.id}/ticket",
                headers=_headers(origin=origin), json={},
            )).status_code == 409
            assert (await client.delete(
                f"/ui/sessions/{b_id}", headers=_headers(origin=origin)
            )).status_code == 409

            # Close a tab without deleting its agent session.
            released = await client.post(
                "/ui/release", headers=_headers(lease_a, origin=origin),
                json={"session_id": initial.id},
            )
            assert released.status_code == 204
            assert (await client.post(
                "/ui/heartbeat", headers=_headers(lease_a, origin=origin),
                json={"session_id": initial.id},
            )).status_code == 409
            invitation = await client.post(
                f"/ui/sessions/{initial.id}/ticket",
                headers=_headers(origin=origin), json={},
            )
            assert invitation.status_code == 200
            reopened = await client.post(
                "/ui/bootstrap", headers=_headers(origin=origin),
                json={"ticket": invitation.json()["ticket"]},
            )
            assert reopened.status_code == 200
            assert reopened.json()["session_id"] == initial.id
            assert reopened.json()["lease"] != lease_a

            # The browser cannot delete a session belonging to another active tab.
            assert (await client.delete(
                f"/ui/sessions/{b_id}", headers=_headers(origin=origin)
            )).status_code == 409
            assert (await client.post(
                "/ui/release", headers=_headers(lease_b, origin=origin),
                json={"session_id": b_id},
            )).status_code == 204
            assert (await client.delete(
                f"/ui/sessions/{b_id}", headers=_headers(origin=origin)
            )).status_code == 204
            assert b_id not in service.sessions
            assert initial.id in service.sessions
            assert (await client.delete(
                f"/ui/sessions/{initial.id}",
                headers=_headers(reopened.json()["lease"], origin=origin),
            )).status_code == 204
            assert initial.id not in service.sessions
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_registry_lease_expiration_and_single_use_ticket():
    service = AgentApiService(lambda session: _Agent(session))
    initial = await service.create()
    registry = BrowserSessionRegistry(service, initial.id)
    try:
        ticket = registry.invitation(initial.id)
        sid, token = registry.redeem(ticket)
        assert sid == initial.id
        with pytest.raises(ValueError):
            registry.redeem(ticket)
        with pytest.raises(SessionOccupiedError):
            registry.claim(initial.id)
        assert registry.is_owner(initial.id, token)
        registry._leases[initial.id].last_seen -= LEASE_TTL_SECONDS + 1
        assert not registry.is_owner(initial.id, token)
        new_token = registry.claim(initial.id)
        assert new_token != token
        assert not registry.release(initial.id, token)
        assert registry.release(initial.id, new_token)
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_initial_contexts_do_not_share_mcp_startup_deadline():
    """After MCP startup, slow initial contexts must not consume its 30s budget."""
    gate = asyncio.Event()
    started = asyncio.Event()

    class SlowContextAgent(_Agent):
        async def ask(self, prompt):
            if prompt.startswith("add_web_context "):
                started.set()
                await gate.wait()
            return await super().ask(prompt)

    service = AgentApiService(
        lambda session: SlowContextAgent(session),
        initial_commands=("add_web_context https://example.com/first",
                          "add_web_context https://example.com/second"),
    )
    creation = asyncio.create_task(service.create())
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        assert len(service.sessions) == 1
        pending_session = next(iter(service.sessions.values()))
        assert pending_session._startup_ready.done()
        assert not pending_session._ready.done()
        assert not creation.done()
        gate.set()
        session = await asyncio.wait_for(creation, timeout=2)
        assert session.initial_messages == [
            "reply add_web_context https://example.com/first",
            "reply add_web_context https://example.com/second",
        ]
    finally:
        gate.set()
        await service.shutdown()


@pytest.mark.asyncio
async def test_replay_omits_resolved_or_denied_tool_approvals():
    """A reload must not offer a stale approval that now returns HTTP 409."""
    import json
    service = AgentApiService(lambda session: _Agent(session))
    session = await service.create()
    try:
        run = session.submit("example")
        for _ in range(100):
            if run.done:
                break
            await asyncio.sleep(0.005)
        assert run.done
        # Reproduce a historical approval request in an otherwise completed
        # run. It is deliberately not pending in the broker.
        old = {"type": "CUSTOM", "name": "approval_required",
               "value": {"id": "already-denied", "tool": "os__write_file"}}
        run.events = (run.events[:1] + [(2, old)] +
                      [(seq + 1, event) for seq, event in run.events[1:]])
        run.next_sequence += 1
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)),
            base_url="http://127.0.0.1:9191",
        ) as client:
            events = await client.get(
                f"/sessions/{session.id}/runs/{run.id}/events",
                headers={"Authorization": f"Bearer {service.token}"},
            )
            assert events.status_code == 200
            payloads = [
                json.loads(line[6:]) for line in events.text.splitlines()
                if line.startswith("data: ")
            ]
            assert not any(item.get("name") == "approval_required"
                           for item in payloads)
            assert any(item["type"] == "RUN_FINISHED" for item in payloads)
    finally:
        await service.shutdown()
