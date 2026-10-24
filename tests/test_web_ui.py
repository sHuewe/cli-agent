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


def test_web_ui_header_is_sticky() -> None:
    assert "header { position: sticky; top: 0;" in web_ui.APP_CSS


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
    original_open = web_ui._open_preview_file

    def swap_and_open(path):
        target.unlink()
        try:
            target.symlink_to(secret)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation unavailable")
        return original_open(path)

    monkeypatch.setattr(web_ui, "_open_preview_file", swap_and_open)
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
