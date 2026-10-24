"""AG-UI 1.0 endpoint: HTTP/SSE, thread history, tool approvals and validation."""
from __future__ import annotations

import json
import uuid
from importlib.resources import files

import httpx
import pytest
from jsonschema import Draft202012Validator

from cli_agent.agent_api import AgentApiService, create_app
from cli_agent.ag_ui_protocol import parse_run_input, encode_sse
from cli_agent.web_ui_sessions import BrowserSessionRegistry


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
            await self.session.on_agent_event(
                "tool_started", {"call_id": "tool-1", "tool": "os__write_file"}
            )
            approved = await self.session.approve(
                "os__write_file", {"path": "test.txt", "content": "content"}
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
    files("cli_agent").joinpath("ag_ui_schema_1_0.json").read_text("utf-8")
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
