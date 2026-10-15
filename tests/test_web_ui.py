from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent import web_ui
from cli_agent.cli import build_parser


def test_web_ui_flag_is_optional_and_defaults_off() -> None:
    assert build_parser().parse_args([]).with_web_ui is False
    assert build_parser().parse_args(["--with-web-ui"]).with_web_ui is True


def test_web_ui_host_is_fixed_to_ipv4_loopback() -> None:
    assert web_ui.WEB_UI_HOST == "127.0.0.1"


def test_web_ui_approval_fails_closed_without_browser() -> None:
    broker = web_ui.WebUiApprovalBroker()
    result = asyncio.run(
        broker.approve_tool_call("os__write_file", {"path": "test.txt"})
    )
    assert result is False


@pytest.mark.parametrize(
    ("decision", "expected"),
    [("yes", True), ("no", False), ("session", "session")],
)
def test_web_ui_approval_round_trip(decision, expected) -> None:
    async def run():
        broker = web_ui.WebUiApprovalBroker()
        sent = []

        async def sender(payload):
            sent.append(payload)

        broker.attach(sender)
        task = asyncio.create_task(
            broker.approve_tool_call(
                "os__write_file",
                {"path": "test.txt", "content": "hello"},
            )
        )
        while not sent:
            await asyncio.sleep(0)

        request = sent[0]
        assert request["type"] == "approval_required"
        assert request["tool"] == "os__write_file"
        assert "test.txt" in request["arguments"]
        assert broker.resolve(request["id"], decision) is True
        return await task

    assert asyncio.run(run()) == expected


def test_web_ui_disconnect_denies_pending_approval() -> None:
    async def run():
        broker = web_ui.WebUiApprovalBroker()
        sent = []

        async def sender(payload):
            sent.append(payload)

        broker.attach(sender)
        task = asyncio.create_task(
            broker.approve_tool_call("external__write", {"value": "x"})
        )
        while not sent:
            await asyncio.sleep(0)
        broker.detach(sender)
        return await task

    assert asyncio.run(run()) is False


def test_web_ui_rejects_wrong_token_or_origin() -> None:
    broker = web_ui.WebUiApprovalBroker()
    session = web_ui._WebUiSession(
        agent=SimpleNamespace(),
        approval_broker=broker,
        token="secret",
        expected_origin="http://127.0.0.1:12345",
        workspace=Path("."),
        model="model",
        mcp_servers=(),
        output_target=None,
        initial_messages=(),
        debug=False,
    )

    valid = SimpleNamespace(
        query_params={"token": "secret"},
        headers={"origin": "http://127.0.0.1:12345"},
    )
    wrong_token = SimpleNamespace(
        query_params={"token": "wrong"},
        headers={"origin": "http://127.0.0.1:12345"},
    )
    wrong_origin = SimpleNamespace(
        query_params={"token": "secret"},
        headers={"origin": "http://evil.example"},
    )

    assert session._authorized(valid) is True
    assert session._authorized(wrong_token) is False
    assert session._authorized(wrong_origin) is False


def test_missing_web_extra_has_actionable_error(monkeypatch) -> None:
    real_import = web_ui.importlib.import_module

    def fake_import(name):
        if name == "starlette.applications":
            exc = ModuleNotFoundError("No module named 'starlette'")
            exc.name = "starlette"
            raise exc
        return real_import(name)

    monkeypatch.setattr(web_ui.importlib, "import_module", fake_import)

    with pytest.raises(RuntimeError, match=r"cli-agent\[web\]"):
        web_ui._load_web_dependencies()


def test_web_ui_oversized_prompt_releases_busy_state() -> None:
    source = web_ui._WebUiSession.websocket.__code__
    assert source is not None
    assert "busy" in web_ui.APP_JS


def test_web_ui_answer_is_sent_before_output_write() -> None:
    class Agent:
        async def ask(self, _prompt):
            return "successful answer"

    class FailingOutput:
        def write_text(self, _text):
            raise OSError("write failed")

    async def run():
        broker = web_ui.WebUiApprovalBroker()
        session = web_ui._WebUiSession(
            agent=Agent(),
            approval_broker=broker,
            token="secret",
            expected_origin="http://127.0.0.1:12345",
            workspace=Path("."),
            model="model",
            mcp_servers=(),
            output_target=FailingOutput(),
            initial_messages=(),
            debug=False,
        )
        sent = []

        async def sender(payload):
            sent.append(payload)

        session._active_sender = sender
        await session._handle_prompt("hello")
        return sent

    sent = asyncio.run(run())
    assert sent[0] == {
        "type": "answer",
        "content": "successful answer",
        "html": "<p>successful answer</p>\n",
    }
    assert sent[1]["type"] == "error"
    assert "write failed" in sent[1]["content"]
    assert sent[-1] == {"type": "busy", "value": False}



def test_web_ui_markdown_renders_emphasis_lists_code_tables_and_escapes_html() -> None:
    rendered = web_ui._render_markdown(
        "# Title\n\n"
        "**bold** and *italic*\n\n"
        "* first\n"
        "* second\n\n"
        "`inline`\n\n"
        "| A | B |\n"
        "|---|---|\n"
        "| 1 | 2 |\n\n"
        "<script>alert(\'x\')</script>"
    )

    assert "<h1>Title</h1>" in rendered
    assert "<strong>bold</strong>" in rendered
    assert "<em>italic</em>" in rendered
    assert "<ul>" in rendered
    assert "<li>first</li>" in rendered
    assert "<li>second</li>" in rendered
    assert "<code>inline</code>" in rendered
    assert "<table>" in rendered
    assert "&lt;script&gt;" in rendered
    assert "<script>" not in rendered


def test_web_ui_only_uses_rendered_html_for_assistant_messages() -> None:
    assert 'kind === "assistant" && typeof renderedHtml === "string"' in web_ui.APP_JS
    assert "element.innerHTML = renderedHtml" in web_ui.APP_JS
    assert "element.textContent = content" in web_ui.APP_JS


def test_web_ui_connection_lock_only_guards_active_sender_state() -> None:
    import inspect

    source = inspect.getsource(web_ui._WebUiSession.websocket)
    guarded = source.split("async with self._connection_lock:", 1)[1].split(
        "await websocket.accept()", 1
    )[0]
    assert "self._active_sender = sender" in guarded
    assert "while True:" not in guarded


def test_web_ui_concurrent_prompt_rejection_releases_browser_busy_state() -> None:
    import inspect

    source = inspect.getsource(web_ui._WebUiSession.websocket)
    command_section = source.split('if kind == "command":', 1)[1].split(
        'if kind != "message":', 1
    )[0]
    concurrent = command_section.split("if self._busy():", 1)[1].split(
        "continue", 1
    )[0]
    assert 'await sender({"type": "busy", "value": False})' in concurrent


def test_web_ui_prompt_result_is_buffered_across_disconnect() -> None:
    class Agent:
        async def ask(self, _prompt):
            await asyncio.sleep(0)
            return "answer after reconnect"

    async def run():
        broker = web_ui.WebUiApprovalBroker()
        session = web_ui._WebUiSession(
            agent=Agent(),
            approval_broker=broker,
            token="secret",
            expected_origin="http://127.0.0.1:12345",
            workspace=Path("."),
            model="model",
            mcp_servers=(),
            output_target=None,
            initial_messages=(),
            debug=False,
        )

        async def stale_sender(_payload):
            raise RuntimeError("disconnected")

        session._active_sender = stale_sender
        await session._handle_prompt("hello")

        delivered = []

        async def new_sender(payload):
            delivered.append(payload)

        session._active_sender = new_sender
        await session._flush_pending_events()
        return delivered

    delivered = asyncio.run(run())
    assert delivered == [
        {
            "type": "answer",
            "content": "answer after reconnect",
            "html": "<p>answer after reconnect</p>\n",
        },
        {"type": "busy", "value": False},
    ]


@pytest.mark.parametrize(
    ("command", "argument", "expected"),
    [
        ("tokens", None, "tokens"),
        ("clear_web_context", None, "clear_web_context"),
        ("add_web_context", "https://example.com", "add_web_context https://example.com"),
        ("enable", "os", "enable os"),
        ("disable", "os", "disable os"),
    ],
)
def test_web_ui_command_prompt_builds_only_local_commands(
    command,
    argument,
    expected,
) -> None:
    assert web_ui._web_ui_command_prompt(command, argument) == expected


@pytest.mark.parametrize(
    ("command", "argument"),
    [
        ("unknown", None),
        ("tokens", "unexpected"),
        ("add_web_context", None),
        ("add_web_context", "not-a-url"),
        ("enable", None),
        ("disable", ""),
    ],
)
def test_web_ui_command_prompt_rejects_invalid_commands(command, argument) -> None:
    with pytest.raises(ValueError):
        web_ui._web_ui_command_prompt(command, argument)


def test_web_ui_renders_local_command_buttons() -> None:
    for command in (
        "tokens",
        "add_web_context",
        "clear_web_context",
        "enable",
        "disable",
    ):
        assert f'data-command="{command}"' in web_ui.INDEX_HTML


def test_web_ui_prompt_queue_keeps_agent_execution_out_of_websocket_task() -> None:
    import inspect

    source = inspect.getsource(web_ui._WebUiSession.websocket)
    assert "asyncio.create_task(" not in source
    assert "self._enqueue_prompt(" in source


def test_web_ui_queue_marks_request_busy_until_processed() -> None:
    async def run():
        broker = web_ui.WebUiApprovalBroker()
        session = web_ui._WebUiSession(
            agent=SimpleNamespace(),
            approval_broker=broker,
            token="secret",
            expected_origin="http://127.0.0.1:12345",
            workspace=Path("."),
            model="model",
            mcp_servers=(),
            output_target=None,
            initial_messages=(),
            debug=False,
        )
        assert session._busy() is False
        session._enqueue_prompt("tokens")
        assert session._busy() is True
        assert await session.next_prompt() == "tokens"

    asyncio.run(run())


def test_web_ui_client_retries_transient_connection_conflict() -> None:
    assert "event.code === 4409" in web_ui.APP_JS
    assert "window.setTimeout(connectSocket, reconnectDelayMs)" in web_ui.APP_JS
    assert "maxReconnectAttempts" in web_ui.APP_JS


def test_web_ui_header_is_sticky() -> None:
    assert "header { position: sticky; top: 0;" in web_ui.APP_CSS


def test_web_ui_renders_working_context_tabs() -> None:
    assert 'id="show-working-context"' in web_ui.INDEX_HTML
    assert '>LLM Context</button>' in web_ui.INDEX_HTML
    assert 'id="working-context-dialog"' in web_ui.INDEX_HTML
    assert 'id="context-tab-messages"' in web_ui.INDEX_HTML
    assert 'id="context-tab-tools"' in web_ui.INDEX_HTML
    assert 'id="context-tab-contexts"' in web_ui.INDEX_HTML
    assert 'id="working-messages-json"' in web_ui.INDEX_HTML
    assert 'id="working-tools-list"' in web_ui.INDEX_HTML
    assert 'id="working-contexts-list"' in web_ui.INDEX_HTML
    assert 'id="reset-history"' in web_ui.INDEX_HTML
    assert 'type: "working_context"' in web_ui.APP_JS
    assert 'payload.type === "working_context"' in web_ui.APP_JS


def test_web_ui_working_context_includes_messages_and_tools() -> None:
    class Agent:
        def working_messages_snapshot(self):
            return [{"role": "user", "content": "hello"}]

        def tool_states_snapshot(self):
            return [
                {
                    "server": "demo",
                    "name": "demo__read",
                    "description": "read data",
                    "server_enabled": True,
                    "enabled": True,
                }
            ]

        def context_states_snapshot(self):
            return [
                {
                    "id": "file:0",
                    "kind": "file",
                    "label": "context.txt",
                    "source": "context.txt",
                    "content": "reference",
                    "enabled": True,
                }
            ]

        def okf_status_snapshot(self):
            return {"configured": True, "enabled": True, "available": True}

    class WebSocket:
        query_params = {"token": "secret"}
        headers = {"origin": "http://127.0.0.1:12345"}

        def __init__(self):
            self.sent = []
            self.received = False

        async def accept(self):
            return None

        async def close(self, *, code):
            raise AssertionError(f"unexpected close: {code}")

        async def send_json(self, payload):
            self.sent.append(payload)

        async def receive_json(self):
            if self.received:
                raise RuntimeError("disconnect")
            self.received = True
            return {"type": "working_context"}

    async def run():
        broker = web_ui.WebUiApprovalBroker()
        session = web_ui._WebUiSession(
            agent=Agent(),
            approval_broker=broker,
            token="secret",
            expected_origin="http://127.0.0.1:12345",
            workspace=Path("."),
            model="model",
            mcp_servers=(),
            output_target=None,
            initial_messages=(),
            debug=False,
        )
        websocket = WebSocket()
        await session.websocket(websocket)
        return websocket.sent

    sent = asyncio.run(run())
    assert {
        "type": "working_context",
        "messages": [{"role": "user", "content": "hello"}],
        "tools": [
            {
                "server": "demo",
                "name": "demo__read",
                "description": "read data",
                "server_enabled": True,
                "enabled": True,
            }
        ],
        "contexts": [
            {
                "id": "file:0",
                "kind": "file",
                "label": "context.txt",
                "source": "context.txt",
                "content": "reference",
                "enabled": True,
            }
        ],
        "okf": {"configured": True, "enabled": True, "available": True},
    } in sent


def test_working_messages_snapshot_is_detached() -> None:
    from cli_agent.agent_conversation import ConversationMixin

    class Dummy(ConversationMixin):
        pass

    agent = Dummy()
    agent._last_working_messages = [
        {"role": "user", "content": {"value": ["original"]}}
    ]
    snapshot = agent.working_messages_snapshot()
    snapshot[0]["content"]["value"].append("changed")

    assert agent._last_working_messages == [
        {"role": "user", "content": {"value": ["original"]}}
    ]


def test_working_tools_snapshot_is_detached() -> None:
    from cli_agent.agent_conversation import ConversationMixin

    class Dummy(ConversationMixin):
        pass

    agent = Dummy()
    agent._last_working_tools = [
        {"type": "function", "function": {"name": "demo", "parameters": {"type": "object"}}}
    ]
    snapshot = agent.working_tools_snapshot()
    snapshot[0]["function"]["parameters"]["type"] = "array"

    assert agent._last_working_tools == [
        {"type": "function", "function": {"name": "demo", "parameters": {"type": "object"}}}
    ]


def test_working_tools_snapshot_defaults_to_empty_list() -> None:
    from cli_agent.agent_conversation import ConversationMixin

    class Dummy(ConversationMixin):
        pass

    assert Dummy().working_tools_snapshot() == []



def test_history_reset_clears_conversation_diagnostics() -> None:
    from cli_agent.agent_conversation import ConversationMixin

    class Dummy(ConversationMixin):
        pass

    agent = Dummy()
    agent.history = [{"role": "user", "content": "old"}]
    agent._last_working_messages = [{"role": "user", "content": "old"}]
    agent._last_working_tools = [{"type": "function"}]
    agent._dumped_history_json = "old"

    agent.reset_history()

    assert agent.history == []
    assert agent.working_messages_snapshot() == []
    assert agent.working_tools_snapshot() == []
    assert agent._dumped_history_json is None


def test_web_ui_renders_mcp_user_messages_centered_and_distinct() -> None:
    assert ".message.mcp-user" in web_ui.APP_CSS
    assert "align-self: center" in web_ui.APP_CSS
    assert 'payload.type === "mcp_user_message"' in web_ui.APP_JS
    assert 'addMessage(' in web_ui.APP_JS
    assert '"mcp-user"' in web_ui.APP_JS



def test_web_ui_served_javascript_has_valid_mcp_notice_escape() -> None:
    # APP_JS is a Python triple-quoted string: a single \n in its source
    # becomes a literal newline inside a JavaScript string and breaks startup.
    assert 'source ? "MCP (" + source + ")\\n" + content : content' in web_ui.APP_JS


def test_web_ui_served_javascript_syntax() -> None:
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")

    result = subprocess.run(
        [node, "--check", "--input-type=commonjs"],
        input=web_ui.APP_JS,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_web_ui_broker_sends_mcp_user_message_event() -> None:
    async def exercise():
        broker = web_ui.WebUiApprovalBroker()
        sent = []

        async def sender(payload):
            sent.append(payload)

        broker.set_event_sender(sender)
        await broker.show_mcp_user_message(
            "Prepare dependencies on the host.",
            "code-validator__run_java_build",
        )
        return sent

    assert asyncio.run(exercise()) == [
        {
            "type": "mcp_user_message",
            "content": "Prepare dependencies on the host.",
            "source": "code-validator__run_java_build",
        }
    ]


def test_web_ui_mcp_user_message_is_queued_across_disconnects() -> None:
    class Agent:
        pass

    async def exercise():
        broker = web_ui.WebUiApprovalBroker()
        session = web_ui._WebUiSession(
            agent=Agent(),
            approval_broker=broker,
            token="secret",
            expected_origin="http://127.0.0.1:12345",
            workspace=Path("."),
            model="model",
            mcp_servers=(),
            output_target=None,
            initial_messages=(),
            debug=False,
        )
        await broker.show_mcp_user_message(
            "Prepare dependencies on the host.",
            "code-validator__run_java_build",
        )
        assert session._pending_events == [
            {
                "type": "mcp_user_message",
                "content": "Prepare dependencies on the host.",
                "source": "code-validator__run_java_build",
            }
        ]

        delivered = []

        async def sender(payload):
            delivered.append(payload)

        session._active_sender = sender
        await session._flush_pending_events()
        return delivered, session._pending_events

    delivered, pending = asyncio.run(exercise())
    assert delivered == [
        {
            "type": "mcp_user_message",
            "content": "Prepare dependencies on the host.",
            "source": "code-validator__run_java_build",
        }
    ]
    assert pending == []


def test_web_ui_contains_runtime_toggle_protocol() -> None:
    for event_type in ("reset_history", "tool_toggle", "context_toggle", "okf_toggle"):
        assert f'type: "{event_type}"' in web_ui.APP_JS
    assert "renderTools(tools)" in web_ui.APP_JS
    assert "renderContexts(contexts, payload.okf || {})" in web_ui.APP_JS



def test_web_ui_runtime_toggle_buttons_follow_busy_state() -> None:
    assert 'button[data-runtime-toggle]' in web_ui.APP_JS
    assert 'button.dataset.runtimeToggle = "true"' in web_ui.APP_JS
    assert 'button.dataset.permanentlyDisabled = String(Boolean(disabled))' in web_ui.APP_JS
    assert 'button.disabled = busy || disconnected || permanentlyDisabled' in web_ui.APP_JS



def test_web_ui_history_reset_failure_returns_error_without_disconnect() -> None:
    class Agent:
        def reset_history(self):
            raise PermissionError("dump locked")

        def working_messages_snapshot(self):
            return [{"role": "user", "content": "old"}]

        def tool_states_snapshot(self):
            return []

        def context_states_snapshot(self):
            return []

        def okf_status_snapshot(self):
            return {"configured": False, "enabled": False, "available": False}

    class WebSocket:
        query_params = {"token": "secret"}
        headers = {"origin": "http://127.0.0.1:12345"}

        def __init__(self):
            self.sent = []
            self.messages = iter(
                [
                    {"type": "reset_history"},
                    {"type": "working_context"},
                ]
            )

        async def accept(self):
            return None

        async def close(self, *, code):
            raise AssertionError(f"unexpected close: {code}")

        async def send_json(self, payload):
            self.sent.append(payload)

        async def receive_json(self):
            try:
                return next(self.messages)
            except StopIteration as exc:
                raise RuntimeError("disconnect") from exc

    async def run():
        broker = web_ui.WebUiApprovalBroker()
        session = web_ui._WebUiSession(
            agent=Agent(),
            approval_broker=broker,
            token="secret",
            expected_origin="http://127.0.0.1:12345",
            workspace=Path("."),
            model="model",
            mcp_servers=(),
            output_target=None,
            initial_messages=(),
            debug=False,
        )
        websocket = WebSocket()
        await session.websocket(websocket)
        return websocket.sent

    sent = asyncio.run(run())

    assert any(
        event.get("type") == "error" and "dump locked" in event.get("content", "")
        for event in sent
    )
    assert {"type": "history_reset"} not in sent
    assert {
        "type": "working_context",
        "messages": [{"role": "user", "content": "old"}],
        "tools": [],
        "contexts": [],
        "okf": {"configured": False, "enabled": False, "available": False},
    } in sent



def test_working_context_refresh_does_not_open_dialog_implicitly() -> None:
    working_context_handler = web_ui.APP_JS.split(
        'if (payload.type === "working_context") {',
        1,
    )[1].split("return;", 1)[0]

    assert "showModal()" not in working_context_handler
    explicit_action = web_ui.APP_JS.split(
        'showWorkingContext.addEventListener("click"',
        1,
    )[1].split("});", 1)[0]
    assert "workingContextDialog.showModal()" in explicit_action


def test_write_diff_preview_and_changed_file_rejection(tmp_path):
    from cli_agent.config import McpServerConfig
    from cli_agent.os_operations import Workspace

    (tmp_path / "a.properties").write_bytes(b"color=gr\xfcn\n")
    workspace = Workspace.from_directory(
        tmp_path, McpServerConfig(name="os", config={"allow_write_files": True})
    )

    async def run():
        broker = web_ui.WebUiApprovalBroker(workspace=workspace)
        messages = []

        async def sender(payload):
            messages.append(payload)

        broker.attach(sender)
        task = asyncio.create_task(
            broker.approve_tool_call(
                "os__write_file",
                {"path": "a.properties", "content": "color=blau\n"},
            )
        )
        while not messages:
            await asyncio.sleep(0)
        assert "-color=grün" in messages[0]["diff"]
        assert "+color=blau" in messages[0]["diff"]
        (tmp_path / "a.properties").write_bytes(b"color=rot\n")
        assert broker.resolve(messages[0]["id"], "yes")
        return await task

    assert asyncio.run(run()) is False


def test_write_diff_cannot_read_protected_or_external_paths(tmp_path):
    from cli_agent.config import McpServerConfig
    from cli_agent.os_operations import Workspace

    (tmp_path / "private.txt").write_text("secret")
    workspace = Workspace.from_directory(
        tmp_path,
        McpServerConfig(name="os", config={"allow_write_files": True}),
        protected_paths=(tmp_path / "private.txt",),
    )
    broker = web_ui.WebUiApprovalBroker(workspace=workspace)
    for path in ("private.txt", "../outside.txt"):
        preview, snapshot = broker._write_preview(
            "os__write_file", {"path": path, "content": "new"}
        )
        assert snapshot is None
        assert "diff_error" in preview
        assert "secret" not in str(preview)


def test_external_tool_never_receives_local_file_diff(tmp_path):
    from cli_agent.config import McpServerConfig
    from cli_agent.os_operations import Workspace

    (tmp_path / "source.txt").write_text("private")
    broker = web_ui.WebUiApprovalBroker(
        workspace=Workspace.from_directory(
            tmp_path, McpServerConfig(name="os", config={"allow_write_files": True})
        )
    )
    preview, snapshot = broker._write_preview(
        "external__write_file",
        {"path": "source.txt", "content": "replacement"},
    )
    assert preview == {}
    assert snapshot is None


@pytest.mark.parametrize("newline", ["\r\n", "\r", "\n"])
def test_write_diff_ignores_existing_line_ending_style(tmp_path, newline):
    from cli_agent.config import McpServerConfig
    from cli_agent.os_operations import Workspace

    (tmp_path / "file.txt").write_bytes(
        ("".join(f"line{i}{newline}" for i in range(1500))).encode("utf-8")
    )
    broker = web_ui.WebUiApprovalBroker(
        workspace=Workspace.from_directory(
            tmp_path, McpServerConfig(name="os", config={"allow_write_files": True})
        )
    )
    proposal = "".join(
        f"line{i if i != 800 else 'changed'}\n" for i in range(1500)
    )
    preview, snapshot = broker._write_preview(
        "os__write_file", {"path": "file.txt", "content": proposal}
    )
    assert snapshot is not None
    assert "diff_error" not in preview
    assert "-line800\n" in preview["diff"]
    assert "+linechanged\n" in preview["diff"]
    assert "-line799" not in preview["diff"]


def test_write_diff_escapes_unicode_controls_in_paths_and_content(tmp_path):
    from cli_agent.config import McpServerConfig
    from cli_agent.os_operations import Workspace

    filename = "a\u202eb.txt"
    (tmp_path / filename).write_text("old\u2066value", encoding="utf-8")
    broker = web_ui.WebUiApprovalBroker(
        workspace=Workspace.from_directory(
            tmp_path, McpServerConfig(name="os", config={"allow_write_files": True})
        )
    )
    preview, snapshot = broker._write_preview(
        "os__write_file",
        {"path": filename, "content": "new\u202evalue\u200d"},
    )
    assert snapshot is not None
    assert "\\u202e" in preview["diff"]
    assert "\\u2066" in preview["diff"]
    assert "\\u200d" in preview["diff"]
    assert "\u202e" not in preview["diff"]
    assert "\u2066" not in preview["diff"]


@pytest.mark.parametrize(
    ("before", "after", "removed", "added"),
    [
        ("old", "new", "-old\n\\ No newline at end of file", "+new\n\\ No newline at end of file"),
        ("same", "same\n", "-same\n\\ No newline at end of file", "+same\n"),
        ("same\n", "same", "-same\n", "+same\n\\ No newline at end of file"),
    ],
)
def test_write_diff_marks_unterminated_lines(tmp_path, before, after, removed, added):
    from cli_agent.config import McpServerConfig
    from cli_agent.os_operations import Workspace

    (tmp_path / "file.txt").write_text(before, encoding="utf-8")
    broker = web_ui.WebUiApprovalBroker(
        workspace=Workspace.from_directory(
            tmp_path, McpServerConfig(name="os", config={"allow_write_files": True})
        )
    )
    preview, snapshot = broker._write_preview(
        "os__write_file", {"path": "file.txt", "content": after}
    )
    assert snapshot is not None
    assert removed in preview["diff"]
    assert added in preview["diff"]


def test_write_preview_limits_reads_before_open(tmp_path, monkeypatch):
    from cli_agent.config import McpServerConfig
    from cli_agent.os_operations import Workspace

    path = tmp_path / "large.txt"
    path.write_bytes(b"a" * (web_ui.MAX_APPROVAL_DIFF_BYTES + 1))
    workspace = Workspace.from_directory(
        tmp_path, McpServerConfig(name="os", config={"allow_write_files": True})
    )
    broker = web_ui.WebUiApprovalBroker(workspace=workspace)
    preview, snapshot = broker._write_preview(
        "os__write_file", {"path": "large.txt", "content": "short"}
    )
    assert snapshot is None
    assert "zu groß" in preview["diff_error"]


def test_write_preview_rejects_symlink_swap_before_open(tmp_path, monkeypatch):
    from cli_agent.config import McpServerConfig
    from cli_agent.os_operations import Workspace

    target = tmp_path / "a.txt"
    secret = tmp_path / "secret.txt"
    target.write_text("allowed")
    secret.write_text("sensitive")
    workspace = Workspace.from_directory(
        tmp_path,
        McpServerConfig(name="os", config={"allow_write_files": True}),
        protected_paths=(secret,),
    )
    original_open = web_ui.os.open

    def swap_and_open(path, flags, *args, **kwargs):
        target.unlink()
        try:
            target.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation unavailable")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(web_ui.os, "open", swap_and_open)
    preview, snapshot = web_ui.WebUiApprovalBroker(workspace=workspace)._write_preview(
        "os__write_file", {"path": "a.txt", "content": "new"}
    )
    assert snapshot is None
    assert "sensitive" not in str(preview)
    assert "diff_error" in preview


def test_write_preview_rejects_file_growth_on_confirm(tmp_path):
    from cli_agent.config import McpServerConfig
    from cli_agent.os_operations import Workspace

    path = tmp_path / "a.txt"
    path.write_text("initial", encoding="utf-8")
    workspace = Workspace.from_directory(
        tmp_path, McpServerConfig(name="os", config={"allow_write_files": True})
    )

    async def run():
        broker = web_ui.WebUiApprovalBroker(workspace=workspace)
        messages = []

        async def sender(payload):
            messages.append(payload)

        broker.attach(sender)
        task = asyncio.create_task(
            broker.approve_tool_call(
                "os__write_file", {"path": "a.txt", "content": "changed"}
            )
        )
        while not messages:
            await asyncio.sleep(0)
        path.write_bytes(b"x" * (web_ui.MAX_APPROVAL_DIFF_BYTES + 1))
        assert broker.resolve(messages[0]["id"], "yes")
        return await task

    assert asyncio.run(run()) is False


@pytest.mark.parametrize("decision", ["no", "yes", "session"])
def test_blocked_write_approval_can_be_dismissed_without_authorizing(tmp_path, decision):
    from cli_agent.config import McpServerConfig
    from cli_agent.os_operations import Workspace

    (tmp_path / "huge.txt").write_bytes(
        b"a" * (web_ui.MAX_APPROVAL_DIFF_BYTES + 1)
    )
    workspace = Workspace.from_directory(
        tmp_path, McpServerConfig(name="os", config={"allow_write_files": True})
    )

    async def run():
        broker = web_ui.WebUiApprovalBroker(workspace=workspace)
        messages = []

        async def sender(payload):
            messages.append(payload)

        broker.attach(sender)
        task = asyncio.create_task(
            broker.approve_tool_call(
                "os__write_file", {"path": "huge.txt", "content": "changed"}
            )
        )
        while not messages:
            await asyncio.sleep(0)
        request_id = messages[0]["id"]
        assert messages[0]["diff_blocked"] is True
        assert task.done() is False
        assert broker.resolve(request_id, decision)
        assert await task is False
        return broker.resolve(request_id, "no")

    assert asyncio.run(run()) is False


@pytest.mark.skipif(web_ui.os.name == "nt", reason="POSIX st_ino fallback only")
def test_write_preview_fails_closed_when_inode_unavailable(tmp_path, monkeypatch):
    from cli_agent.config import McpServerConfig
    from cli_agent.os_operations import Workspace

    file_path = tmp_path / "safe.txt"
    file_path.write_text("legitimate")
    workspace = Workspace.from_directory(
        tmp_path, McpServerConfig(name="os", config={"allow_write_files": True})
    )
    original_fstat = web_ui.os.fstat

    def unavailable_id(fd):
        from types import SimpleNamespace

        values = original_fstat(fd)
        return SimpleNamespace(
            st_mode=values.st_mode, st_nlink=values.st_nlink,
            st_dev=values.st_dev, st_ino=0,
            st_size=values.st_size, st_mtime_ns=values.st_mtime_ns,
            st_ctime_ns=values.st_ctime_ns,
        )

    with monkeypatch.context() as patch:
        patch.setattr(web_ui.os, "fstat", unavailable_id)
        preview, snapshot = web_ui.WebUiApprovalBroker(
            workspace=workspace
        )._write_preview("os__write_file", {
            "path": "safe.txt", "content": "replacement"
        })
    assert snapshot is None
    assert "Dateiidentität" in preview["diff_error"]
    assert "legitimate" not in str(preview)
