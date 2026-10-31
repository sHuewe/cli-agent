"""AG-UI 1.0 endpoint: HTTP/SSE, thread history, tool approvals and validation."""
from __future__ import annotations

import asyncio
import json
import uuid
from importlib.resources import files

import httpx
import pytest
from jsonschema import Draft202012Validator

from cli_agent.interfaces.agent_api import AgentApiService, create_app
from cli_agent.interfaces.ag_ui_protocol import parse_run_input, encode_sse
from cli_agent.interfaces.web_ui_sessions import BrowserSessionRegistry


class AgUiAgent:
    def __init__(self, session):
        self.session = session
        self.history = []
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        pass

    async def ask(self, prompt):
        self.calls.append(prompt)
        if prompt == "needs approval":
            # The real tool dispatcher asks for authorization BEFORE
            # announcing tool_started. It passes the model's original ID.
            self._agui_approval_call_id = "tool-1"
            approved = await self.session.approve(
                "os__write_file", {"path": "test.txt", "content": "content"}
            )
            if approved:
                await self.session.on_agent_event(
                    "tool_started", {"call_id": "tool-1", "tool": "os__write_file"}
                )
            await self.session.on_agent_event(
                "tool_finished", {"call_id": "tool-1", "status": "success" if approved else "denied"}
            )
            return f"approved={approved}"
        return "reply " + prompt


def _input(thread, prompt, *, run_id=None, messages=None, resume=None):
    value = {
        "threadId": thread, "runId": run_id or uuid.uuid4().hex,
        "protocolVersion": "1.0",
        "messages": messages if messages is not None else [
            {"id": "user-1", "role": "user", "content": prompt}
        ],
    }
    if resume is not None:
        value["resume"] = resume
    return value


_AGUI_SCHEMA = json.loads(
    files("cli_agent.interfaces").joinpath("ag_ui_schema_1_0.json").read_text("utf-8")
)
_EVENT_VALIDATOR = Draft202012Validator(_AGUI_SCHEMA)
_INPUT_VALIDATOR = Draft202012Validator({
    **_AGUI_SCHEMA, "$ref": "#/$defs/RunAgentInput"
})
_CAPABILITIES_VALIDATOR = Draft202012Validator({
    **_AGUI_SCHEMA, "$ref": "#/$defs/AgentCapabilities"
})


def _events(response):
    assert response.status_code == 200, response.text
    assert "text/event-stream" in response.headers["content-type"]
    events = [
        json.loads(line[6:]) for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    for event in events:
        _EVENT_VALIDATOR.validate(event)
    return events


@pytest.mark.asyncio
async def test_agui_standard_post_sse_and_multiple_runs_same_thread():
    created = []
    def factory(session):
        agent = AgUiAgent(session)
        created.append(agent)
        return agent

    service = AgentApiService(factory)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)),
            base_url="http://127.0.0.1:9191"
        ) as client:
            auth = {"Authorization": "Bearer " + service.token,
                    "Accept": "text/event-stream"}
            first = _input("external-thread", "Hello")
            result = await client.post("/ag-ui", json=first, headers=auth)
            events = _events(result)
            assert events[0]["type"] == "RUN_STARTED"
            assert events[0]["threadId"] == "external-thread"
            assert events[0]["runId"] == first["runId"]
            assert events[0]["protocolVersion"] == "1.0"
            assert [x["type"] for x in events][-4:] == [
                "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT",
                "TEXT_MESSAGE_END", "RUN_FINISHED",
            ]
            assert events[-1]["outcome"] == {"type": "success"}
            assert events[2]["delta"] == "reply Hello"
            assert len(service.sessions) == 1

            messages = [
                first["messages"][0],
                {"id": events[1]["messageId"], "role": "assistant",
                 "content": "reply Hello"},
                {"id": "user-2", "role": "user", "content": "Next"},
            ]
            second = _input("external-thread", "Next", messages=messages)
            another = _events(await client.post("/ag-ui", json=second, headers=auth))
            assert another[-1]["type"] == "RUN_FINISHED"
            assert created[0].calls == ["Hello", "Next"]
            assert created[0].history == [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "reply Hello"},
            ]
    finally:
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_agui_interrupt_and_resume_without_restarting_agent_or_tool(approved):
    created = []
    def factory(session):
        agent = AgUiAgent(session)
        created.append(agent)
        return agent

    service = AgentApiService(factory)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)),
            base_url="http://127.0.0.1:9191",
        ) as client:
            auth = {"Authorization": "Bearer " + service.token,
                    "Accept": "text/event-stream"}
            first = _input("interrupt-thread", "needs approval")
            events = _events(await client.post("/ag-ui", json=first, headers=auth))
            assert events[-1]["type"] == "RUN_FINISHED"
            outcome = events[-1]["outcome"]
            assert outcome["type"] == "interrupt"
            interrupt = outcome["interrupts"][0]
            assert interrupt["reason"] == "tool_call"
            assert interrupt["toolCallId"] == "tool-1"
            assert [e["type"] for e in events] == [
                "RUN_STARTED", "TOOL_CALL_START", "TOOL_CALL_ARGS",
                "TOOL_CALL_END", "RUN_FINISHED",
            ]
            assert created[0].calls == ["needs approval"]
            assert service.sessions[next(iter(service.sessions))]._busy

            resumed = _input(
                "interrupt-thread", "needs approval",
                messages=first["messages"],
                resume=[{"interruptId": interrupt["id"],
                         "status": "resolved",
                         "payload": {"approved": approved}}],
            )
            continued = _events(await client.post("/ag-ui", json=resumed, headers=auth))
            assert continued[0]["type"] == "RUN_STARTED"
            assert continued[0]["runId"] == resumed["runId"]
            assert any(e["type"] == "TOOL_CALL_RESULT" for e in continued)
            assert not any(e["type"] == "TOOL_CALL_START" for e in continued)
            assert any(
                e["type"] == "TOOL_CALL_RESULT" and e["toolCallId"] == "tool-1"
                for e in continued
            )
            assert continued[-1]["type"] == "RUN_FINISHED"
            assert any(e.get("delta") == f"approved={approved}"
                       for e in continued)
            assert created[0].calls == ["needs approval"]
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_agui_rejects_invalid_input_and_replay():
    service = AgentApiService(lambda session: AgUiAgent(session))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)),
            base_url="http://127.0.0.1:9191"
        ) as client:
            auth = {"Authorization": "Bearer " + service.token,
                    "Accept": "text/event-stream"}
            assert (await client.post("/ag-ui", json={"threadId": "t"},
                                      headers=auth)).status_code == 400
            payload = _input("t", "hello")
            assert (await client.post("/ag-ui", json=payload, headers=auth)).status_code == 200
            assert (await client.post("/ag-ui", json=payload, headers=auth)).status_code == 409
            assert (await client.post("/ag-ui", json=_input("x", "abc"),
                                      headers={"Accept": "text/event-stream"})).status_code == 401
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_browser_agui_requires_exclusive_tab_lease():
    service = AgentApiService(lambda session: AgUiAgent(session))
    initial = await service.create()
    registry = BrowserSessionRegistry(service, initial.id)
    origin = "http://127.0.0.1:9191"
    ui = {
        "token": "browser-secret", "session_id": initial.id,
        "registry": registry, "origin": origin,
        "info": {"model": "fake", "workspace": ".", "mcp_servers": []},
        "shutdown": lambda: None,
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service, ui=ui)),
            base_url=origin
        ) as client:
            header = {"Authorization": "Bearer browser-secret",
                      "X-Cli-Agent-Browser": "1", "Origin": origin,
                      "Accept": "text/event-stream"}
            assert (await client.post(
                "/ag-ui", json=_input(initial.id, "hello"),
                headers=header
            )).status_code == 403
            claim = await client.post("/ui/bootstrap", json={}, headers=header)
            assert claim.status_code == 200
            header["X-Cli-Agent-Lease"] = claim.json()["lease"]
            assert _events(await client.post(
                "/ag-ui", json=_input(initial.id, "hello"),
                headers=header
            ))[-1]["type"] == "RUN_FINISHED"
            assert (await client.post(
                "/ag-ui", json=_input("unowned-thread", "hi"), headers=header
            )).status_code == 403
    finally:
        await service.shutdown()


def test_agui_known_input_shapes():
    assert parse_run_input(_input("t", "hi")).prompt == "hi"
    assert "data: " in encode_sse({"type": "RUN_STARTED", "runId": "a"})
    with pytest.raises(ValueError):
        parse_run_input({**_input("t", "hi"), "protocolVersion": "2.0"})


@pytest.mark.parametrize(
    "mutation",
    [
        lambda x: x.pop("threadId"),
        lambda x: x.update({"threadId": ""}),
        lambda x: x.update({"threadId": 42}),
        lambda x: x.update({"runId": 3}),
        lambda x: x.update({"protocolVersion": 1}),
        lambda x: x.update({"messages": {}}),
        lambda x: x.update({"messages": [{}]}),
        lambda x: x.update({"messages": [{"id": 9, "role": "user", "content": "x"}]}),
        lambda x: x.update({"messages": [{"id": "u", "role": "user", "content": 7}]}),
        lambda x: x.update({"messages": [{"id": "u", "role": "user", "content": [False]}]}),
        lambda x: x.update({"messages": [{"id": "u", "role": "user", "content": [{"type": "text", "text": 7}]}]}),
        lambda x: x.update({"resume": "not-an-array"}),
        lambda x: x.update({"resume": [{"interruptId": "x", "status": "bad"}]}),
        lambda x: x.update({"resume": [{"interruptId": 7, "status": "resolved"}]}),
        lambda x: x.update({"tools": "not-an-array"}),
        lambda x: x.update({"tools": [{"description": "missing-name"}]}),
        lambda x: x.update({"context": "not-an-array"}),
        lambda x: x.update({"context": [{"value": "missing-description"}]}),
        lambda x: x.update({"state": None}),
    ],
)
def test_agui_rejects_malformed_known_fields(mutation):
    payload = _input("th", "prompt")
    mutation(payload)
    with pytest.raises(ValueError):
        parse_run_input(payload)


def test_agui_accepts_partial_multimodal_user_text_without_image_access():
    payload = _input("th", "")
    payload["messages"] = [
        {"id": "m", "role": "user", "content": [
            {"type": "image", "source": {"type": "url", "value": "https://example.org/a.png"}},
            {"type": "text", "text": "Describe whatever you can"},
        ]}
    ]
    assert parse_run_input(payload).prompt == "Describe whatever you can"


@pytest.mark.asyncio
async def test_browser_local_commands_stay_outside_agui_transport():
    called = []
    def factory(session):
        agent = AgUiAgent(session)
        called.append(agent)
        return agent

    service = AgentApiService(factory)
    initial = await service.create()
    origin = "http://127.0.0.1:9191"
    ui = {
        "token": "browser-secret", "session_id": initial.id,
        "registry": BrowserSessionRegistry(service, initial.id),
        "origin": origin,
        "info": {"model": "fake", "workspace": ".", "mcp_servers": []},
        "shutdown": lambda: None,
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service, ui=ui)),
            base_url=origin
        ) as client:
            headers = {"Authorization": "Bearer browser-secret",
                       "X-Cli-Agent-Browser": "1", "Origin": origin}
            # No valid lease: cannot call the local-only command API.
            assert (await client.post("/ui/command", json={
                "session_id": initial.id, "command": "tokens",
            }, headers=headers)).status_code == 403
            claim = await client.post("/ui/bootstrap", json={}, headers=headers)
            headers["X-Cli-Agent-Lease"] = claim.json()["lease"]
            for body in (
                {"session_id": initial.id, "command": "add_web_context", "argument": "example.com"},
                {"session_id": initial.id, "command": "enable", "argument": "two words"},
                {"session_id": initial.id, "command": "tokens", "argument": "extra"},
                {"session_id": initial.id, "command": "unknown"},
                {"session_id": initial.id, "command": "add_web_context"},
                {"command": "tokens"},
            ):
                assert (await client.post("/ui/command", json=body,
                    headers=headers)).status_code == 400
            assert called[0].calls == []
            valid = await client.post("/ui/command", json={
                "session_id": initial.id, "command": "tokens",
            }, headers=headers)
            assert valid.status_code == 200
            assert valid.json()["answer"] == "reply tokens"
            assert called[0].calls == ["tokens"]
    finally:
        await service.shutdown()


def test_official_ag_ui_1_0_schema_accepts_the_standard_request():
    _INPUT_VALIDATOR.validate(_input("thread", "Hello"))
    assert _AGUI_SCHEMA["$id"] == "https://ag-ui.com/spec/1.0/schema.json"


@pytest.mark.asyncio
async def test_agui_capabilities_match_the_standard_and_advertise_only_supported_features():
    service = AgentApiService(lambda session: AgUiAgent(session))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)),
            base_url="http://127.0.0.1:9191",
        ) as client:
            assert (await client.get("/ag-ui/capabilities")).status_code == 401
            response = await client.get(
                "/ag-ui/capabilities",
                headers={"Authorization": "Bearer " + service.token},
            )
            assert response.status_code == 200
            capabilities = response.json()
            _CAPABILITIES_VALIDATOR.validate(capabilities)
            assert capabilities["transport"]["streaming"] is True
            assert capabilities["humanInTheLoop"]["interrupts"] is True
            assert capabilities["tools"]["clientProvided"] is True
            assert capabilities["transport"]["resumable"] is False
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_interrupted_session_can_be_closed_after_tab_release():
    service = AgentApiService(lambda session: AgUiAgent(session))
    initial = await service.create()
    origin = "http://127.0.0.1:9191"
    ui = {
        "token": "browser-secret", "session_id": initial.id,
        "registry": BrowserSessionRegistry(service, initial.id),
        "origin": origin, "info": {"model": "fake", "workspace": ".", "mcp_servers": []},
        "shutdown": lambda: None,
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service, ui=ui)),
            base_url=origin,
        ) as client:
            headers = {"Authorization": "Bearer browser-secret",
                       "X-Cli-Agent-Browser": "1", "Origin": origin,
                       "Accept": "text/event-stream"}
            claim = await client.post("/ui/bootstrap", json={}, headers=headers)
            headers["X-Cli-Agent-Lease"] = claim.json()["lease"]
            result = _events(await client.post(
                "/ag-ui", json=_input(initial.id, "needs approval"),
                headers=headers,
            ))
            assert result[-1]["outcome"]["type"] == "interrupt"
            assert initial._busy
            released = await client.post(
                "/ui/release", json={"session_id": initial.id}, headers=headers
            )
            assert released.status_code == 204
            deleted = await client.delete(
                "/ui/sessions/" + initial.id, headers=headers
            )
            assert deleted.status_code == 204
            assert initial.id not in service.sessions
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_agui_resume_requires_valid_pending_interrupt_and_boolean_decision():
    service = AgentApiService(lambda session: AgUiAgent(session))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)),
            base_url="http://127.0.0.1:9191",
        ) as client:
            headers = {"Authorization": "Bearer " + service.token,
                       "Accept": "text/event-stream"}
            first = _input("resume-guard", "needs approval")
            events = _events(await client.post("/ag-ui", json=first, headers=headers))
            interrupt_id = events[-1]["outcome"]["interrupts"][0]["id"]
            wrong = _input("resume-guard", "needs approval", messages=first["messages"],
                           resume=[{"interruptId": "unrecognized", "status": "resolved",
                                    "payload": {"approved": True}}])
            assert (await client.post("/ag-ui", json=wrong, headers=headers)).status_code == 409
            malformed = _input("resume-guard", "needs approval", messages=first["messages"],
                               resume=[{"interruptId": interrupt_id, "status": "resolved",
                                        "payload": {"approved": "yes"}}])
            assert (await client.post("/ag-ui", json=malformed, headers=headers)).status_code == 409
            unfinished = _input("resume-guard", "needs approval", messages=first["messages"])
            assert (await client.post("/ag-ui", json=unfinished, headers=headers)).status_code == 409
            multi = _input("resume-guard", "needs approval", messages=first["messages"],
                           resume=[{"interruptId": interrupt_id, "status": "resolved",
                                    "payload": {"approved": True}},
                                   {"interruptId": interrupt_id, "status": "cancelled"}])
            assert (await client.post("/ag-ui", json=multi, headers=headers)).status_code == 400
            valid = _input("resume-guard", "needs approval", messages=first["messages"],
                           resume=[{"interruptId": interrupt_id, "status": "cancelled"}])
            continued = _events(await client.post("/ag-ui", json=valid, headers=headers))
            assert continued[-1]["type"] == "RUN_FINISHED"
            assert any(e.get("delta") == "approved=False" for e in continued)
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_agui_session_run_buffers_are_bounded_and_lifecycle_is_isolated():
    from cli_agent.interfaces.agent_api import AgentRun

    service = AgentApiService(lambda session: AgUiAgent(session))
    session = await service.create()
    try:
        run = AgentRun(id="run-a", thread_id="thread", prompt="hello",
                       max_events=3, agui=True)
        assert not run.done
        assert run.snapshot() == {
            "run_id": "run-a", "status": "queued",
            "answer": None, "error": None,
        }
        for idx in range(5):
            run.emit({"type": "CUSTOM", "name": f"progress-{idx}", "value": {}})
        assert [seq for seq, _ in run.events] == [3, 4, 5]
        assert run.next_sequence == 6
        assert run.changed.is_set()
        session.subscribe(run)
        assert run.listeners == 1
        session.unsubscribe(run)
        assert run.listeners == 0
        with pytest.raises(RuntimeError, match="untergelaufen"):
            session.unsubscribe(run)
        run.status = "failed"
        run.error = "simulated"
        assert run.done
        assert run.snapshot()["error"] == "simulated"

        assert session.run("unknown") is None
        assert not await service.remove("unknown")
        assert await service.remove(session.id)
        assert session.id not in service.sessions
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_agui_mcp_tool_events_keep_consistent_ids_and_only_safe_results():
    service = AgentApiService(lambda session: AgUiAgent(session))
    session = await service.create()
    from cli_agent.interfaces.agent_api import AgentRun
    session._run = AgentRun(id="run", thread_id="thread", prompt="x",
                            agui=True, status="running")
    run = session._run
    try:
        session._pending_tool_id = None
        await session.on_agent_event("tool_started", {
            "call_id": "model-call", "tool": "os__read_file",
        })
        await session.on_agent_event("tool_finished", {
            "call_id": "model-call", "status": "success",
            "secret": "not for clients",
        })
        assert [event["type"] for _, event in run.events] == [
            "TOOL_CALL_START", "TOOL_CALL_END", "TOOL_CALL_RESULT",
        ]
        assert run.events[-1][1]["toolCallId"] == "model-call"
        assert "secret" not in str(run.events)
        assert not session._opened_tool_calls
        assert session._pending_tool_id is None
        run.status = "completed"
        before = len(run.events)
        await session.on_agent_event("tool_started", {
            "call_id": "late", "tool": "os__read_file",
        })
        assert len(run.events) == before
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_external_agui_threads_can_be_deleted_by_client_known_thread_id():
    """Sequential standalone conversations must not permanently exhaust slots."""
    from cli_agent.interfaces.agent_api import MAX_SESSIONS
    service = AgentApiService(lambda session: AgUiAgent(session))
    auth = {"Authorization": f"Bearer {service.token}", "Accept": "text/event-stream"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)),
            base_url="http://127.0.0.1:9191",
        ) as client:
            for index in range(MAX_SESSIONS + 3):
                thread = f"external-{index}"
                response = await client.post(
                    "/ag-ui", json=_input(thread, "hello"),
                    headers=auth,
                )
                assert _events(response)[-1]["type"] == "RUN_FINISHED"
                internal_id = service._agui_threads[thread]
                assert internal_id != thread
                assert internal_id in service.sessions
                assert (await client.delete(
                    f"/threads/{thread}",
                    headers={"Authorization": f"Bearer {service.token}"},
                )).status_code == 204
                assert internal_id not in service.sessions
                assert thread not in service._agui_threads
                assert not service.sessions
                assert (await client.delete(
                    f"/threads/{thread}",
                    headers={"Authorization": f"Bearer {service.token}"},
                )).status_code == 404
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_deleting_internal_session_invalidates_agui_thread_mapping():
    service = AgentApiService(lambda session: AgUiAgent(session))
    try:
        session = await service.resolve_agui_thread("client-thread")
        assert service._agui_threads["client-thread"] == session.id
        assert await service.remove(session.id)
        assert "client-thread" not in service._agui_threads
        replacement = await service.resolve_agui_thread("client-thread")
        assert replacement.id != session.id
        assert len(service.sessions) == 1
        assert not await service.remove_agui_thread(session.id)
        assert await service.remove_agui_thread("client-thread")
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_agui_thread_delete_requires_service_token_and_cannot_close_browser_session():
    service = AgentApiService(lambda session: AgUiAgent(session))
    browser_session = await service.create()
    origin = "http://127.0.0.1:9191"
    ui = {
        "token": "browser-secret", "session_id": browser_session.id,
        "registry": BrowserSessionRegistry(service, browser_session.id),
        "origin": origin,
        "info": {"model": "fake", "workspace": ".", "mcp_servers": []},
        "shutdown": lambda: None,
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service, ui=ui)),
            base_url=origin,
        ) as client:
            assert (await client.delete(f"/threads/{browser_session.id}")).status_code == 401
            assert (await client.delete(
                f"/threads/{browser_session.id}",
                headers={"Authorization": "Bearer browser-secret"},
            )).status_code == 401
            assert (await client.delete(
                f"/threads/{browser_session.id}",
                headers={"Authorization": f"Bearer {service.token}"},
            )).status_code == 404
            assert browser_session.id in service.sessions
            assert service._agui_threads == {}
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_agui_thread_deletion_releases_interrupted_agent_and_worker():
    service = AgentApiService(lambda session: AgUiAgent(session))
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)),
            base_url="http://127.0.0.1:9191",
        ) as client:
            token = {"Authorization": f"Bearer {service.token}"}
            first = _events(await client.post(
                "/ag-ui", json=_input("paused", "needs approval"),
                headers={**token, "Accept": "text/event-stream"},
            ))
            assert first[-1]["outcome"]["type"] == "interrupt"
            session = service.sessions[service._agui_threads["paused"]]
            assert session._busy
            assert (await client.delete("/threads/paused", headers=token)).status_code == 204
            assert session._closed
            assert not session._busy or session._task.done()
            assert session.id not in service.sessions
            assert not service._agui_threads
            assert _events(await client.post(
                "/ag-ui", json=_input("paused", "new conversation"),
                headers={**token, "Accept": "text/event-stream"},
            ))[-1]["type"] == "RUN_FINISHED"
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_slow_agui_thread_start_does_not_block_other_threads():
    """MCP startup is per-thread, not a global gate for all AG-UI runs."""
    from cli_agent.interfaces.agent_api import AgentSession

    service = AgentApiService(lambda session: AgUiAgent(session))
    existing = await service.resolve_agui_thread("existing")
    entered = asyncio.Event()
    release = asyncio.Event()
    original_start = AgentSession.start

    async def slow_first_start(self):
        if not entered.is_set():
            entered.set()
            await release.wait()
        await original_start(self)

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(AgentSession, "start", slow_first_start)
            first = asyncio.create_task(service.resolve_agui_thread("slow"))
            await asyncio.wait_for(entered.wait(), timeout=2)
            second = asyncio.create_task(service.resolve_agui_thread("slow"))
            await asyncio.sleep(0)
            assert not first.done()
            assert not second.done()

            assert await asyncio.wait_for(
                service.resolve_agui_thread("existing"), timeout=2
            ) is existing
            unrelated = await asyncio.wait_for(
                service.resolve_agui_thread("other"), timeout=2
            )
            assert unrelated is not existing
            release.set()
            first_session, second_session = await asyncio.wait_for(
                asyncio.gather(first, second), timeout=2
            )
            assert first_session is second_session
            assert first_session is not unrelated
            assert len(service.sessions) == 3
            assert len(service._agui_threads) == 3
            assert not service._agui_thread_inits
    finally:
        release.set()
        await service.shutdown()


@pytest.mark.asyncio
async def test_agui_thread_start_failure_releases_slot_and_allows_retry():
    """All awaiters share the failed startup and can later retry the thread."""
    from cli_agent.interfaces.agent_api import AgentSession

    service = AgentApiService(lambda session: AgUiAgent(session))
    original_start = AgentSession.start
    started = asyncio.Event()
    release = asyncio.Event()

    async def fail_first_start(self):
        if not started.is_set():
            started.set()
            await release.wait()
            raise RuntimeError("MCP connection failed")
        await original_start(self)

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(AgentSession, "start", fail_first_start)
            first = asyncio.create_task(service.resolve_agui_thread("retry"))
            await asyncio.wait_for(started.wait(), timeout=2)
            second = asyncio.create_task(service.resolve_agui_thread("retry"))
            await asyncio.sleep(0)
            release.set()
            results = await asyncio.wait_for(
                asyncio.gather(first, second, return_exceptions=True), timeout=2
            )
            assert all(
                isinstance(item, RuntimeError) and "MCP connection failed" in str(item)
                for item in results
            )
            assert not service.sessions
            assert not service._agui_threads
            assert not service._agui_thread_inits
            retried = await service.resolve_agui_thread("retry")
            assert retried.id in service.sessions
    finally:
        release.set()
        await service.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["delete", "shutdown"])
async def test_agui_thread_start_cancelled_during_cleanup(action):
    """Deletion/shutdown must also release a not-yet-published MCP session."""
    from cli_agent.interfaces.agent_api import AgentSession

    service = AgentApiService(lambda session: AgUiAgent(session))
    started = asyncio.Event()
    original_start = AgentSession.start

    async def stalled_start(self):
        if not started.is_set():
            started.set()
            await asyncio.Event().wait()
        await original_start(self)

    try:
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(AgentSession, "start", stalled_start)
            waiting = asyncio.create_task(service.resolve_agui_thread("abandoned"))
            await asyncio.wait_for(started.wait(), timeout=2)
            assert len(service.sessions) == 1
            if action == "delete":
                assert await asyncio.wait_for(
                    service.remove_agui_thread("abandoned"), timeout=2
                )
            else:
                await asyncio.wait_for(service.shutdown(), timeout=2)
            result = await asyncio.gather(waiting, return_exceptions=True)
            assert isinstance(result[0], RuntimeError)
            assert not service.sessions
            assert not service._agui_threads
            assert not service._agui_thread_inits
            if action == "delete":
                assert (await service.resolve_agui_thread("abandoned")).id in service.sessions
            else:
                with pytest.raises(RuntimeError, match="beendet"):
                    await service.resolve_agui_thread("abandoned")
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_explicit_api_session_is_used_for_agui_and_control_endpoints():
    """POST /sessions configuration must affect the very same AG-UI worker."""
    from cli_agent.interfaces.agent_api import MAX_SESSIONS

    service = AgentApiService(lambda session: AgUiAgent(session))
    headers = {"Authorization": f"Bearer {service.token}"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)),
            base_url="http://127.0.0.1:9191",
        ) as client:
            created = await client.post("/sessions", headers=headers)
            assert created.status_code == 201
            sid = created.json()["session_id"]
            original = service.sessions[sid]
            assert service._agui_threads[sid] == sid
            run = await client.post(
                "/ag-ui", json=_input(sid, "configured request"),
                headers={**headers, "Accept": "text/event-stream"},
            )
            assert _events(run)[-1]["type"] == "RUN_FINISHED"
            assert run.headers["x-cli-agent-session-id"] == sid
            assert len(service.sessions) == 1
            assert service.sessions[sid] is original
            assert original.agent.calls == ["configured request"]
            assert (await client.delete(f"/threads/{sid}", headers=headers)).status_code == 204
            assert not service.sessions
            assert not service._agui_threads
            assert (await client.delete(f"/sessions/{sid}", headers=headers)).status_code == 404
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_automatic_agui_thread_exposes_control_session_id():
    """Automatic threads must be configurable without private map access."""
    service = AgentApiService(lambda session: AgUiAgent(session))
    headers = {"Authorization": f"Bearer {service.token}"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)),
            base_url="http://127.0.0.1:9191",
        ) as client:
            response = await client.post(
                "/ag-ui", json=_input("external-control-thread", "hello"),
                headers={**headers, "Accept": "text/event-stream"},
            )
            assert _events(response)[-1]["type"] == "RUN_FINISHED"
            sid = response.headers["x-cli-agent-session-id"]
            assert sid != "external-control-thread"
            assert service.sessions[sid].agent.calls == ["hello"]
            # The session ID is usable by the retained control API.
            assert (await client.delete(f"/sessions/{sid}", headers=headers)).status_code == 204
            assert not service.sessions
            assert not service._agui_threads
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_external_agui_cannot_claim_browser_owned_session_id():
    """Neither the external bearer token nor a guessed thread ID binds a UI session."""
    service = AgentApiService(lambda session: AgUiAgent(session))
    browser_session = await service.create()
    ui = {
        "token": "browser-secret", "session_id": browser_session.id,
        "registry": BrowserSessionRegistry(service, browser_session.id),
        "origin": "http://127.0.0.1:9191",
        "info": {"model": "fake", "workspace": ".", "mcp_servers": []},
        "shutdown": lambda: None,
    }
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service, ui=ui)),
            base_url=ui["origin"],
        ) as client:
            auth = {"Authorization": f"Bearer {service.token}",
                    "Accept": "text/event-stream"}
            result = await client.post(
                "/ag-ui", json=_input(browser_session.id, "external"),
                headers=auth,
            )
            assert _events(result)[-1]["type"] == "RUN_FINISHED"
            assert result.headers["x-cli-agent-session-id"] != browser_session.id
            assert browser_session.agent.calls == []
            assert len(service.sessions) == 2
            assert (await client.delete(
                f"/threads/{browser_session.id}",
                headers={"Authorization": f"Bearer {service.token}"},
            )).status_code == 204
            assert browser_session.id in service.sessions
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_explicit_cancel_stops_only_matching_run_and_preserves_session():
    """A disconnect is not cancellation; explicit cancellation is run-scoped."""
    entered = asyncio.Event()
    class SlowAgent(AgUiAgent):
        async def ask(self, prompt):
            if prompt == "slow":
                entered.set()
                await asyncio.sleep(60)
            return await super().ask(prompt)

    service = AgentApiService(lambda session: SlowAgent(session))
    headers = {"Authorization": "Bearer " + service.token}
    try:
        session = await service.resolve_agui_thread("cancel-thread")
        run = session.submit("slow", agui=True, thread_id="cancel-thread",
                             run_id="cancel-run", require_listener=False)
        await asyncio.wait_for(entered.wait(), timeout=2)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=create_app(service)),
            base_url="http://127.0.0.1:9191",
        ) as client:
            payload = {"action": "cancel", "threadId": "cancel-thread",
                       "runId": "cancel-run"}
            wrong = await client.post("/ag-ui", json={**payload, "runId": "wrong"},
                                      headers=headers)
            assert wrong.status_code == 409
            assert session._busy
            accepted = await client.post("/ag-ui", json=payload, headers=headers)
            assert accepted.status_code == 202
            for _ in range(100):
                if run.done and not session._busy:
                    break
                await asyncio.sleep(0.01)
            assert run.status == "cancelled"
            assert run.events[-1][1]["outcome"] == {"type": "cancelled"}
            assert not session._closed
            # Same MCP-owning session accepts another request.
            next_run = session.submit("next", agui=True,
                                      thread_id="cancel-thread", run_id="next-run",
                                      require_listener=False)
            for _ in range(100):
                if next_run.done:
                    break
                await asyncio.sleep(0.01)
            assert next_run.status == "completed"
            assert next_run.answer == "reply next"
            assert service.sessions[session.id] is session
    finally:
        await service.shutdown()


@pytest.mark.asyncio
async def test_close_immediately_after_run_cancel_does_not_deadlock():
    entered = asyncio.Event()

    class SlowAgent(AgUiAgent):
        async def ask(self, prompt):
            entered.set()
            await asyncio.sleep(60)

    service = AgentApiService(lambda session: SlowAgent(session))
    session = await service.resolve_agui_thread("close-during-cancel")
    try:
        session.submit("slow", thread_id="close-during-cancel",
                       run_id="close-run", agui=True)
        await asyncio.wait_for(entered.wait(), 2)
        session.cancel_run(thread_id="close-during-cancel", run_id="close-run")
        await asyncio.wait_for(session.close(), 2)
        assert session._closed
    finally:
        await asyncio.wait_for(service.shutdown(), 2)


@pytest.mark.asyncio
async def test_mcp_session_propagates_cancel_notification(monkeypatch):
    """Cancel the actual MCP request ID, not an unrelated session request."""
    from mcp import ClientSession
    from cli_agent.runtime.agent_mcp import CancellableClientSession

    entered = asyncio.Event()
    observed = []

    async def blocking_request(self, *_args, **_kwargs):
        entered.set()
        await asyncio.sleep(60)

    async def recorded_notification(self, notification, related_request_id=None):
        observed.append((notification.root.params.requestId, related_request_id))

    monkeypatch.setattr(ClientSession, "send_request", blocking_request)
    monkeypatch.setattr(CancellableClientSession, "send_notification",
                        recorded_notification)
    session = object.__new__(CancellableClientSession)
    session._request_id = 42
    task = asyncio.create_task(session.send_request(object(), object))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert observed == [(42, 42)]


@pytest.mark.asyncio
async def test_mcp_cancel_notification_timeout_does_not_block_run_cancellation(monkeypatch):
    """Even a non-reading MCP transport must not block CancelledError."""
    from mcp import ClientSession
    from cli_agent.runtime.agent_mcp import (
        CancellableClientSession, MCP_CANCEL_NOTIFY_TIMEOUT_SECONDS,
    )

    entered = asyncio.Event()

    async def blocking_request(self, *_args, **_kwargs):
        entered.set()
        await asyncio.sleep(60)

    async def blocked_notification(self, *_args, **_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(ClientSession, "send_request", blocking_request)
    monkeypatch.setattr(CancellableClientSession, "send_notification",
                        blocked_notification)
    session = object.__new__(CancellableClientSession)
    session._request_id = 24
    task = asyncio.create_task(session.send_request(object(), object))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, MCP_CANCEL_NOTIFY_TIMEOUT_SECONDS + 1)
