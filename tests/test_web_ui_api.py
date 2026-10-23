"""Regression coverage for the embedded Web UI transported through AgentApiService."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from cli_agent.agent_api import AgentApiService, create_app


class BrowserAgent:
    def __init__(self, session):
        self.session = session
        self.requests = []
        self.reset_count = 0
        self.enabled_tools = {"os__read_file": True}
        self.context_enabled = {"file:0": True, "web:0": True}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def ask(self, prompt):
        self.requests.append(prompt)
        return "Antwort: " + prompt

    def working_messages_snapshot(self):
        return [{"role": "user", "content": "hello"}]

    def tool_states_snapshot(self):
        return [{"name": k, "enabled": v} for k, v in self.enabled_tools.items()]

    def context_states_snapshot(self):
        return [{"id": k, "enabled": v, "kind": k.split(":")[0]}
                for k, v in self.context_enabled.items()]

    def okf_status_snapshot(self):
        return {"configured": False, "enabled": False}

    def set_tool_enabled(self, name, *, enabled):
        self.enabled_tools[name] = enabled

    def set_context_enabled(self, name, *, enabled):
        self.context_enabled[name] = enabled

    def reset_history(self):
        self.reset_count += 1

    def token_usage_text(self):
        return "42 tokens"


def browser_headers():
    return {"Authorization": "Bearer browser-secret", "X-Cli-Agent-Browser": "1"}


@pytest.mark.asyncio
async def test_embedded_ui_auth_routes_and_state_lifecycle():
    agents = []

    def factory(session):
        agent = BrowserAgent(session)
        agents.append(agent)
        return agent

    service = AgentApiService(factory)
    session = await service.create()
    stopped = []
    ui = {
        "token": "browser-secret", "session_id": session.id,
        "origin": "http://127.0.0.1:9191",
        "info": {"model": "test", "workspace": "/workspace", "mcp_servers": []},
        "shutdown": lambda: stopped.append(True),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service, ui=ui)),
        base_url="http://127.0.0.1:9191",
    ) as client:
        assert (await client.get("/")).status_code == 200
        assert (await client.get("/assets/app.js")).status_code == 200
        assert "WebSocket(wsUrl)" not in (await client.get("/assets/app.js")).text
        assert (await client.get("/ui/bootstrap")).status_code == 401
        headers = browser_headers()
        assert (await client.get("/ui/bootstrap", headers=headers)).json()["session_id"] == session.id
        assert (await client.get("/ui/bootstrap", headers={**headers, "Host": "evil.example"})).status_code == 403
        assert (await client.get("/ui/bootstrap", headers={**headers, "X-Forwarded-Host": "evil.example"})).status_code == 403
        assert (await client.get("/health", headers=headers)).status_code == 401
        assert (await client.post("/sessions", json={}, headers={**headers, "Origin": ui["origin"]})).status_code == 403
        assert (await client.get("/sessions/not-ours/working-context", headers=headers)).status_code == 401

        uri = f"/sessions/{session.id}"
        working = (await client.get(uri + "/working-context", headers=headers)).json()
        assert working["messages"][0]["content"] == "hello"
        assert {c["id"] for c in working["contexts"]} == {"file:0", "web:0"}
        assert (await client.get(uri + "/usage", headers=headers)).json()["text"] == "42 tokens"
        assert (await client.patch(uri + "/context",
            json={"contexts": {"file:0": False, "web:0": False},
                  "tools": {"os__read_file": False}},
            headers={**headers, "Origin": ui["origin"]})).status_code == 200
        assert agents[0].context_enabled == {"file:0": False, "web:0": False}
        assert agents[0].enabled_tools["os__read_file"] is False
        assert (await client.post(uri + "/reset", json={},
            headers={**headers, "Origin": "http://evil.example"})).status_code == 403
        assert (await client.post(uri + "/reset", json={},
            headers={**headers, "Origin": ui["origin"]})).status_code == 200
        assert agents[0].reset_count == 1

        rid = (await client.post(uri + "/runs", json={"prompt": "hi"},
            headers={**headers, "Origin": ui["origin"]})).json()["run_id"]
        for _ in range(100):
            run = (await client.get(uri + "/runs/" + rid, headers=headers)).json()
            if run["status"] == "completed":
                break
            await asyncio.sleep(.005)
        assert run["answer"] == "Antwort: hi"

        assert (await client.post("/ui/render", json={"text": "**safe**"},
            headers={**headers, "Origin": ui["origin"]})).status_code == 200
        assert (await client.post("/ui/shutdown", json={},
            headers={**headers, "Origin": ui["origin"]})).status_code == 200
        assert stopped == [True]
    await service.shutdown()


@pytest.mark.asyncio
async def test_cli_initial_context_and_output_are_run_on_session_worker():
    class Target:
        def __init__(self):
            self.writes = []

        def write_text(self, text):
            self.writes.append(text)

    created = []
    def factory(session):
        agent = BrowserAgent(session)
        created.append(agent)
        return agent

    target = Target()
    service = AgentApiService(
        factory, output_target=target,
        initial_commands=("add_web_context https://example.org",),
    )
    session = await service.create()
    assert session.initial_messages == ["Antwort: add_web_context https://example.org"]
    run = session.submit("ordinary")
    for _ in range(100):
        if run.done:
            break
        await asyncio.sleep(.005)
    assert run.answer == "Antwort: ordinary"
    assert target.writes == ["Antwort: ordinary"]
    assert created[0].requests == ["add_web_context https://example.org", "ordinary"]
    await service.shutdown()
