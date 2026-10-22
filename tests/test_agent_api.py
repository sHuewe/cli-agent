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


async def run_until_done(client, sid, rid, service):
    for _ in range(100):
        response = await client.get(
            f"/sessions/{sid}/runs/{rid}", headers=headers(service)
        )
        if response.json()["status"] in {"completed", "failed", "cancelled"}:
            return response.json()
        await asyncio.sleep(0.005)
    raise AssertionError("run did not finish")


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
async def test_session_isolation_lifecycle_and_agui_sse():
    service, agents = make_test_service()
    async with await client_for(service) as client:
        a = (await client.post("/sessions", headers=headers(service))).json()["session_id"]
        b = (await client.post("/sessions", headers=headers(service))).json()["session_id"]
        assert a != b
        rid = (await client.post(
            f"/sessions/{a}/runs", json={"prompt": "hello"}, headers=headers(service)
        )).json()["run_id"]
        state = await run_until_done(client, a, rid, service)
        assert state["answer"] == "reply hello"
        assert agents[0].history == ["hello"]
        assert agents[1].history == []
        assert (await client.get(f"/sessions/{b}/runs/{rid}", headers=headers(service))).status_code == 404
        stream = await client.get(f"/sessions/{a}/runs/{rid}/events", headers=headers(service))
        assert stream.status_code == 200
        assert stream.headers["content-type"].startswith("text/event-stream")
        events = [
            json.loads(line[6:]) for line in stream.text.splitlines()
            if line.startswith("data: ")
        ]
        assert [e["type"] for e in events] == [
            "RUN_STARTED", "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT",
            "TEXT_MESSAGE_END", "RUN_FINISHED",
        ]
        assert events[2]["delta"] == "reply hello"
        assert (await client.delete(f"/sessions/{a}", headers=headers(service))).status_code == 204
        assert agents[0].closed
        assert (await client.delete(f"/sessions/{a}", headers=headers(service))).status_code == 404
    await service.shutdown()
    assert agents[1].closed


@pytest.mark.asyncio
async def test_invalid_json_busy_state_and_error_redaction():
    service, _agents = make_test_service()
    async with await client_for(service) as client:
        sid = (await client.post("/sessions", headers=headers(service))).json()["session_id"]
        url = f"/sessions/{sid}/runs"
        for request in [
            {"json": {"prompt": ""}},
            {"json": {"prompt": 4}},
            {"json": {"prompt": "x", "workspace": "/etc"}},
            {"content": b"[]", "headers": {"Content-Type": "application/json"}},
            {"content": b"broken", "headers": {"Content-Type": "application/json"}},
            {"content": b"ok", "headers": {"Content-Type": "text/plain"}},
        ]:
            headers_arg = {**headers(service), **request.get("headers", {})}
            resp = await client.post(url, headers=headers_arg, **{k: v for k, v in request.items() if k != "headers"})
            assert resp.status_code == 400
        slow = (await client.post(url, json={"prompt": "slow"}, headers=headers(service))).json()["run_id"]
        assert (await client.post(url, json={"prompt": "second"}, headers=headers(service))).status_code == 409
        assert (await client.patch(f"/sessions/{sid}/context", json={"tools": {"read": False}}, headers=headers(service))).status_code == 409
        assert (await client.delete(f"/sessions/{sid}", headers=headers(service))).status_code == 204
        assert (await client.get(f"/sessions/{sid}/runs/{slow}", headers=headers(service))).status_code == 404
        sid = (await client.post("/sessions", headers=headers(service))).json()["session_id"]
        rid = (await client.post(f"/sessions/{sid}/runs", json={"prompt": "fail"}, headers=headers(service))).json()["run_id"]
        state = await run_until_done(client, sid, rid, service)
        assert state["status"] == "failed"
        assert "secret not for stream" in state["error"]  # authenticated diagnostics
        stream = await client.get(f"/sessions/{sid}/runs/{rid}/events", headers=headers(service))
        assert "secret not for stream" not in stream.text
    await service.shutdown()


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
async def test_approval_is_per_run_and_denied_without_listener():
    service, _agents = make_test_service()
    async with await client_for(service) as client:
        sid = (await client.post("/sessions", headers=headers(service))).json()["session_id"]
        run_url = f"/sessions/{sid}/runs"
        rid = (await client.post(run_url, json={"prompt": "needs approval"}, headers=headers(service))).json()["run_id"]
        assert (await run_until_done(client, sid, rid, service))["answer"] == "approved=False"
        session = service.sessions[sid]
        session.agent.approval_gate = asyncio.Event()
        rid2 = (await client.post(run_url, json={"prompt": "needs approval"}, headers=headers(service))).json()["run_id"]
        session.subscribe(session._run)
        session.agent.approval_gate.set()
        for _ in range(100):
            pending = list(session.broker._pending)
            if pending:
                break
            await asyncio.sleep(0.005)
        assert pending
        assert (await client.post(
            f"/sessions/{sid}/runs/{rid}/approvals/{pending[0]}",
            json={"decision": "yes"}, headers=headers(service)
        )).status_code == 404
        assert (await client.post(
            f"/sessions/{sid}/runs/{rid2}/approvals/{pending[0]}",
            json={"decision": "wrong"}, headers=headers(service)
        )).status_code == 400
        assert (await client.post(
            f"/sessions/{sid}/runs/{rid2}/approvals/{pending[0]}",
            json={"decision": "session"}, headers=headers(service)
        )).status_code == 200
        assert (await run_until_done(client, sid, rid2, service))["answer"] == "approved=session"
        assert (await client.post(
            f"/sessions/{sid}/runs/{rid2}/approvals/{pending[0]}",
            json={"decision": "yes"}, headers=headers(service)
        )).status_code == 409
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
async def test_sse_replay_event_id_validation_and_tool_events():
    service, _agents = make_test_service()
    async with await client_for(service) as client:
        sid = (await client.post("/sessions", headers=headers(service))).json()["session_id"]
        session = service.sessions[sid]
        rid = (await client.post(
            f"/sessions/{sid}/runs", json={"prompt": "events"}, headers=headers(service)
        )).json()["run_id"]
        await run_until_done(client, sid, rid, service)
        await session.on_agent_event("tool_started", {"call_id": "x", "tool": "os__read_file"})
        # The finished run must not be mutated by subsequent events.
        assert len(session._run.events) == 5
        events_url = f"/sessions/{sid}/runs/{rid}/events"
        replay = await client.get(events_url, headers={**headers(service), "Last-Event-ID": "3"})
        assert [json.loads(x[6:])["type"] for x in replay.text.splitlines() if x.startswith("data: ")] == [
            "TEXT_MESSAGE_END", "RUN_FINISHED"
        ]
        for invalid in ["-1", "garbage", "100"]:
            assert (await client.get(events_url, headers={
                **headers(service), "Last-Event-ID": invalid
            })).status_code == 400
        assert (await client.get(
            f"/sessions/{sid}/runs/wrong/events", headers=headers(service)
        )).status_code == 404
        session._run.status = "running"
        await session.on_agent_event("tool_started", {"call_id": "t1", "tool": "os__read_file"})
        await session.on_agent_event("tool_finished", {"call_id": "t1", "status": "success"})
        assert [e[1]["type"] for e in session._run.events[-3:]] == [
            "TOOL_CALL_START", "TOOL_CALL_END", "CUSTOM"
        ]
        session._run.status = "completed"
    await service.shutdown()


@pytest.mark.asyncio
async def test_api_validation_and_disconnect_fails_closed():
    service, _agents = make_test_service()
    async with await client_for(service) as client:
        sid = (await client.post("/sessions", headers=headers(service))).json()["session_id"]
        assert (await client.get(f"/sessions/{sid}/context", headers=headers(service))).status_code == 200
        assert (await client.patch(f"/sessions/{sid}/context", json={"tools": {"unexpected": True}}, headers=headers(service))).status_code == 400
        assert (await client.patch(f"/sessions/{sid}/context", json={"unexpected": True}, headers=headers(service))).status_code == 400
        assert (await client.patch(f"/sessions/{sid}/context", json={"contexts": {"file-a": 1}}, headers=headers(service))).status_code == 400
        assert (await client.post(f"/sessions/{sid}/runs", json={"prompt": "x" * (512 * 1024 + 1)}, headers=headers(service))).status_code == 400
        assert (await client.post(f"/sessions/{sid}/runs", content=b" " * (1024 * 1024 + 1), headers={**headers(service), "Content-Type": "application/json"})).status_code == 400
        assert (await client.get("/sessions/not-a-session/context", headers=headers(service))).status_code == 404
        assert (await client.patch("/sessions/not-a-session/context", json={"tools": {}}, headers=headers(service))).status_code == 404
    await service.shutdown()


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

    fake_config = FakeConfig(model=FakeModel(), logging=SimpleNamespace())
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


@pytest.mark.asyncio
async def test_context_patch_rechecks_busy_after_body_read():
    from starlette.requests import Request
    service, agents = make_test_service()
    entered = asyncio.Event()
    release = asyncio.Event()
    original_stream = Request.stream

    async def paused_stream(request):
        if request.url.path.endswith("/context") and request.method == "PATCH":
            entered.set()
            await release.wait()
        async for chunk in original_stream(request):
            yield chunk

    async with await client_for(service) as client:
        sid = (await client.post("/sessions", headers=headers(service))).json()["session_id"]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Request, "stream", paused_stream)
            pending = asyncio.create_task(client.patch(
                f"/sessions/{sid}/context",
                json={"tools": {"read": False}}, headers=headers(service),
            ))
            await asyncio.wait_for(entered.wait(), 3)
            rid = (await client.post(f"/sessions/{sid}/runs", json={"prompt": "slow"}, headers=headers(service))).json()["run_id"]
            release.set()
            response = await asyncio.wait_for(pending, 3)
            assert response.status_code == 409
            assert agents[0].enabled["read"] is True
            assert service.sessions[sid]._run.id == rid
    await service.shutdown()


@pytest.mark.asyncio
async def test_old_run_subscribers_do_not_authorize_new_run_approvals():
    service, _agents = make_test_service()
    async with await client_for(service) as client:
        sid = (await client.post("/sessions", headers=headers(service))).json()["session_id"]
        session = service.sessions[sid]
        url = f"/sessions/{sid}/runs"
        first_id = (await client.post(url, json={"prompt": "first"}, headers=headers(service))).json()["run_id"]
        await run_until_done(client, sid, first_id, service)
        previous = session._run
        session.subscribe(previous)  # Previous SSE generator is still draining.
        assert previous.listeners == 1
        next_id = (await client.post(
            url, json={"prompt": "needs approval"}, headers=headers(service)
        )).json()["run_id"]
        current = session._run
        assert current is not previous
        assert current.listeners == 0
        result = await run_until_done(client, sid, next_id, service)
        assert result["answer"] == "approved=False"
        assert not session.broker._pending
        session.unsubscribe(previous)
        assert current.listeners == 0
    await service.shutdown()


@pytest.mark.asyncio
async def test_stale_sse_disconnect_does_not_deny_current_run_approval():
    service, _agents = make_test_service()
    async with await client_for(service) as client:
        sid = (await client.post("/sessions", headers=headers(service))).json()["session_id"]
        session = service.sessions[sid]
        url = f"/sessions/{sid}/runs"
        first_id = (await client.post(url, json={"prompt": "first"}, headers=headers(service))).json()["run_id"]
        await run_until_done(client, sid, first_id, service)
        previous = session._run
        session.subscribe(previous)
        session.agent.approval_gate = asyncio.Event()
        next_id = (await client.post(url, json={"prompt": "needs approval"}, headers=headers(service))).json()["run_id"]
        current = session._run
        session.subscribe(current)
        session.agent.approval_gate.set()
        for _ in range(100):
            if session.broker._pending:
                break
            await asyncio.sleep(0.005)
        assert session.broker._pending
        session.unsubscribe(previous)
        assert session.broker._pending  # Old listener must not deny the new request.
        request_id = next(iter(session.broker._pending))
        assert (await client.post(
            f"/sessions/{sid}/runs/{next_id}/approvals/{request_id}",
            json={"decision": "yes"}, headers=headers(service)
        )).status_code == 200
        assert (await run_until_done(client, sid, next_id, service))["answer"] == "approved=True"
        session.unsubscribe(current)
    await service.shutdown()


@pytest.mark.asyncio
async def test_current_run_last_listener_disconnect_denies_pending_approval():
    service, _agents = make_test_service()
    async with await client_for(service) as client:
        sid = (await client.post("/sessions", headers=headers(service))).json()["session_id"]
        session = service.sessions[sid]
        url = f"/sessions/{sid}/runs"
        session.agent.approval_gate = asyncio.Event()
        rid = (await client.post(url, json={"prompt": "needs approval"}, headers=headers(service))).json()["run_id"]
        current = session._run
        session.subscribe(current)
        session.agent.approval_gate.set()
        for _ in range(100):
            if session.broker._pending:
                break
            await asyncio.sleep(0.005)
        assert session.broker._pending
        session.unsubscribe(current)
        assert (await run_until_done(client, sid, rid, service))["answer"] == "approved=False"
    await service.shutdown()


@pytest.mark.asyncio
async def test_chunked_json_limit_stops_before_buffering_extra_data():
    from starlette.requests import Request
    from cli_agent.agent_api import MAX_JSON_BYTES

    service, _agents = make_test_service()
    consumed = []
    original_stream = Request.stream

    async def oversized_stream(request):
        if request.method == "POST" and request.url.path.endswith("/runs"):
            for piece in (b'{"prompt":"', b"x" * MAX_JSON_BYTES, b"this-must-not-be-read"):
                consumed.append(piece[:24])
                yield piece
            return
        async for piece in original_stream(request):
            yield piece

    async with await client_for(service) as client:
        sid = (await client.post("/sessions", headers=headers(service))).json()["session_id"]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Request, "stream", oversized_stream)
            resp = await client.post(
                f"/sessions/{sid}/runs",
                content=b'{}',
                headers={**headers(service), "Content-Type": "application/json"},
            )
        assert resp.status_code == 400
        assert "zu groß" in resp.json()["error"]
        assert len(consumed) == 2
    await service.shutdown()


@pytest.mark.asyncio
async def test_chunked_json_valid_without_content_length():
    from starlette.requests import Request

    service, _agents = make_test_service()
    original_stream = Request.stream

    async def chunked_stream(request):
        if request.method == "POST" and request.url.path.endswith("/runs"):
            yield b'{"pro'
            yield b'mpt":"hello"}'
            return
        async for piece in original_stream(request):
            yield piece

    async with await client_for(service) as client:
        sid = (await client.post("/sessions", headers=headers(service))).json()["session_id"]
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(Request, "stream", chunked_stream)
            resp = await client.post(
                f"/sessions/{sid}/runs",
                content=b'{}',
                headers={**headers(service), "Content-Type": "application/json"},
            )
        assert resp.status_code == 202
        state = await run_until_done(client, sid, resp.json()["run_id"], service)
        assert state["answer"] == "reply hello"
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
async def test_long_supported_run_replays_all_events_from_beginning():
    """200 calls produce 600 tool events; late SSE subscribers retain RUN_STARTED."""
    from cli_agent.agent_api import MAX_EVENTS_PER_RUN
    service, _ = make_test_service()
    async with await client_for(service) as client:
        sid = (await client.post("/sessions", headers=headers(service))).json()["session_id"]
        session = service.sessions[sid]
        rid = (await client.post(
            f"/sessions/{sid}/runs", json={"prompt": "first"}, headers=headers(service)
        )).json()["run_id"]
        await run_until_done(client, sid, rid, service)
        run = session._run
        assert run.max_events >= 3 * 200 + 5
        # Add the tool events to the completed run to simulate the SSE history
        # after a legitimate 200-tool agent loop.
        for index in range(200):
            run.emit({"type": "TOOL_CALL_START", "toolCallId": str(index)})
            run.emit({"type": "TOOL_CALL_END", "toolCallId": str(index)})
            run.emit({"type": "CUSTOM", "name": "tool_status", "value": {"status": "success"}})
        assert len(run.events) == 605
        assert run.events[0][1]["type"] == "RUN_STARTED"
        response = await client.get(
            f"/sessions/{sid}/runs/{rid}/events", headers=headers(service)
        )
        assert response.status_code == 200
        events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
        assert len(events) == 605
        assert events[0]["type"] == "RUN_STARTED"
    await service.shutdown()


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
