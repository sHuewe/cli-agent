from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.interfaces import web_ui
from cli_agent.interfaces.cli import build_parser


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


def test_web_ui_header_is_full_width() -> None:
    assert "header {\n  position: relative;" in web_ui.APP_CSS
    assert "  width: 100%;" in web_ui.APP_CSS
    assert "  margin: 0;" in web_ui.APP_CSS


def test_working_messages_snapshot_is_detached() -> None:
    from cli_agent.runtime.agent_conversation import ConversationMixin

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
    from cli_agent.runtime.agent_conversation import ConversationMixin

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
    from cli_agent.runtime.agent_conversation import ConversationMixin

    class Dummy(ConversationMixin):
        pass

    assert Dummy().working_tools_snapshot() == []



def test_history_reset_clears_conversation_diagnostics() -> None:
    from cli_agent.runtime.agent_conversation import ConversationMixin

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
    from cli_agent.settings.config import McpServerConfig
    from cli_agent.platform.os_operations import Workspace

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
    from cli_agent.settings.config import McpServerConfig
    from cli_agent.platform.os_operations import Workspace

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
    from cli_agent.settings.config import McpServerConfig
    from cli_agent.platform.os_operations import Workspace

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
    from cli_agent.settings.config import McpServerConfig
    from cli_agent.platform.os_operations import Workspace

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
    from cli_agent.settings.config import McpServerConfig
    from cli_agent.platform.os_operations import Workspace

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
    from cli_agent.settings.config import McpServerConfig
    from cli_agent.platform.os_operations import Workspace

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
    from cli_agent.settings.config import McpServerConfig
    from cli_agent.platform.os_operations import Workspace

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
    from cli_agent.settings.config import McpServerConfig
    from cli_agent.platform.os_operations import Workspace

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
    from cli_agent.settings.config import McpServerConfig
    from cli_agent.platform.os_operations import Workspace

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
    from cli_agent.settings.config import McpServerConfig
    from cli_agent.platform.os_operations import Workspace

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
    from cli_agent.settings.config import McpServerConfig
    from cli_agent.platform.os_operations import Workspace

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


def test_chroma_search_summaries_render_only_from_standard_agui_tool_results():
    from importlib.resources import files

    script = files("cli_agent.interfaces").joinpath("static", "app.js").read_text(
        encoding="utf-8",
    )
    assert 'event.type === "TOOL_CALL_RESULT"' in script
    assert "renderChromaSearchSummary(event)" in script
    assert 'status.dataset.toolName !== "vector__knowledge_search"' in script
    assert "value.matches" in script
    assert 't("vector.summary"' in script
    from cli_agent.interfaces.web_ui_locales import get_locale
    english = get_locale("en")
    german = get_locale("de")
    assert english is not None and german is not None
    assert "Collection: {collection}" in english["vector.summary"]
    assert german["vector.initial"] == "Initiale Suche"
    assert "summary.textContent =" in script
    assert "summary.innerHTML" not in script


def test_configured_ui_title_and_controls_are_rendered_safely() -> None:
    from cli_agent.settings.config import UiButtonsConfig, UiCommandButtonsConfig, UiConfig

    assert "<title>cli-agent</title>" in web_ui.render_index_html()
    configured = UiConfig(
        title="<SignalDeck & Assistant>",
        default_language="de",
        buttons=UiButtonsConfig(
            new_session=False,
            list_sessions=False,
            reset_history=False,
            show_working_context=False,
            end_session=False,
            language_selector=False,
            commands=UiCommandButtonsConfig(
                tokens=False,
                add_web_context=False,
                clear_web_context=False,
                enable=False,
                disable=False,
            ),
        ),
    )
    rendered = web_ui.render_index_html(configured)
    assert "<title>&lt;SignalDeck &amp; Assistant&gt;</title>" in rendered
    assert "<h1>&lt;SignalDeck &amp; Assistant&gt;</h1>" in rendered
    assert "<SignalDeck & Assistant>" not in rendered
    assert 'data-default-language="de"' in rendered
    assert 'data-language-selector="false"' in rendered
    assert 'class="language-picker" hidden' in rendered
    assert 'id="new-session" hidden' in rendered
    assert 'id="list-sessions" hidden' in rendered
    assert 'id="reset-history" hidden' in rendered
    assert 'id="show-working-context" hidden' in rendered
    assert 'id="quit" hidden' in rendered
    assert 'id="commands" hidden' in rendered
    for command in ("tokens", "add_web_context", "clear_web_context", "enable", "disable"):
        assert f'data-command="{command}" hidden' in rendered
    assert '[hidden] { display: none !important; }' in web_ui.APP_CSS
    assert 'id="prompt"' in rendered
    assert 'id="send"' in rendered
    assert 'data-decision="yes"' in rendered


def test_default_ui_title_and_buttons_remain_unchanged() -> None:
    from cli_agent.settings.config import UiConfig

    assert "<title>cli-agent</title>" in web_ui.render_index_html(UiConfig())
    assert '{{ ui.title }}' in web_ui.INDEX_HTML
    assert 'id="new-session"' in web_ui.INDEX_HTML



def test_jinja_nested_ui_blocks_can_be_overridden_and_reordered():
    from cli_agent.settings.config import UiConfig

    project = """{% extends "cli_agent/index.html" %}
{% block app_info %}
  <div class="project-info">
    {{ self.app_title() }}
    <span>Welcome to the project</span>
    {{ self.session_meta() }}
  </div>
{% endblock %}
{% block header_buttons %}
  <div class="header-actions">
    {{ self.button_show_working_context() }}
    <span id="project-label">Control panel</span>
    {{ self.button_new_session() }}
    {{ self.button_language_selector() }}
    {{ self.button_list_sessions() }}
    {{ self.button_reset_history() }}
    {{ self.button_end_session() }}
  </div>
{% endblock %}
"""
    rendered = web_ui.render_index_html(UiConfig(title="My Project"), project_template=project)
    assert "<h1>My Project</h1>" in rendered
    assert "Welcome to the project" in rendered
    assert rendered.index('id="show-working-context"') < rendered.index('id="new-session"')
    assert rendered.index('id="new-session"') < rendered.index('id="list-sessions"')
    assert rendered.index('id="project-label"') < rendered.index('id="new-session"')
    assert 'id="chat"' in rendered
    assert 'id="composer"' in rendered
    assert 'id="session-meta"' in rendered


def test_nested_jinja_app_info_can_be_overridden_without_touching_header():
    from cli_agent.settings.config import UiConfig

    project = """{% extends "cli_agent/index.html" %}
{% block app_info %}<div>{{ self.app_title() }}<p>Project subtitle</p>{{ self.session_meta() }}</div>{% endblock %}
"""
    rendered = web_ui.render_index_html(UiConfig(), project_template=project)
    assert "Project subtitle" in rendered
    assert 'id="new-session"' in rendered
    assert 'id="session-meta"' in rendered


@pytest.mark.parametrize("template", [
    '{% extends "cli_agent/index.html" %}{% block app_info %}<div>Missing session meta</div>{% endblock %}',
    '{% extends "cli_agent/index.html" %}{% block header_buttons %}<div>Only text</div>{% endblock %}',
    '{% extends "cli_agent/index.html" %}{% block extra_head %}<script src="/assets/app.js"></script>{% endblock %}',
])
def test_jinja_rejects_broken_javascript_structure(template):
    from cli_agent.settings.config import UiConfig

    with pytest.raises(ValueError, match="UI template"):
        web_ui.render_index_html(UiConfig(), project_template=template)



def test_built_in_ui_has_modern_styles_and_theme_links():
    from importlib.resources import files
    from cli_agent.settings.config import UiConfig

    rendered = web_ui.render_index_html(
        UiConfig(theme="bright", stylesheets=("ui/branding.css",)),
    )
    assert 'href="/assets/themes/bright.css"' in rendered
    assert 'href="/assets/ui/branding.css"' in rendered
    assert 'class="app-brand"' in rendered
    for theme in ("bright", "dark"):
        data = files("cli_agent.interfaces").joinpath(
            "static", "themes", theme + ".css",
        ).read_text(encoding="utf-8")
        assert "--ui-panel:" in data
        assert "--ui-text:" in data
        assert "--ui-accent:" in data
    assert "100dvh" in web_ui.APP_CSS
    assert "#composer:focus-within" in web_ui.APP_CSS
    assert ".chat > * { flex-shrink: 0; }" in web_ui.APP_CSS
    assert "[hidden] { display: none !important; }" in web_ui.APP_CSS



def test_configurable_app_icon_and_nested_block():
    from cli_agent.settings.config import UiConfig

    default = web_ui.render_index_html(UiConfig())
    assert 'class="app-icon"' in default
    assert "app-icon-custom" not in default
    assert ".app-icon::after {" in web_ui.APP_CSS
    assert "header .app-brand::before {" not in web_ui.APP_CSS

    custom = web_ui.render_index_html(UiConfig(icon="⚡"))
    assert 'class="app-icon app-icon-custom" aria-hidden="true">⚡</span>' in custom
    assert '<h1>cli-agent</h1>' in custom
    assert 'id="session-meta"' in custom

    escaped = web_ui.render_index_html(UiConfig(icon="<&>"))
    assert '>&lt;&amp;&gt;</span>' in escaped
    assert '<&>' not in escaped

    hidden = web_ui.render_index_html(UiConfig(icon=""))
    assert 'class="app-icon"' not in hidden
    assert 'class="app-icon app-icon-custom"' not in hidden
    assert 'id="session-meta"' in hidden

    project = """{% extends "cli_agent/index.html" %}
{% block app_icon %}
<span class="app-icon app-icon-custom" aria-hidden="true">Project</span>
{% endblock app_icon %}
"""
    override = web_ui.render_index_html(UiConfig(), project_template=project)
    assert '>Project</span>' in override
    assert '<h1>cli-agent</h1>' in override
    assert 'id="session-meta"' in override


def test_project_image_icon_rendering_and_css():
    from cli_agent.settings.config import UiConfig
    rendered = web_ui.render_index_html(UiConfig(icon="ui/logo.png"))
    assert 'src="/assets/ui/icon/logo.png"' in rendered
    assert 'class="app-icon app-icon-custom app-icon-image"' in rendered
    assert ".app-icon-image img" in web_ui.APP_CSS
    assert 'id="session-meta"' in rendered


def test_default_header_icon_is_left_of_title_and_metadata():
    from cli_agent.settings.config import UiConfig

    rendered = web_ui.render_index_html(UiConfig())
    brand = rendered.index('class="app-brand"')
    icon = rendered.index('class="app-icon"', brand)
    copy = rendered.index('class="app-brand-copy"', icon)
    title = rendered.index('<h1>cli-agent</h1>', copy)
    meta = rendered.index('id="session-meta"', title)
    assert brand < icon < copy < title < meta

    # The old :first-child selector overrode header .app-brand due to specificity.
    assert "header > div:first-child:not(.app-brand)" in web_ui.APP_CSS
    assert "header .app-brand, header > div:first-child {" not in web_ui.APP_CSS
    assert "header .app-brand-copy {" in web_ui.APP_CSS
    assert "flex-direction: row;" in web_ui.APP_CSS


def test_header_and_chat_scrollbar_span_viewport_but_chat_content_is_centered():
    from cli_agent.settings.config import UiConfig

    html = web_ui.render_index_html(UiConfig())
    # Header is a direct body child, not inside the width-limited chat shell.
    assert html.index("<body>") < html.index("<header>") < html.index('<main class="shell">')
    assert html.index("</header>") < html.index('<main class="shell">')
    css = web_ui.APP_CSS
    assert "width: 100%;" in css
    assert "flex-direction: column;" in css
    assert "height: 100dvh;" in css
    assert "border-radius: 0;" in css
    modern = css[css.index("/* Modern application layout."):]
    shell = modern[modern.index(".shell {"):modern.index("\nheader {")]
    chat = modern[modern.index(".chat {"):modern.index("/* The chat is the scroll container.")]
    assert "max-width: none;" in shell
    assert "width: 100%;" in shell
    assert "padding: 0 0 22px;" in shell
    assert "--ui-content-width: 1168px;" in shell
    assert "width: 100%;" in chat
    assert "overflow-y: auto;" in chat
    assert "var(--ui-content-width)" in chat
    assert ".shell > .commands, .shell > #composer {" in modern
    assert "align-self: center;" in modern
    assert ".shell { --ui-content-width: 1268px; }" in modern
    assert "width: calc(100% - 24px);" in modern
    assert "width: calc(100% - 16px);" in modern
    assert ".chat > * { flex-shrink: 0; }" in css
    assert "height: auto;" in css


def test_header_metadata_visibility_and_centered_brand_title():
    from cli_agent.settings.config import UiConfig

    visible = web_ui.render_index_html(UiConfig())
    hidden = web_ui.render_index_html(UiConfig(show_metadata=False))
    assert 'id="session-meta" class="meta">Connecting' in visible
    assert 'id="session-meta" class="meta" hidden>Connecting' in hidden
    assert hidden.count('id="session-meta"') == 1
    assert '<h1>cli-agent</h1>' in hidden
    assert 'class="app-icon"' in hidden
    # app-brand remains horizontally aligned; title is centered vertically
    # beside its icon when the metadata row is hidden.
    css = web_ui.APP_CSS
    assert "header .app-brand-copy {" in css
    assert "justify-content: center;" in css
    assert "align-items: center;" in css


def test_project_template_works_with_hidden_metadata():
    from cli_agent.settings.config import UiConfig

    override = """{% extends "cli_agent/index.html" %}
{% block app_info %}
<div class="app-brand">
  {{ self.app_icon() }}
  <div class="app-brand-copy">{{ self.app_title() }}{{ self.session_meta() }}</div>
</div>
{% endblock %}
"""
    html = web_ui.render_index_html(
        UiConfig(show_metadata=False, icon="⚡"), project_template=override,
    )
    assert 'id="session-meta" class="meta" hidden' in html
    assert '<h1>cli-agent</h1>' in html


def test_short_viewport_fallback_keeps_composer_accessible():
    """A short/mobile viewport must scroll the page, never clip the composer."""
    css = web_ui.APP_CSS
    start = css.index("@media (max-height: 700px)")
    end = css.index("@media (prefers-reduced-motion: reduce)", start)
    short_viewport = css[start:end]

    # The normal full-height page is not changed for ordinary desktop layouts.
    assert "height: 100dvh;" in css[:start]
    assert "overflow: hidden;" in css[:start]

    # In compact layouts, scrolling the document keeps the composer
    # reachable regardless of header/command wrap and keyboard height.
    assert "height: auto;" in short_viewport
    assert "overflow-y: auto;" in short_viewport
    assert "overflow: visible;" in short_viewport
    assert "max-height: clamp(80px, 35dvh, 220px);" in short_viewport
    assert "min-height: 0;" in short_viewport
    assert "flex: 0 0 auto;" in short_viewport


def test_project_ui_button_labels_override_i18n_without_removing_actions():
    from cli_agent.settings.config import (
        UiButtonsConfig, UiCommandButtonsConfig, UiConfig,
    )

    config = UiConfig(
        buttons=UiButtonsConfig(
            reset_history=True,
            labels={"reset_history": "Neu", "new_session": "<&>"},
            commands=UiCommandButtonsConfig(
                labels={"tokens": "Verbrauch", "enable": "MCP einschalten"},
            ),
        ),
    )
    html = web_ui.render_index_html(config)
    import re
    reset = re.search(r'<button id="reset-history"[^>]*>.*?</button>', html)
    assert reset is not None
    assert ">Neu</button>" in reset.group(0)
    assert 'data-i18n="history.reset"' not in reset.group(0)
    assert 'id="reset-history"' in reset.group(0)
    custom = re.search(r'<button id="new-session"[^>]*>.*?</button>', html)
    assert custom is not None
    assert ">&lt;&amp;&gt;</button>" in custom.group(0)
    assert 'data-i18n="session.new"' not in custom.group(0)
    tokens = re.search(r'<button class="secondary" data-command="tokens"[^>]*>.*?</button>', html)
    assert tokens is not None
    assert ">Verbrauch</button>" in tokens.group(0)
    assert 'data-i18n="commands.tokens"' not in tokens.group(0)
    assert 'data-command="tokens"' in tokens.group(0)
    enabled = re.search(r'<button class="secondary" data-command="enable"[^>]*>.*?</button>', html)
    assert enabled is not None
    assert ">MCP einschalten</button>" in enabled.group(0)
    assert 'data-i18n="commands.enableMcp"' not in enabled.group(0)


def test_project_ui_unmodified_buttons_remain_localizable():
    from cli_agent.settings.config import UiConfig

    html = web_ui.render_index_html(UiConfig())
    assert 'data-i18n="history.reset">Reset history</button>' in html
    assert 'data-i18n="commands.tokens">Tokens</button>' in html
    assert 'data-i18n="session.new">New session in new tab</button>' in html
    assert 'data-i18n="language.label">Language</span>' in html


def test_project_ui_language_selector_label_can_be_customized():
    from cli_agent.settings.config import UiButtonsConfig, UiConfig

    html = web_ui.render_index_html(
        UiConfig(buttons=UiButtonsConfig(labels={"language_selector": "Sprache"})),
    )
    assert "Sprache</span>" in html
    assert 'data-i18n="language.label"' not in html
    assert 'id="language-select"' in html


def test_end_session_button_default_is_still_localizable():
    from cli_agent.settings.config import UiButtonsConfig, UiConfig

    default = web_ui.render_index_html(UiConfig())
    assert 'data-i18n="session.end">End session</button>' in default
    custom = web_ui.render_index_html(
        UiConfig(buttons=UiButtonsConfig(labels={"end_session": "Schließen"})),
    )
    assert '>Schließen</button>' in custom
    assert 'data-i18n="session.end"' not in custom
    assert 'id="quit"' in custom


def test_composer_send_and_stop_share_single_grid_cell():
    from cli_agent.interfaces.web_ui import APP_CSS, render_index_html
    from cli_agent.settings.config import UiConfig

    html = render_index_html(UiConfig())
    assert '<button id="send" type="submit" data-i18n="composer.send">Send</button>' in html
    assert 'id="stop-run"' in html
    assert 'type="button" hidden data-i18n="composer.stop"' in html
    modern = APP_CSS[APP_CSS.index("/* Modern application layout."):]
    assert "#composer > #prompt { grid-column: 1; grid-row: 1; }" in modern
    assert "#composer > #send, #composer > #stop-run {" in modern
    assert "grid-column: 2;" in modern
    assert "grid-row: 1;" in modern
    assert "#stop-run::before {" in modern
    assert "background: #b43e49;" in modern
    assert "#send::before {" in modern
    assert "#composer > #send, #composer > #stop-run { min-width: 67px;" in modern


def test_packaged_base_css_is_unchanged_and_loadable() -> None:
    from importlib.resources import files

    stylesheet = files("cli_agent.interfaces").joinpath("static", "app.css")
    assert web_ui.APP_CSS == stylesheet.read_text(encoding="utf-8")
    assert ":root {" in web_ui.APP_CSS
    assert "[hidden] { display: none !important; }" in web_ui.APP_CSS
    assert ".chat > * { flex-shrink: 0; }" in web_ui.APP_CSS
    assert "#composer:focus-within" in web_ui.APP_CSS
