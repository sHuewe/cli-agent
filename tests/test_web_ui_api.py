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
        [node, "-e", script], capture_output=True, text=True, timeout=60,
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


def test_cancelled_exit_confirmation_keeps_composer_interactive():
    from cli_agent.web_ui_api import API_APP_JS

    form = API_APP_JS.split('form.addEventListener("submit",', 1)[1].split(
        'prompt.addEventListener("keydown"', 1
    )[0]
    assert '["exit", "quit"].includes(content.toLowerCase())' in form
    assert form.index('void deleteUnusedSession(sessionId)') < form.index(
        'addMessage("user", content)'
    )
    assert form.index('void deleteUnusedSession(sessionId)') < form.index(
        'setBusy(true)'
    )
    dispatch = API_APP_JS.split('async function dispatchAction(msg)', 1)[1]
    assert 'if (sessionId) setBusy(false);' in dispatch


def test_paused_approval_sessions_are_actionable_in_sessions_dialog():
    from cli_agent.web_ui_api import API_APP_JS

    rendered = API_APP_JS.split('async function refreshSessions()', 1)[1].split(
        'let heartbeat = null;', 1
    )[0]
    assert 'const executing = item.busy && !item.approval_pending;' in rendered
    assert 'open.disabled = item.occupied || executing;' in rendered
    assert 'close.disabled = (item.occupied && !current) || executing;' in rendered


@pytest.mark.asyncio
@pytest.mark.parametrize("os_access", ["read", "write"])
async def test_cli_embedded_ui_startup_configuration_and_cleanup(
    monkeypatch, tmp_path, os_access,
):
    """Exercise the real --with-web-ui startup path without a networked LLM."""
    import sys
    import types

    from cli_agent import cli, file_context, model_factory, os_operations, web_ui_api
    from cli_agent import agent_api, logging_setup

    monkeypatch.setattr(logging_setup, "configure_logging", lambda _config: None)

    created = []
    shutdowns = []
    opened = []
    previews = []
    model_calls = []
    preapprovals = []
    server_configs = []
    server_stopped = asyncio.Event()
    loop = asyncio.get_running_loop()

    class FakeSession:
        id = "initial-browser-session"
        async def approve(self, *_args):
            return True
        async def on_agent_event(self, *_args):
            return None
        async def _send_user_message(self, *_args):
            return None
        class Broker:
            async def show_mcp_user_message(self, *_args):
                return None
            def set_event_sender(self, sender):
                self.sender = sender
        def __init__(self):
            self.broker = self.Broker()

    class FakeAgent:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            created.append(self)

    class FakeService:
        def __init__(self, factory, **kwargs):
            self.factory = factory
            self.options = kwargs
            self.agent_session = FakeSession()
            self.agent = factory(self.agent_session)
        async def create(self):
            return self.agent_session
        async def shutdown(self):
            shutdowns.append(True)

    class FakeServer:
        def __init__(self, config):
            self.started = False
            self.should_exit = False
            self.config = config
        async def serve(self, *, sockets):
            assert len(sockets) == 1
            assert sockets[0].getsockname()[0] == "127.0.0.1"
            self.started = True
            await asyncio.wait_for(server_stopped.wait(), timeout=3)

    fake_uvicorn = types.SimpleNamespace(
        Config=lambda app, **options: server_configs.append((app, options)) or
        SimpleNamespace(app=app, **options),
        Server=FakeServer,
    )

    fake_model = object()
    monkeypatch.setitem(sys.modules, "uvicorn", fake_uvicorn)
    monkeypatch.setattr(agent_api, "AgentApiService", FakeService)
    monkeypatch.setattr(agent_api, "create_app", lambda svc, *, ui: ui)
    monkeypatch.setattr(file_context, "ContextFileCliAgent", FakeAgent)
    monkeypatch.setattr(model_factory, "create_model_client",
                        lambda *a, **kw: model_calls.append((a, kw)) or fake_model)
    monkeypatch.setattr(cli, "build_preapproval_callback",
                        lambda allowed, *, fallback: preapprovals.append(
                            (allowed, fallback)
                        ) or fallback)
    monkeypatch.setattr(web_ui_api.webbrowser if hasattr(web_ui_api, "webbrowser")
                        else __import__("webbrowser"), "open",
                        lambda url, **kwargs: (opened.append(url),
                                               loop.call_soon_threadsafe(server_stopped.set)))
    monkeypatch.setattr(
        os_operations.Workspace, "from_directory",
        lambda *a, **kw: previews.append((a, kw)) or object(),
    )
    args = SimpleNamespace(
        config=tmp_path / "cli-agent.toml",
        os_access=os_access, approve_tool=("safe__read",),
        debug=True, add_web_context=("https://example.org",),
        response_format="json",
    )
    cfg = SimpleNamespace(
        logging=object(),
        agent=SimpleNamespace(timezone="Europe/Berlin"),
        model=SimpleNamespace(model="fake-model"),
        mcp_servers=(SimpleNamespace(name="os"),),
        okf=None, knowledge_vector=None,
    )
    admin = SimpleNamespace(
        network=object(), model_credentials=(),
        web=SimpleNamespace(providers=()), mcp=object(),
    )
    await web_ui_api.run_web_ui_via_api(
        args=args, config=cfg, admin_config=admin,
        workspace=tmp_path, file_contexts=(),
        output_target=None, excluded_paths=(tmp_path / "secret",),
    )
    assert shutdowns == [True]
    assert len(created) == 1
    assert created[0].args[1] is fake_model
    assert created[0].kwargs["workspace_access"] == os_access
    assert created[0].kwargs["response_format"] == "json"
    assert created[0].kwargs["agent_timezone"] == "Europe/Berlin"
    assert created[0].event_callback == preapprovals[0][1].__self__.on_agent_event
    assert preapprovals[0][0] == ("safe__read",)
    assert model_calls
    assert len(previews) == (1 if os_access == "write" else 0)
    assert opened and opened[0].startswith("http://127.0.0.1:")
    assert "#token=" in opened[0]
    assert server_configs[0][1]["host"] == "127.0.0.1"
    assert server_configs[0][1]["proxy_headers"] is False


def test_api_backed_browser_javascript_syntax():
    """Validate the actual browser script; Python coverage cannot inspect it."""
    import shutil
    import subprocess

    from cli_agent.web_ui_api import API_APP_JS

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    result = subprocess.run(
        [node, "--check", "--input-type=commonjs"],
        input=API_APP_JS, text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr


def test_browser_tool_calls_display_all_statuses_and_survive_approval_resume():
    """Run the actual SSE parser and collapsible tool view in Node.js."""
    import shutil
    import subprocess

    from cli_agent.web_ui_api import API_APP_JS

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    view = API_APP_JS.split("  function toolGroup() {", 1)[1].split(
        "  function setBusy(value) {", 1
    )[0]
    watcher = API_APP_JS.split("  async function watchRun(activeRun, response = null) {", 1)[1].split(
        "  // The server accepts at most", 1
    )[0]
    script = r'''
class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.textContent = "";
  }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); }
}
const document = {createElement: tag => new Element(tag)};
const chat = new Element("chat");
let activeToolGroup = null;
let runId = "run-1";
const aguiMessages = [];
const emitted = [];
const emit = event => emitted.push(event);
const renderAnswer = async () => null;
const dismissApproval = () => {};
const api = () => { throw new Error("unexpected API request"); };
const crypto = {randomUUID: () => "new-message"};
''' + '  function toolGroup() {' + view + (
        '  async function watchRun(activeRun, response = null) {' + watcher
    ) + r'''
function stream(events) {
  const bytes = new TextEncoder().encode(
    events.map(event => "data: " + JSON.stringify(event) + "\n\n").join("")
  );
  let delivered = false;
  return {body: {getReader: () => ({
    read: async () => {
      if (delivered) return {done: true};
      delivered = true;
      return {done: false, value: bytes};
    },
    releaseLock: () => {}
  })}};
}
function assert(condition, message) {
  if (!condition) throw new Error(message);
}
(async () => {
  const unsafeName = '<img src=x onerror=alert(1)>';
  await watchRun("run-1", stream([
    {type: "TOOL_CALL_START", toolCallId: "a", toolCallName: unsafeName},
    {type: "TOOL_CALL_START", toolCallId: "a", toolCallName: unsafeName},
    {type: "TOOL_CALL_START", toolCallId: "b", toolCallName: "os__write_file"},
    {type: "TOOL_CALL_ARGS", toolCallId: "b", delta: "private-arguments"},
    {type: "TOOL_CALL_END", toolCallId: "a"},
    {type: "TOOL_CALL_RESULT", toolCallId: "a", content: "success"},
    {type: "RUN_FINISHED", outcome: {type: "interrupt", interrupts: [
      {id: "approval-1", toolCallId: "b", message: "Freigabe nötig"}
    ]}}
  ]));
  assert(chat.children.length === 1, "expected one collapsible group");
  assert(chat.children[0].tag === "details" && chat.children[0].open,
    "tool calls are not expandable");
  assert(activeToolGroup.calls.size === 2, "duplicate call counted twice");
  assert(activeToolGroup.summary.textContent === "Tool-Aufrufe (2)", "wrong count");
  assert(activeToolGroup.list.children[0].children[0].textContent === unsafeName,
    "tool names must be inserted as textContent");
  assert(activeToolGroup.calls.get("a").textContent === "Erfolgreich", "missing success");
  assert(activeToolGroup.calls.get("b").textContent === "Freigabe nötig", "missing approval");
  assert(emitted.some(x => x.type === "approval_required" && x.toolCallId === "b"),
    "approval event lost tool call ID");
  assert(!JSON.stringify(chat).includes("private-arguments"), "tool arguments leaked into UI");

  await watchRun("run-2", stream([
    {type: "TOOL_CALL_RESULT", toolCallId: "b", content: "denied"},
    {type: "RUN_FINISHED"}
  ]));
  assert(activeToolGroup.calls.get("b").textContent === "Abgelehnt", "missing denial");
  assert(chat.children.length === 1, "approval continuation created a second group");

  activeToolGroup = null;  // A new user turn.
  await watchRun("run-3", stream([
    {type: "TOOL_CALL_START", toolCallId: "c", toolCallName: "external__search"},
    {type: "TOOL_CALL_RESULT", toolCallId: "c", content: "error"},
    {type: "TOOL_CALL_START", toolCallId: "d", toolCallName: "external__other"},
    {type: "TOOL_CALL_RESULT", toolCallId: "d", content: "private-tool-output"},
    {type: "RUN_FINISHED"}
  ]));
  assert(chat.children.length === 2, "new turn reused previous group");
  assert(activeToolGroup.calls.get("c").textContent === "Fehler", "missing error");
  assert(activeToolGroup.calls.get("d").textContent === "Beendet", "unknown status leaked");
  assert(!JSON.stringify(chat).includes("private-tool-output"), "tool result leaked into UI");
  console.log("ok");
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_browser_published_document_uses_authenticated_download_and_rejects_unsafe_urls():
    """Exercise document ContentParts and the download button in actual Node.js."""
    import shutil
    import subprocess
    from cli_agent.web_ui_api import API_APP_JS

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    view = API_APP_JS.split("  function toolGroup() {", 1)[1].split(
        "  function setBusy(value) {", 1
    )[0]
    watcher = API_APP_JS.split("  async function watchRun(activeRun, response = null) {", 1)[1].split(
        "  // The server accepts at most", 1
    )[0]
    script = r'''
class Element {
  constructor(tag) {
    this.tag = tag;
    this.children = [];
    this.dataset = {};
    this.textContent = "";
    this.parentElement = null;
    this.listeners = {};
  }
  append(...children) {
    for (const child of children) {
      this.children.push(child);
      child.parentElement = this;
    }
  }
  appendChild(child) { this.append(child); }
  addEventListener(type, cb) { this.listeners[type] = cb; }
  click() { this.clicked = true; }
  remove() {}
  scrollIntoView() {}
}
const document = {createElement: tag => new Element(tag), body: new Element("body")};
const chat = new Element("chat");
let activeToolGroup = null;
let runId = "run-1";
const aguiMessages = [];
const emitted = [];
const emit = event => emitted.push(event);
const renderAnswer = async () => null;
const dismissApproval = () => {};
const crypto = {randomUUID: () => "new-message"};
let requested = [];
const api = async url => {
  requested.push(url);
  return {blob: async () => new Blob(["example"])};
};
let revoked = [];
const URL = {createObjectURL: () => "blob:local", revokeObjectURL: u => revoked.push(u)};
const setTimeout = cb => { cb(); };
function assert(condition, message) { if (!condition) throw Error(message); }
''' + '  function toolGroup() {' + view + (
        '  async function watchRun(activeRun, response = null) {' + watcher
    ) + r'''
function stream(events) {
  const bytes = new TextEncoder().encode(
    events.map(event => "data: " + JSON.stringify(event) + "\n\n").join("")
  );
  let delivered = false;
  return {body: {getReader: () => ({
    read: async () => delivered ? {done: true} : (delivered = true, {done: false, value: bytes}),
    releaseLock: () => {}
  })}};
}
(async () => {
  const url = "/threads/~dGhyZWFk/artifacts/" + "a".repeat(32);
  const documentPart = {
    type: "document", source: {type: "url", value: url, mimeType: "text/csv"},
    metadata: {filename: "report.csv"}
  };
  const events = [
    {type: "TOOL_CALL_START", toolCallId: "publish", toolCallName: "agent__publish_artifact"},
    {type: "TOOL_CALL_RESULT", toolCallId: "publish", content: [
      {type: "text", text: "Datei zum Download bereit."}, documentPart
    ]},
    {type: "TOOL_CALL_RESULT", toolCallId: "publish", content: [documentPart]},
    {type: "RUN_FINISHED"}
  ];
  await watchRun("run-1", stream(events));
  const row = activeToolGroup.list.children[0];
  assert(row.children.length === 3, "missing or duplicate download button");
  const button = row.children[2];
  assert(button.textContent === "Herunterladen: report.csv", "wrong filename");
  assert(activeToolGroup.calls.get("publish").dataset.state === "success", "wrong status");
  await button.listeners.click();
  assert(requested.length === 1 && requested[0] === url, "download bypassed API");
  assert(document.body.children[0].download === "report.csv", "filename lost");
  assert(revoked[0] === "blob:local", "object URL not revoked");

  activeToolGroup = null;
  await watchRun("run-2", stream([
    {type: "TOOL_CALL_START", toolCallId: "unsafe", toolCallName: "agent__publish_artifact"},
    {type: "TOOL_CALL_RESULT", toolCallId: "unsafe", content: [
      {type: "document", source: {type: "url", value: "https://evil.invalid/file"},
       metadata: {filename: "report.csv"}},
      {type: "document", source: {type: "url", value: url},
       metadata: {filename: "../secret"}}
    ]},
    {type: "RUN_FINISHED"}
  ]));
  assert(activeToolGroup.list.children[0].children.length === 2, "unsafe download exposed");
  assert(activeToolGroup.calls.get("unsafe").dataset.state === "finished", "unsafe result status");
  console.log("ok");
})().catch(error => {console.error(error); process.exitCode = 1;});
'''
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_cancelled_session_close_does_not_issue_delete_request():
    """Exercise the browser's real confirmation handler in Node.js."""
    import shutil
    import subprocess

    from cli_agent.web_ui_api import API_APP_JS

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    start = API_APP_JS.index("  async function deleteUnusedSession(sid) {")
    end = API_APP_JS.index("  async function refreshSessions()", start)
    snippet = API_APP_JS[start:end]
    script = (
        'const window = {confirm: () => false};\n'
        'let calls = 0;\n'
        'async function api() { calls++; throw new Error("unexpected request"); }\n'
        + snippet +
        '\n(async () => {\n'
        ' await deleteUnusedSession("active-session");\n'
        ' if (calls !== 0) throw new Error("cancel attempted a request");\n'
        ' console.log("ok");\n'
        '})().catch(error => { console.error(error); process.exitCode = 1; });\n'
    )
    result = subprocess.run(
        [node, "-e", script], text=True, capture_output=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_approval_resume_rejection_preserves_dialog_and_allows_retry():
    """Run the real approval branch in Node: HTTP 429 must keep approval actionable."""
    import shutil
    import subprocess

    from cli_agent.web_ui_api import API_APP_JS

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    dispatch = API_APP_JS.split('    if (msg.type === "approval_response") {', 1)[1].split(
        '\n  }\n\n  function processServerMessage', 1
    )[0]
    branch = '    if (msg.type === "approval_response") {' + dispatch
    view = API_APP_JS.split("  function toolGroup() {", 1)[1].split(
        "  function setBusy(value) {", 1
    )[0]
    script = '''
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.dataset = {}; this.textContent = ""; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); }
}
const document = {createElement: tag => new Element(tag)};
const chat = new Element("chat");
let activeToolGroup = null;
let pendingApprovalId = "approval-1";
let pendingApprovalToolCallId = "tool-1";
let approvalSubmitting = false;
let sessionId = "session-1";
let aguiMessages = [];
let runId = null;
let busy = true;
let attempts = 0;
let closed = 0;
const buttons = [{disabled: false}, {disabled: false}];
const approval = {querySelectorAll: () => buttons};
const errors = [];
const addMessage = (_type, text) => errors.push(text);
const setBusy = value => { busy = value; };
const aguiInput = resume => ({messages: [], runId: "next-run", resume});
const api = async () => {
  attempts++;
  if (attempts === 1) throw new Error("HTTP 429");
  return {body: "accepted"};
};
const dismissApproval = () => {
  pendingApprovalId = null;
  pendingApprovalToolCallId = null;
  closed++;
};
const statusesAtWatch = [];
let streamFailure = false;
const watchRun = async () => {
  statusesAtWatch.push(activeToolGroup.calls.get("tool-1").dataset.state);
  if (streamFailure) finishPendingTools("error");
};
''' + '  function toolGroup() {' + view + '''
async function action(msg) {
''' + branch + '''
}
(async () => {
  toolStarted({toolCallId: "tool-1", toolCallName: "write"});
  setToolStatus("tool-1", "approval");
  await action({type:"approval_response",id:"approval-1",decision:"yes"});
  if (pendingApprovalId !== "approval-1" || !busy || closed || attempts !== 1)
    throw new Error("Rejected resume lost its interrupt");
  if (activeToolGroup.calls.get("tool-1").dataset.state !== "approval")
    throw new Error("Rejected resume changed the approval status");
  if (buttons.some(x => x.disabled)) throw new Error("Retry controls disabled");
  await action({type:"approval_response",id:"approval-1",decision:"no"});
  if (pendingApprovalId !== null || closed !== 1 || attempts !== 2)
    throw new Error("Retry did not complete");
  if (statusesAtWatch[0] !== "denied")
    throw new Error("Accepted denial left approval pending");
  if (errors.length !== 1 || !errors[0].includes("429"))
    throw new Error("Rejection not reported");
  // A second interrupt resumes without another TOOL_CALL_START. Its row
  // must become running before streaming so a stream error can finish it.
  pendingApprovalId = "approval-2";
  pendingApprovalToolCallId = "tool-1";
  setToolStatus("tool-1", "approval");
  streamFailure = true;
  await action({type:"approval_response",id:"approval-2",decision:"session"});
  if (statusesAtWatch[1] !== "running")
    throw new Error("Accepted approval did not restart the tool status");
  if (activeToolGroup.calls.get("tool-1").dataset.state !== "error")
    throw new Error("Stream failure left the resumed tool awaiting approval");
  console.log("ok");
})().catch(err => {console.error(err); process.exitCode=1;});
'''
    result = subprocess.run([node, "-e", script], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_approval_cancel_and_click_keep_interrupt_until_response():
    from cli_agent.web_ui_api import API_APP_JS

    handlers = API_APP_JS.split('approval.addEventListener("click"', 1)[1].split(
        'quit.addEventListener("click"', 1
    )[0]
    assert 'pendingApprovalId = null;' not in handlers
    assert 'approval.close();' not in handlers
    assert 'event.preventDefault();' in handlers
    assert 'approvalSubmitting' in handlers
