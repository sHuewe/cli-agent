"""Contract tests for the Open WebUI Pipe without an Open WebUI installation."""
import asyncio
import importlib.util
import json
from pathlib import Path

import httpx
import pytest


PIPE_PATH = Path(__file__).resolve().parents[1] / "integrations/open-webui/cli_agent_pipe.py"
spec = importlib.util.spec_from_file_location("cli_agent_open_webui_pipe", PIPE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class FakeApi:
    def __init__(self, events=None):
        self.events = events or [
            {"type": "RUN_STARTED"},
            {"type": "TEXT_MESSAGE_CONTENT", "delta": "Hallo"},
            {"type": "RUN_FINISHED"},
        ]
        self.sessions = []
        self.deleted = []
        self.prompts = []
        self.decisions = []
        self.stale = False
        self.busy = False

    def handle(self, request):
        assert request.headers.get("authorization") == "Bearer secret"
        path = request.url.path
        if request.method == "POST" and path == "/sessions":
            sid = f"s{len(self.sessions) + 1}"
            self.sessions.append(sid)
            return httpx.Response(201, json={"session_id": sid})
        if request.method == "DELETE" and path.startswith("/sessions/"):
            self.deleted.append(path.split("/")[-1])
            return httpx.Response(204)
        if request.method == "POST" and path.endswith("/runs"):
            if self.stale:
                self.stale = False
                return httpx.Response(404)
            if self.busy:
                return httpx.Response(409)
            self.prompts.append(json.loads(request.content)["prompt"])
            return httpx.Response(202, json={"run_id": "r1"})
        if request.method == "GET" and path.endswith("/events"):
            payload = "".join(
                "data: " + json.dumps(event) + "\n\n" for event in self.events
            )
            return httpx.Response(200, text=payload, headers={"content-type": "text/event-stream"})
        if request.method == "GET" and path.endswith("/r1"):
            return httpx.Response(200, json={"status": "completed", "answer": "Hallo"})
        if request.method == "POST" and "/approvals/" in path:
            self.decisions.append(json.loads(request.content)["decision"])
            return httpx.Response(200, json={"accepted": True})
        raise AssertionError(f"Unexpected API request: {request.method} {path}")


@pytest.fixture
def setup_pipe(monkeypatch):
    api = FakeApi()
    original = module.httpx.AsyncClient

    def client_factory(**kwargs):
        return original(transport=httpx.MockTransport(api.handle), **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", client_factory)
    pipe = module.Pipe()
    pipe.valves.API_TOKEN = "secret"
    return pipe, api


def body(text="Hi"):
    return {"messages": [{"role": "user", "content": text}]}


@pytest.mark.asyncio
async def test_session_reuse_and_isolation(setup_pipe):
    pipe, api = setup_pipe
    for user, chat, text in [("u1", "c1", "one"), ("u1", "c1", "two"),
                             ("u1", "c2", "three"), ("u2", "c1", "four")]:
        result = await pipe.pipe(body(text), __user__={"id": user},
                                 __metadata__={"chat_id": chat})
        assert result == "Hallo"
    assert len(api.sessions) == 3
    assert api.prompts == ["one", "two", "three", "four"]
    assert api.deleted == []


@pytest.mark.asyncio
async def test_ephemeral_session_cleanup(setup_pipe):
    pipe, api = setup_pipe
    assert await pipe.pipe(body()) == "Hallo"
    assert api.deleted == ["s1"]


@pytest.mark.asyncio
async def test_stale_session_is_recreated(setup_pipe):
    pipe, api = setup_pipe
    args = {"__user__": {"id": "u"}, "__metadata__": {"chat_id": "c"}}
    await pipe.pipe(body(), **args)
    api.stale = True
    await pipe.pipe(body("again"), **args)
    assert len(api.sessions) == 2
    assert pipe._sessions[("u", "c")] == "s2"


@pytest.mark.asyncio
async def test_busy_session_not_retried(setup_pipe):
    pipe, api = setup_pipe
    api.busy = True
    with pytest.raises(httpx.HTTPStatusError) as error:
        await pipe.pipe(body(), __user__={"id": "u"}, __metadata__={"chat_id": "c"})
    assert error.value.response.status_code == 409
    assert len(api.sessions) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmation,blocked,expected", [
    ({"confirmed": True}, False, "yes"),
    ({"confirmed": False}, False, "no"),
    (None, False, "no"),
    ({"confirmed": True}, True, "no"),
])
async def test_approval_fail_closed(setup_pipe, confirmation, blocked, expected):
    pipe, api = setup_pipe
    api.events = [
        {"type": "CUSTOM", "name": "approval_required",
         "value": {"id": "approval1", "tool": "os__write_file",
                   "arguments": {"path": "x"}, "diff_blocked": blocked}},
        {"type": "TEXT_MESSAGE_CONTENT", "delta": "Hallo"},
    ]
    calls = []

    async def confirm(event):
        calls.append(event)
        return confirmation

    await pipe.pipe(body(), __event_call__=confirm)
    assert api.decisions == [expected]
    assert len(calls) == (0 if blocked else 1)
    if calls:
        assert "approval1" not in calls[0]["data"]["message"]


@pytest.mark.asyncio
async def test_tool_status_and_mcp_message(setup_pipe):
    pipe, api = setup_pipe
    api.events = [
        {"type": "TOOL_CALL_START", "toolCallName": "os__read_file"},
        {"type": "CUSTOM", "name": "mcp_user_message",
         "value": {"content": "Reading file"}},
        {"type": "TEXT_MESSAGE_CONTENT", "delta": "Hal"},
        {"type": "TEXT_MESSAGE_CONTENT", "delta": "lo"},
    ]
    statuses = []

    async def emit(event):
        statuses.append(event["data"]["description"])

    assert await pipe.pipe(body(), __event_emitter__=emit) == "Hallo"
    assert "Tool: os__read_file" in statuses
    assert "Reading file" in statuses
    assert statuses[-1] == "Fertig"


@pytest.mark.asyncio
async def test_run_error_is_propagated_and_ephemeral_cleaned(setup_pipe):
    pipe, api = setup_pipe
    api.events = [{"type": "RUN_ERROR", "message": "failed"}]
    with pytest.raises(RuntimeError, match="fehlgeschlagen"):
        await pipe.pipe(body())
    assert api.deleted == ["s1"]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://localhost:8765", "http://example.com:8765",
    "https://127.0.0.1:8765", "http://127.0.0.1:8765/other",
    "http://127.0.0.1:8765/?token=x", "http://user@127.0.0.1:8765",
])
async def test_nonlocal_api_rejected_before_network(setup_pipe, url):
    pipe, api = setup_pipe
    pipe.valves.API_URL = url
    with pytest.raises(ValueError, match="API_URL"):
        await pipe.pipe(body())
    assert not api.sessions


@pytest.mark.asyncio
async def test_missing_token_and_prompt_rejected(setup_pipe):
    pipe, api = setup_pipe
    pipe.valves.API_TOKEN = ""
    with pytest.raises(ValueError, match="API_TOKEN"):
        await pipe.pipe(body())
    pipe.valves.API_TOKEN = "secret"
    with pytest.raises(ValueError, match="Textnachricht"):
        await pipe.pipe({"messages": [{"role": "assistant", "content": "hi"}]})
    assert not api.sessions
