"""Regression coverage for the embedded Web UI transported through AgentApiService."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from cli_agent.agent_api import AgentApiService, create_app
from cli_agent.web_ui_sessions import BrowserSessionRegistry


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
        "registry": BrowserSessionRegistry(service, session.id),
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
        assert (await client.post("/ui/bootstrap", json={})).status_code == 401
        headers = browser_headers()
        auth = {**headers, "Origin": ui["origin"]}
        claimed = (await client.post("/ui/bootstrap", json={}, headers=auth)).json()
        assert claimed["session_id"] == session.id
        assert claimed["lease"]
        headers = {**headers, "X-Cli-Agent-Lease": claimed["lease"]}
        auth = {**headers, "Origin": ui["origin"]}
        assert (await client.post("/ui/bootstrap", json={}, headers=auth)).status_code == 409
        assert (await client.post("/ui/bootstrap", json={}, headers={**auth, "Host": "evil.example"})).status_code == 403
        assert (await client.post("/ui/bootstrap", json={}, headers={**auth, "X-Forwarded-Host": "evil.example"})).status_code == 403
        assert (await client.get("/health", headers=headers)).status_code == 401
        assert (await client.post("/sessions", json={}, headers={**headers, "Origin": ui["origin"]})).status_code == 403
        assert (await client.get("/sessions/not-ours/working-context", headers=headers)).status_code == 403
        assert (await client.get(f"/sessions/{session.id}/working-context", headers=browser_headers())).status_code == 403

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

        for command in ("exit", "QUIT", " Quit "):
            response = await client.post(
                "/ag-ui", json={
                    "threadId": session.id, "runId": "exit-" + command,
                    "protocolVersion": "1.0",
                    "messages": [{"id": "u", "role": "user", "content": command}],
                }, headers={**auth, "Accept": "text/event-stream"},
            )
            assert response.status_code == 400
        assert agents[0].requests == []
        assert stopped == []

        # Proprietary run/approval routes were intentionally removed.
        assert (await client.post(uri + "/runs", json={"prompt": "hi"},
                                  headers=auth)).status_code == 404
        assert (await client.get(uri + "/runs/test/events",
                                 headers=headers)).status_code == 404
        assert (await client.post(uri + "/runs/test/approvals/a",
                                  json={"decision": "yes"},
                                  headers=auth)).status_code == 404

        for invalid in (
            {"command": "add_web_context", "argument": "example.com"},
            {"command": "enable", "argument": "server with spaces"},
            {"command": "tokens", "argument": "extra"},
            {"command": "unknown", "argument": "x"},
            {"command": "add_web_context"},
        ):
            bad = await client.post(
                "/ui/command", json={"session_id": session.id, **invalid},
                headers=auth,
            )
            assert bad.status_code == 400
        assert agents[0].requests == []

        valid = await client.post("/ui/command", json={
            "session_id": session.id, "command": "tokens",
        }, headers=auth)
        assert valid.status_code == 200
        assert valid.json()["answer"] == "Antwort: tokens"

        result = await client.post("/ag-ui", json={
            "threadId": session.id, "runId": "browser-run",
            "protocolVersion": "1.0",
            "messages": [{"id": "u", "role": "user", "content": "hi"}],
        }, headers={**auth, "Accept": "text/event-stream"})
        assert result.status_code == 200
        assert "RUN_FINISHED" in result.text
        assert "Antwort: hi" in result.text
        assert agents[0].requests == ["tokens", "hi"]

        assert (await client.post("/ui/render", json={"text": "**safe**"},
            headers={**headers, "Origin": ui["origin"]})).status_code == 200
        # A browser must not be able to shut down the entire Python process.
        assert (await client.post("/ui/shutdown", json={},
            headers={**headers, "Origin": ui["origin"]})).status_code == 404
        assert stopped == []
        assert session.id in service.sessions
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


def test_bootstrap_preserves_initial_context_diagnostics():
    """The web UI must not clear initial context-loading errors on bootstrap."""
    from cli_agent.web_ui_api import API_APP_JS

    bootstrap = API_APP_JS.split("async function bootstrap() {", 1)[1]
    # Initial messages are displayed by the session-event handler; the
    # transcript should be cleared *before* emitting that event.
    assert bootstrap.index("chat.replaceChildren();") < bootstrap.index(
        'emit({type: "session", ...response});'
    )
    assert 'for (const message of payload.initial_messages || [])' in API_APP_JS
    assert "function dismissApproval()" in API_APP_JS
    assert "event.type === \"RUN_FINISHED\"" in API_APP_JS


def test_browser_rendering_failures_fall_back_to_plain_text():
    """A failed optional Markdown request must never hide the model answer."""
    from cli_agent.web_ui_api import API_APP_JS

    assert 'async function renderAnswer(text)' in API_APP_JS
    assert 'catch (_error) {' in API_APP_JS.split(
        "async function renderAnswer(text)", 1
    )[1].split("async function watchRun", 1)[0]
    assert 'emit({type: "answer", content: answer, html: await renderAnswer(answer)});' in API_APP_JS
    assert 'addMessage("assistant", content, await renderAnswer(content));' in API_APP_JS
    assert 'element.textContent = content;' in API_APP_JS


def test_local_exit_and_quit_are_not_sent_to_agent():
    from cli_agent.web_ui_api import API_APP_JS

    dispatch = API_APP_JS.split("async function dispatchAction(msg)", 1)[1]
    assert '["exit", "quit"].includes(msg.content.trim().toLowerCase())' in dispatch
    assert dispatch.index('await deleteUnusedSession(sessionId);') < dispatch.index(
        'api("/ag-ui", "POST", input)'
    )
    assert 'api("/ui/shutdown"' not in API_APP_JS
    assert 'quit.textContent = "Session beenden";' in API_APP_JS
    assert 'quit.disabled = busy || disconnected;' in API_APP_JS
    assert 'void deleteUnusedSession(sessionId).catch(showApiError);' in API_APP_JS


def test_browser_buttons_submit_structured_commands():
    from cli_agent.web_ui_api import API_APP_JS

    dispatch = API_APP_JS.split("async function dispatchAction(msg)", 1)[1]
    assert 'msg.type === "command"' in dispatch
    assert 'api("/ui/command", "POST", {' in dispatch
    assert 'api("/ag-ui", "POST", input)' in dispatch
    # Do not assemble unchecked commands into ordinary prompt strings.
    assert 'promptText = msg.command +' not in dispatch


def test_browser_agui_transcript_compaction_with_real_javascript():
    """Drive the embedded JS compactor, not a Python reimplementation."""
    import json
    import shutil
    import subprocess

    from cli_agent.web_ui_api import API_APP_JS

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed for embedded browser JS assertions.")
    start = API_APP_JS.index("  const MAX_SYNCED_MESSAGES")
    stop = API_APP_JS.index("  async function dispatchAction(msg)", start)
    logic = API_APP_JS[start:stop]
    script = logic + """
    const makeInput = messages => ({
      threadId: "thread", runId: "run", protocolVersion: "1.0", messages
    });
    const messages = [];
    for (let n = 0; n < 300; n++) {
      messages.push({id: "u" + n, role: "user", content: "question"});
      messages.push({id: "a" + n, role: "assistant", content: "answer"});
    }
    const bounded = compactAguiHistory(makeInput(messages));
    if (bounded.length > 480 || bounded.length <= 0)
      throw new Error("Message bound ineffective");
    if (bounded[0].role !== "user" ||
        bounded[bounded.length - 1].id !== "a299")
      throw new Error("Turn boundaries or latest response lost");

    const big = [];
    for (let n = 0; n < 24; n++) {
      big.push({id: "u" + n, role: "user", content: "Ü".repeat(25000)});
      big.push({id: "a" + n, role: "assistant", content: "🎯".repeat(8000)});
    }
    const byteBounded = compactAguiHistory(makeInput(big));
    const bytes = new TextEncoder().encode(JSON.stringify(makeInput(byteBounded))).length;
    if (bytes > 800000) throw new Error("UTF-8 payload limit ineffective");
    if (byteBounded.at(-1).id !== "a23")
      throw new Error("Latest response lost to byte limit");
    if (byteBounded[0].role !== "user")
      throw new Error("Oversized trim breaks conversation turns");

    const last = makeInput([{id: "latest", role: "user", content: "x".repeat(810000)}]);
    let rejected = false;
    try { compactAguiHistory(last); }
    catch (err) { rejected = String(err.message).includes("zu groß"); }
    if (!rejected) throw new Error("Overlarge latest turn must be rejected");

    const resume = makeInput([
      {id: "old", role: "user", content: "x".repeat(550000)},
      {id: "old-answer", role: "assistant", content: "ok"},
      {id: "current", role: "user", content: "approve this"},
      {id: "pending", role: "assistant", content: "",
       toolCalls: [{id:"tool1", type:"function",
                    function:{name:"show", arguments:"{}"}}]},
      {id: "result", role: "tool", toolCallId: "tool1", content: "done"},
    ]);
    const compactedResume = compactAguiHistory(resume);
    if (compactedResume.at(-1).id !== "result")
      throw new Error("Latest tool result was removed");
    console.log("ok");
    """
    result = subprocess.run(
        [node, "-e", script], capture_output=True, text=True, timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_browser_agui_messages_are_trimmed_before_post_and_restored_after_rejection():
    from cli_agent.web_ui_api import API_APP_JS

    assert "result.messages = compactAguiHistory(result);" in API_APP_JS
    dispatch = API_APP_JS.split('if (msg.type === "message") {', 1)[1]
    assert "input = aguiInput();" in dispatch
    assert "aguiMessages = input.messages;" in dispatch
    assert "aguiMessages.pop();" in dispatch
    assert 'const MAX_SYNCED_MESSAGES = 480;' in API_APP_JS
    assert 'const MAX_SYNCED_REQUEST_BYTES = 800000;' in API_APP_JS


def test_browser_wire_transcript_is_bounded_without_truncating_latest_prompt():
    from cli_agent.web_ui_api import API_APP_JS

    bound = API_APP_JS.split("function boundAguiMessages() {", 1)[1].split(
        "function aguiInput(", 1
    )[0]
    assert "MAX_WIRE_MESSAGES = 240" in API_APP_JS
    assert "MAX_WIRE_BYTES = 400 * 1024" in API_APP_JS
    assert "new TextEncoder()" in bound
    assert "aguiMessages.length - MAX_WIRE_MESSAGES" in bound
    assert "aguiMessages = aguiMessages.slice(start)" in bound
    assert "if (start >= aguiMessages.length)" in bound
    assert "aguiMessages[0].role !== \"user\"" in bound
    assert "messages: boundAguiMessages()" in API_APP_JS
    assert "chat.replaceChildren();" in API_APP_JS
    # The browser display is not truncated when the AG-UI wire history is.
    assert "chat.replaceChildren()" not in bound
