from __future__ import annotations

from pathlib import Path

import asyncio
import tomllib
from types import SimpleNamespace

import pytest

from cli_agent import cli as cli_module
from cli_agent.admin_config import AdminConfig, McpPolicy, TrustedMcpServer, TrustedMcpToolApproval
from cli_agent.cli import (
    _approval_arguments,
    apply_mcp_cli_overrides,
    apply_model_cli_override,
    build_approval_callback,
    build_parser,
)
from cli_agent.config import AppConfig, McpServerConfig, ModelConfig
from cli_agent.mcp_contracts import tool_contract_fingerprint
from cli_agent.network_policy import NetworkConfig
from cli_agent.os_mcp_server import resolve_mcp_config
from cli_agent.terminal_output import sanitize_terminal_text


def test_approval_summary_shows_payload_even_for_sensitive_looking_argument_names() -> None:
    summary = _approval_arguments(
        {
            "path": "src/main.py",
            "content": "confidential source",
            "token": "TOKEN_SENTINEL",
            "password": "PASSWORD_SENTINEL",
            "authorization": "AUTH_SENTINEL",
            "api_key": "API_KEY_SENTINEL",
            "options": {"secret": "NESTED_SECRET_SENTINEL"},
        }
    )

    assert "src/main.py" in summary
    assert "confidential source" in summary
    assert "TOKEN_SENTINEL" in summary
    assert "PASSWORD_SENTINEL" in summary
    assert "AUTH_SENTINEL" in summary
    assert "API_KEY_SENTINEL" in summary
    assert "NESTED_SECRET_SENTINEL" in summary
    assert "verborgen" not in summary


def test_approval_summary_truncates_long_payload_with_head_and_tail() -> None:
    content = "A" * 1500 + "MIDDLE" * 500 + "Z" * 500
    summary = _approval_arguments({"content": content})
    assert "A" * 100 in summary
    assert "Z" * 100 in summary
    assert "Zeichen gekürzt" in summary
    assert len(summary) < len(content)


def test_os_cli_flags_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--with-os-read", "--with-os-write"])


def test_with_os_read_adds_read_only_server() -> None:
    config = apply_mcp_cli_overrides(AppConfig(), os_access="read")
    server = config.mcp_servers[0]
    assert server.name == "os"
    assert server.command == "{python}"
    assert server.allow_write_files() is False
    assert server.args[-2:] == ("--access", "read")


def test_with_os_write_replaces_configured_os_server() -> None:
    original = AppConfig(
        mcp_servers=(
            McpServerConfig(name="external", command="external-server"),
            McpServerConfig(name="os", command="legacy-os-server"),
        )
    )
    config = apply_mcp_cli_overrides(original, os_access="write")
    assert [server.name for server in config.mcp_servers] == ["external", "os"]
    assert config.mcp_servers[1].allow_write_files() is True


def test_without_os_cli_flag_keeps_config_unchanged() -> None:
    original = AppConfig(mcp_servers=(McpServerConfig(name="external", command="external-server"),))
    assert apply_mcp_cli_overrides(original, os_access=None) is original


def test_os_server_explicit_access_overrides_config() -> None:
    configured = (McpServerConfig(name="os", command="unused", config={"allow_write_files": True}),)
    assert resolve_mcp_config(configured, access="read").allow_write_files() is False
    assert resolve_mcp_config((), access="write").allow_write_files() is True


def test_model_cli_argument_is_parsed() -> None:
    assert build_parser().parse_args(["--model", "gwen100"]).model == "gwen100"


def test_response_format_defaults_to_text_and_parses_json() -> None:
    assert build_parser().parse_args(["prompt"]).response_format == "text"
    assert (
        build_parser().parse_args(
            ["--response-format", "json", "prompt"]
        ).response_format
        == "json"
    )



def test_add_file_context_is_alias_for_context_file() -> None:
    direct = build_parser().parse_args(
        ["--context-file", "reference.txt", "prompt"]
    )
    alias = build_parser().parse_args(
        ["--add-file-context", "reference.txt", "prompt"]
    )

    assert direct.context_files == alias.context_files
    assert alias.context_files == [Path("reference.txt")]

def test_cli_automation_options_are_repeatable() -> None:
    args = build_parser().parse_args(
        [
            "--approve-tool",
            "os__write_file",
            "--approve-tool",
            "external__run",
            "--add-file-context",
            "one.txt",
            "--context-file",
            "two.txt",
            "--add-web-context",
            "https://docs.example/a",
            "--add-web-context",
            "https://docs.example/b",
            "prompt",
        ]
    )
    assert args.approve_tool == ["os__write_file", "external__run"]
    assert args.context_files == [Path("one.txt"), Path("two.txt")]
    assert args.add_web_context == ["https://docs.example/a", "https://docs.example/b"]
    assert args.prompt == ["prompt"]


def test_cli_preapproval_matches_exact_tool_name(monkeypatch) -> None:
    async def fail_if_called(_tool_name, _arguments):
        raise AssertionError("interactive approval must not be used")

    monkeypatch.setattr(cli_module, "approve_tool_call", fail_if_called)
    callback = build_approval_callback(["os__write_file"])
    assert asyncio.run(callback("os__write_file", {"path": "a.txt"})) is True


def test_cli_preapproval_does_not_match_other_tool(monkeypatch) -> None:
    seen = []

    async def interactive(tool_name, arguments):
        seen.append((tool_name, arguments))
        return False

    monkeypatch.setattr(cli_module, "approve_tool_call", interactive)
    callback = build_approval_callback(["os__write_file"])
    assert asyncio.run(callback("os__delete_file", {"path": "a.txt"})) is False
    assert seen == [("os__delete_file", {"path": "a.txt"})]


def test_validator_cli_option_is_no_longer_available() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["--with-python-validator"])


def test_run_uses_admin_policy_for_network_and_mcp(tmp_path, monkeypatch) -> None:
    config = AppConfig(
        model=ModelConfig(
            provider="openai",
            model="internal-model",
            base_url="https://llm.internal/v1",
        )
    )
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    contract = tool_contract_fingerprint("search", schema)
    admin_config = AdminConfig(
        network=NetworkConfig(
            model_allowed_hosts=("llm.internal",),
            mcp_allowed_hosts=("mcp.internal",),
            web_allowed_hosts=("docs.internal",),
        ),
        mcp=McpPolicy(
            trusted_servers=(
                TrustedMcpServer(
                    name="continuous",
                    transport="streamable_http",
                    url="https://mcp.internal/mcp",
                    auto_approve_tools=(TrustedMcpToolApproval("search", contract),),
                ),
            ),
        ),
    )
    captured = {}

    class FakeAgent:
        def __init__(self, _workspace, model_client, *_args, **kwargs):
            captured["model_client"] = model_client
            captured["network"] = kwargs["network"]
            captured["mcp_policy"] = kwargs["mcp_policy"]
            captured["approval_callback"] = kwargs["approval_callback"]
            captured["prompts"] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def ask(self, prompt):
            captured["prompts"].append(prompt)
            return "ok"

    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "load_admin_config", lambda: admin_config)
    monkeypatch.setattr(cli_module, "configure_logging", lambda _config: None)
    monkeypatch.setattr(cli_module, "WebContextCliAgent", FakeAgent)

    args = SimpleNamespace(
        config=None,
        model=None,
        os_access=None,
        approve_tool=["continuous__search"],
        add_web_context=["https://docs.internal/reference"],
        debug=False,
        workspace=tmp_path,
        prompt=["hello"],
    )
    asyncio.run(cli_module.run(args))

    assert captured["model_client"].base_url == "https://llm.internal/v1"
    assert captured["network"] is admin_config.network
    assert captured["mcp_policy"] is admin_config.mcp
    assert captured["prompts"] == [
        "add_web_context https://docs.internal/reference",
        "hello",
        "tokens",
    ]
    assert asyncio.run(captured["approval_callback"]("continuous__search", {"query": "test"})) is True


def test_render_trusted_http_server_fragment_preserves_bearer_env() -> None:
    server = TrustedMcpServer(
        name="docs",
        transport="streamable_http",
        url="https://mcp.internal/mcp",
        bearer_token_env="CLI_AGENT_DOCS_TOKEN",
        trust_instructions=True,
    )

    fragment = cli_module._render_trusted_server_fragment(server)
    parsed = tomllib.loads(fragment)
    trusted = parsed["mcp"]["trusted_servers"][0]

    assert trusted["trust_instructions"] is True
    assert (
        trusted["from_env"]["authentication"]["bearer"]
        == "CLI_AGENT_DOCS_TOKEN"
    )
    assert "Bearer " not in fragment


def test_model_cli_override_replaces_only_model_name() -> None:
    original = AppConfig(
        model=ModelConfig(
            provider="openai",
            model="configured-model",
            base_url="https://model-host.example/v1",
            api_key_env="MODEL_API_KEY",
            timeout=42.0,
            headers={"X-Test": "value"},
        )
    )
    config = apply_model_cli_override(original, model="gwen100")
    assert config.model.model == "gwen100"
    assert config.model.provider == original.model.provider
    assert config.model.base_url == original.model.base_url
    assert config.model.api_key_env == original.model.api_key_env
    assert config.model.timeout == original.model.timeout
    assert config.model.headers == original.model.headers


def test_without_model_cli_argument_keeps_config_unchanged() -> None:
    original = AppConfig(model=ModelConfig(model="configured-model"))
    assert apply_model_cli_override(original, model=None) is original


def test_terminal_sanitizer_preserves_normal_unicode_and_emoji() -> None:
    value = "Grüße ✅ 🚀 👨‍💻 ❤️"

    assert sanitize_terminal_text(value) == value


def test_terminal_sanitizer_strict_mode_escapes_invisible_formatting() -> None:
    value = "a\u200bb\u200cc\u200dd\u2060e 👨‍💻 ❤️"

    sanitized = sanitize_terminal_text(
        value,
        escape_invisible_formatting=True,
    )

    assert sanitized == (
        "a\\u200bb\\u200cc\\u200dd\\u2060e "
        "👨\\u200d💻 ❤️"
    )
    assert "\u200b" not in sanitized
    assert "\u200c" not in sanitized
    assert "\u200d" not in sanitized
    assert "\u2060" not in sanitized


def test_terminal_sanitizer_escapes_terminal_and_bidi_controls() -> None:
    value = "safe\x1b[2Jhidden\u202eexe.txt\x07"

    sanitized = sanitize_terminal_text(value)

    assert "\x1b" not in sanitized
    assert "\u202e" not in sanitized
    assert "\x07" not in sanitized
    assert "\\u001b[2J" in sanitized
    assert "\\u202e" in sanitized
    assert "\\u0007" in sanitized


def test_terminal_sanitizer_single_line_escapes_line_controls() -> None:
    value = "tool\nforged\tname\roverwrite"

    assert sanitize_terminal_text(value, multiline=False) == (
        "tool\\nforged\\tname\\roverwrite"
    )


def test_approval_summary_escapes_bidi_but_preserves_emoji() -> None:
    summary = _approval_arguments(
        {
            "message": "Deploy ✅ \u202edanger",
        }
    )

    assert "Deploy ✅" in summary
    assert "\u202e" not in summary
    assert "\\u202e" in summary


def test_approval_tool_name_cannot_inject_terminal_controls(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli_module.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    approved = asyncio.run(
        cli_module.approve_tool_call(
            "evil\x1b[2J\u202etool\nforged",
            {"message": "ok ✅"},
        )
    )

    assert approved is False
    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\u202e" not in output
    assert "evil\\u001b[2J\\u202etool\\nforged" in output
    assert "ok ✅" in output


def test_debug_error_sanitizes_traceback_terminal_controls(
    capsys: pytest.CaptureFixture[str],
) -> None:
    try:
        raise RuntimeError("boom\x1b[2J\u202edanger")
    except RuntimeError as exc:
        cli_module.print_error(exc, debug=True)

    captured = capsys.readouterr()
    assert "\x1b" not in captured.err
    assert "\u202e" not in captured.err
    assert "\\u001b[2J" in captured.err
    assert "\\u202e" in captured.err
    assert "RuntimeError: boom" in captured.err


def test_rendered_tool_fragment_with_c1_control_remains_valid_toml() -> None:
    inspection = cli_module.McpToolInspection(
        server_name="docs",
        transport="stdio",
        tool_name="search\u009btool",
        description="",
        input_schema={"type": "object"},
        contract_sha256="sha256:" + "0" * 64,
        trusted_server_found=False,
        existing_contract_sha256=None,
    )

    rendered = sanitize_terminal_text(
        cli_module._render_tool_approval_fragment(inspection),
        multiline=True,
    )
    parsed = tomllib.loads(rendered)

    approval = parsed["mcp"]["trusted_servers"]["auto_approve_tools"][0]
    assert approval["name"] == "search\u009btool"


def test_preloaded_web_context_status_is_sanitized(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = AppConfig(
        model=ModelConfig(
            provider="openai",
            model="internal-model",
            base_url="https://llm.internal/v1",
        )
    )
    admin_config = AdminConfig(
        network=NetworkConfig(
            model_allowed_hosts=("llm.internal",),
            web_allowed_hosts=("docs.internal",),
        )
    )

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def ask(self, prompt):
            if prompt.startswith("add_web_context "):
                return "Web-Kontext hinzugefügt: https://docs.internal/a\x1b[2J\u202eevil"
            return "ok"

    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "load_admin_config", lambda: admin_config)
    monkeypatch.setattr(cli_module, "configure_logging", lambda _config: None)
    monkeypatch.setattr(cli_module, "WebContextCliAgent", FakeAgent)

    args = SimpleNamespace(
        config=None,
        model=None,
        os_access=None,
        approve_tool=[],
        add_web_context=["https://docs.internal/reference"],
        debug=False,
        workspace=tmp_path,
        prompt=[],
    )

    monkeypatch.setattr("builtins.input", lambda _prompt: (_ for _ in ()).throw(EOFError()))
    asyncio.run(cli_module.run(args))

    output = capsys.readouterr().out
    assert "\x1b" not in output
    assert "\u202e" not in output
    assert "\\u001b[2J" in output
    assert "\\u202e" in output


def test_terminal_sanitizer_escapes_lone_unicode_surrogates() -> None:
    value = "before\ud800middle\udfffafter ✅"

    sanitized = sanitize_terminal_text(value)

    assert sanitized == "before\\ud800middle\\udfffafter ✅"
    sanitized.encode("utf-8")


def test_strict_identifier_rendering_distinguishes_literal_escape_from_control() -> None:
    actual_escape = sanitize_terminal_text(
        "a\x1bb",
        multiline=False,
        escape_invisible_formatting=True,
        escape_literal_backslashes=True,
    )
    literal_escape = sanitize_terminal_text(
        "a\\u001bb",
        multiline=False,
        escape_invisible_formatting=True,
        escape_literal_backslashes=True,
    )

    assert actual_escape == "a\\u001bb"
    assert literal_escape == "a\\\\u001bb"
    assert actual_escape != literal_escape


def test_approval_tool_name_escapes_literal_backslashes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        cli_module.sys,
        "stdin",
        SimpleNamespace(isatty=lambda: True),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    approved = asyncio.run(
        cli_module.approve_tool_call(
            "tool\\u001bname",
            {},
        )
    )

    assert approved is False
    output = capsys.readouterr().out
    assert "tool\\\\u001bname" in output


def test_mcp_description_rendering_distinguishes_literal_escape_from_control() -> None:
    actual_escape = sanitize_terminal_text(
        "desc \x1b here",
        multiline=True,
        escape_invisible_formatting=True,
        escape_literal_backslashes=True,
    )
    literal_escape = sanitize_terminal_text(
        "desc \\u001b here",
        multiline=True,
        escape_invisible_formatting=True,
        escape_literal_backslashes=True,
    )

    assert actual_escape == "desc \\u001b here"
    assert literal_escape == "desc \\\\u001b here"
    assert actual_escape != literal_escape
