"""Local API regression tests; no external model or MCP service needed."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from cli_agent.agent_api import (
    API_HOST, MAX_SESSIONS, AgentApiService, build_parser, create_app,
)


class FakeAgent:
    def __init__(self, session):
        self.session = session
        self.history = []
        self.closed = False
        self.enabled = {"read": True}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True

    async def ask(self, prompt):
        self.history.append(prompt)
        if prompt == "needs approval":
            gate = getattr(self, "approval_gate", None)
            if gate is not None:
                await gate.wait()
            decision = await self.session.approve("remote__write", {"path": "file.txt"})
            return f"approved={decision}"
        if prompt == "fail":
            raise RuntimeError("secret not for stream")
        if prompt == "slow":
            await asyncio.sleep(10)
        return f"reply {prompt}"

    def tool_states_snapshot(self):
        return [{"name": "read", "enabled": self.enabled["read"]}]

    def context_states_snapshot(self):
        return [{"id": "file-a", "enabled": True}]

    def okf_status_snapshot(self):
        return {"configured": False, "enabled": False}

    def set_tool_enabled(self, name, *, enabled):
        self.enabled[name] = enabled

    def set_context_enabled(self, name, *, enabled):
        assert name == "file-a"

    def set_okf_enabled(self, *, enabled):
        raise AssertionError("OKF disabled")


def make_test_service():
    agents = []
    def factory(session):
        agent = FakeAgent(session)
        agents.append(agent)
        return agent
    return AgentApiService(factory), agents


def headers(service):
    return {"Authorization": f"Bearer {service.token}"}


async def client_for(service):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(service)),
        base_url="http://127.0.0.1:8765",
    )


@pytest.mark.asyncio
async def test_auth_origin_and_host_guards():
    service, _agents = make_test_service()
    async with await client_for(service) as client:
        assert (await client.get("/health")).status_code == 401
        assert (await client.get("/health", headers={"Authorization": "Bearer wrong"})).status_code == 401
        assert (await client.get("/health", headers={**headers(service), "Origin": "http://malicious"})).status_code == 403
        assert (await client.get("/health", headers={**headers(service), "Host": "evil.com"})).status_code == 403
        assert (await client.get("/health", headers={**headers(service), "X-Forwarded-Host": "evil.com"})).status_code == 403
        assert (await client.get("/health", headers=headers(service))).json() == {"status": "ok"}
    await service.shutdown()


def test_cli_no_host_override():
    assert API_HOST == "127.0.0.1"
    parser = build_parser()
    assert parser.parse_args(["--with-os-read"]).os_access == "read"
    with pytest.raises(SystemExit):
        parser.parse_args(["--host", "0.0.0.0"])


@pytest.mark.asyncio
async def test_context_is_bounded_to_existing_tools():
    service, agents = make_test_service()
    async with await client_for(service) as client:
        sid = (await client.post("/sessions", headers=headers(service))).json()["session_id"]
        url = f"/sessions/{sid}/context"
        assert (await client.patch(url, json={"tools": {"os__write_file": True}}, headers=headers(service))).status_code == 400
        assert (await client.patch(url, json={"okf": True}, headers=headers(service))).status_code == 400
        assert (await client.patch(url, json={"tools": {"read": "true"}}, headers=headers(service))).status_code == 400
        assert (await client.patch(url, json={"tools": {"read": False}}, headers=headers(service))).status_code == 200
        assert agents[0].enabled["read"] is False
        assert (await client.get(url, headers=headers(service))).json()["tools"][0]["enabled"] is False
    await service.shutdown()


@pytest.mark.asyncio
async def test_session_limit_and_shutdown():
    service, agents = make_test_service()
    sessions = [await service.create() for _ in range(MAX_SESSIONS)]
    with pytest.raises(OverflowError):
        await service.create()
    await service.shutdown()
    assert all(agent.closed for agent in agents)
    assert not service.sessions


@pytest.mark.asyncio
async def test_factory_configuration_uses_server_owned_capabilities(tmp_path, monkeypatch):
    from cli_agent import admin_config, config, execution, file_context, logging_setup, model_factory
    from cli_agent.agent_api import build_service
    from dataclasses import dataclass

    @dataclass
    class FakeModel:
        model: str = "default"

    @dataclass
    class FakeConfig:
        model: FakeModel
        logging: object
        mcp_servers: tuple = ()
        dump_llm_context: bool = False
        okf: object = None
        knowledge_vector: object = None

    fake_config = FakeConfig(model=FakeModel(), logging=SimpleNamespace(), dump_llm_context=True)
    fake_admin = SimpleNamespace(
        network=object(), model_credentials=(), web=SimpleNamespace(providers=()),
        mcp=object(),
    )
    captured = []
    monkeypatch.setattr(config, "load_config", lambda _: fake_config)
    monkeypatch.setattr(admin_config, "load_admin_config", lambda: fake_admin)
    monkeypatch.setattr(logging_setup, "configure_logging", lambda _: None)
    monkeypatch.setattr(model_factory, "create_model_client", lambda *a, **kw: "model-client")
    monkeypatch.setattr(execution, "resolve_excluded_paths", lambda *a: ())
    monkeypatch.setattr(execution, "apply_workspace_access_override", lambda cfg, **kw: cfg)
    monkeypatch.setattr(execution, "apply_data_override", lambda cfg, **kw: cfg)
    monkeypatch.setattr(execution, "apply_named_mcp_overrides", lambda cfg, **kw: cfg)

    class FakeConstructedAgent:
        def __init__(self, *args, **kwargs):
            captured.append((args, kwargs))
            self.session = None

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(file_context, "ContextFileCliAgent", FakeConstructedAgent)

    args = build_parser().parse_args([
        "--workspace", str(tmp_path), "--model", "override", "--with-os-read"
    ])
    service = build_service(args)
    session = await service.create()
    assert captured[0][0][0] == tmp_path.resolve()
    assert captured[0][0][1] == "model-client"
    assert captured[0][1]["workspace_access"] == "read"
    assert captured[0][1]["dump_llm_context"] is False
    assert fake_config.dump_llm_context is True  # API must not mutate CLI configuration
    assert captured[0][1]["approval_callback"] == session.approve
    assert fake_config.model.model == "default"
    await service.shutdown()


def test_api_start_rejects_invalid_static_start_options(tmp_path):
    from cli_agent.agent_api import build_service
    invalid_port = build_parser().parse_args(["--port", "0"])
    with pytest.raises(ValueError, match="Port"):
        build_service(invalid_port)
    absent = build_parser().parse_args(["--workspace", str(tmp_path / "missing")])
    with pytest.raises(ValueError, match="Workspace"):
        build_service(absent)
    excluded = build_parser().parse_args([
        "--workspace", str(tmp_path), "--exclude-path", "secret"
    ])
    with pytest.raises(ValueError, match="OS-Zugriff"):
        build_service(excluded)


@pytest.mark.asyncio
async def test_concurrent_creation_reserves_capacity_before_startup():
    from cli_agent.agent_api import AgentSession
    ready = asyncio.Event()
    original_start = AgentSession.start

    async def slow_start(self):
        await ready.wait()
        return await original_start(self)

    service, agents = make_test_service()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(AgentSession, "start", slow_start)
        pending = [asyncio.create_task(service.create()) for _ in range(MAX_SESSIONS)]
        await asyncio.sleep(0)
        assert len(service.sessions) == MAX_SESSIONS
        with pytest.raises(OverflowError):
            await service.create()
        ready.set()
        sessions = await asyncio.gather(*pending)
    assert len(sessions) == MAX_SESSIONS
    await service.shutdown()
    assert all(a.closed for a in agents)


@pytest.mark.asyncio
async def test_start_failure_frees_reserved_session_slot():
    from cli_agent.agent_api import AgentSession
    original_start = AgentSession.start

    async def failed_start(self):
        raise RuntimeError("startup fails")

    service, _ = make_test_service()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(AgentSession, "start", failed_start)
        with pytest.raises(RuntimeError, match="startup fails"):
            await service.create()
    assert not service.sessions
    session = await service.create()
    assert session.id in service.sessions
    await service.shutdown()


def test_cli_startup_flushes_token_before_uvicorn(monkeypatch, capsys):
    """A supervisor reading piped stdout must see the credential immediately."""
    import sys
    from cli_agent import agent_api

    service, _agents = make_test_service()
    monkeypatch.setattr(agent_api, "build_service", lambda args: service)
    monkeypatch.setattr(sys, "argv", ["cli-agent-api", "--port", "9876"])
    observed = []

    class Stream:
        def __init__(self):
            self.pending = ""
            self.flushed = []

        def write(self, text):
            self.pending += text

        def flush(self):
            self.flushed.append(self.pending)
            self.pending = ""

    stream = Stream()
    monkeypatch.setattr(sys, "stdout", stream)

    def fake_run(app, **kwargs):
        observed.append((list(stream.flushed), kwargs))
        assert stream.pending == ""
        assert f"Bearer-Token: {service.token}" in "".join(stream.flushed)
        assert "cli-agent-api: http://127.0.0.1:9876" in "".join(stream.flushed)

    import uvicorn
    monkeypatch.setattr(uvicorn, "run", fake_run)
    agent_api.main()
    assert observed[0][1]["host"] == "127.0.0.1"
    assert observed[0][1]["proxy_headers"] is False


@pytest.mark.asyncio
async def test_replay_capacity_adapts_to_knowledge_and_custom_limits():
    from cli_agent.agent_api import MAX_EVENTS_PER_RUN, REPLAY_EVENTS_PER_TOOL
    service, _ = make_test_service()
    session = await service.create()
    agent = session.agent
    assert session._replay_capacity() >= MAX_EVENTS_PER_RUN
    agent.max_tool_calls = 2500
    agent._okf_options = SimpleNamespace(max_tool_calls=1000)
    capacity = session._replay_capacity()
    assert capacity >= (2500 + 3 * 1000) * REPLAY_EVENTS_PER_TOOL
    run = session.submit("custom")
    assert run.max_events == capacity
    await service.shutdown()


@pytest.mark.asyncio
async def test_browser_run_without_sse_times_out_and_releases_session(monkeypatch):
    from cli_agent import agent_api
    monkeypatch.setattr(agent_api, "BROWSER_SSE_CONNECT_TIMEOUT", 0.04)
    service, agents = make_test_service()
    session = await service.create()
    try:
        waiting = session.submit("needs approval", require_listener=True)
        for _ in range(200):
            if waiting.done:
                break
            await asyncio.sleep(0.005)
        assert waiting.status == "failed"
        assert "SSE-Client" in (waiting.error or "")
        assert not agents[0].history
        assert not session._busy
        # Headless clients continue to be able to run without an SSE listener.
        next_run = session.submit("hello")
        for _ in range(100):
            if next_run.done:
                break
            await asyncio.sleep(0.005)
        assert next_run.answer == "reply hello"
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_old_sse_run_subscriber_cannot_unlock_new_browser_run():
    service, agents = make_test_service()
    session = await service.create()
    try:
        previous = session.submit("first")
        for _ in range(100):
            if previous.done:
                break
            await asyncio.sleep(0.005)
        assert previous.done
        session.subscribe(previous)
        current = session.submit("needs approval", require_listener=True)
        try:
            for _ in range(100):
                if current.status == "running":
                    break
                await asyncio.sleep(0.005)
            assert current.status == "running"
            assert not session.broker._pending
            assert agents[0].history == ["first"]
            session.subscribe(current)
            for _ in range(100):
                if session.broker._pending:
                    break
                await asyncio.sleep(0.005)
            assert session.broker._pending
            session.unsubscribe(previous)
            assert session.broker._pending
            session.broker.deny_all()
            for _ in range(100):
                if current.done:
                    break
                await asyncio.sleep(0.005)
            assert current.answer == "approved=False"
        finally:
            if current.listeners:
                session.unsubscribe(current)
            if previous.listeners:
                session.unsubscribe(previous)
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_agui_http_validation_bounded_json_and_proprietary_routes_removed():
    from cli_agent.agent_api import MAX_JSON_BYTES
    service, _agents = make_test_service()
    async with await client_for(service) as client:
        endpoint = "/ag-ui"
        auth = {**headers(service), "Accept": "text/event-stream"}
        data = {"threadId": "thread", "runId": "run",
                "protocolVersion": "1.0",
                "messages": [{"id": "u", "role": "user", "content": "hello"}]}
        for invalid in (
            {"messages": []},
            {"threadId": "x"},
            {"threadId": "x", "runId": "y", "messages": "wrong"},
        ):
            assert (await client.post(endpoint, json=invalid, headers=auth)).status_code == 400
        for bad in (b"[]", b"not-json", b"garbage" * (MAX_JSON_BYTES // 7 + 1)):
            response = await client.post(endpoint, content=bad, headers={
                **auth, "Content-Type": "application/json"
            })
            assert response.status_code == 400
        assert (await client.post(endpoint, content=b"{}", headers={
            **auth, "Content-Type": "text/plain"
        })).status_code == 400
        assert (await client.post(endpoint, json=data, headers={**headers(service), "Accept": "text/plain"})).status_code == 406
        assert (await client.post(endpoint, json=data, headers=auth)).status_code == 200
        assert (await client.post(endpoint, json=data, headers=auth)).status_code == 409
        sid = next(iter(service.sessions))
        for method, url in (
            ("post", f"/sessions/{sid}/runs"),
            ("get", f"/sessions/{sid}/runs/foo"),
            ("get", f"/sessions/{sid}/runs/foo/events"),
            ("post", f"/sessions/{sid}/runs/foo/approvals/bar"),
        ):
            assert (await getattr(client, method)(url, headers=auth)).status_code == 404
    await service.shutdown()


@pytest.mark.asyncio
async def test_agui_limits_session_mutations_while_running():
    service, agents = make_test_service()
    # Standalone thread IDs are explicitly mapped to isolated MCP sessions.
    session = await service.resolve_agui_thread("held-external-thread")
    gate = asyncio.Event()
    async def held_ask(prompt):
        await gate.wait()
        return "done"
    agents[0].ask = held_ask
    try:
        async with await client_for(service) as client:
            params = {"threadId": "held-external-thread", "runId": "held",
                      "protocolVersion": "1.0",
                      "messages": [{"id": "u", "role": "user", "content": "hello"}]}
            active = asyncio.create_task(client.post("/ag-ui", json=params, headers={
                **headers(service), "Accept": "text/event-stream"
            }))
            for _ in range(200):
                if session._busy:
                    break
                await asyncio.sleep(.005)
            assert session._busy
            response = await client.patch(
                f"/sessions/{session.id}/context",
                json={"tools": {"read": False}}, headers=headers(service)
            )
            assert response.status_code == 409
            assert agents[0].enabled["read"]
            assert (await client.post("/ag-ui", json={
                **params, "runId": "busy-again",
            }, headers={**headers(service), "Accept": "text/event-stream"})).status_code == 409
            gate.set()
            assert (await active).status_code == 200
    finally:
        gate.set()
        await service.shutdown()
