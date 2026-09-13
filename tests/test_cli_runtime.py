from __future__ import annotations

import asyncio
import builtins
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent import cli as cli_module
from cli_agent.admin_config import AdminConfig
from cli_agent.cli import McpToolInspection
from cli_agent.config import AppConfig, McpServerConfig, ModelConfig


def _args(tmp_path: Path, **overrides):
    values = dict(
        config=None,
        model=None,
        os_access=None,
        context_file=None,
        prompt_file=None,
        output=None,
        overwrite_output=False,
        approve_tool=[],
        add_web_context=[],
        debug=False,
        workspace=tmp_path,
        prompt=[],
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _patch_runtime(monkeypatch: pytest.MonkeyPatch, captured: dict) -> None:
    config = AppConfig(
        model=ModelConfig(
            provider="openai",
            model="test-model",
            base_url="http://localhost:11434/v1",
        )
    )

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            captured["constructed"] = True
            captured.setdefault("prompts", [])

        async def __aenter__(self):
            captured["entered"] = True
            return self

        async def __aexit__(self, *_args):
            captured["exited"] = True
            return None

        async def ask(self, prompt):
            captured["prompts"].append(prompt)
            if prompt == "boom":
                raise ValueError("agent failed")
            return f"answer:{prompt}"

    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "load_admin_config", lambda: AdminConfig())
    monkeypatch.setattr(cli_module, "configure_logging", lambda _config: None)
    monkeypatch.setattr(cli_module, "WebContextCliAgent", FakeAgent)
    monkeypatch.setattr(
        cli_module,
        "create_model_client",
        lambda *_args, **_kwargs: SimpleNamespace(
            model="test-model",
            base_url="http://localhost:11434/v1",
        ),
    )


def test_approve_tool_call_fails_closed_without_tty(monkeypatch) -> None:
    monkeypatch.setattr(cli_module.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert asyncio.run(cli_module.approve_tool_call("os__write_file", {"path": "a.txt"})) is False


def test_approve_tool_call_supports_session_approval(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli_module.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(builtins, "input", lambda _prompt: "s")

    result = asyncio.run(cli_module.approve_tool_call("os__write_file", {"path": "a.txt"}))

    assert result == "session"
    assert "Explizite Freigabe erforderlich" in capsys.readouterr().out


@pytest.mark.parametrize(("answer", "expected"), [("ja", True), ("yes", True), ("n", False), ("", False)])
def test_approve_tool_call_normalizes_yes_and_no(monkeypatch, answer, expected) -> None:
    monkeypatch.setattr(cli_module.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(builtins, "input", lambda _prompt: answer)
    assert asyncio.run(cli_module.approve_tool_call("tool", {})) is expected


def test_invalid_os_access_mode_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unsupported OS MCP access mode"):
        cli_module._os_mcp_server_config("admin")


def test_exception_details_flattens_and_deduplicates_exception_groups() -> None:
    exc = ExceptionGroup(
        "outer",
        [
            ValueError("bad value"),
            ExceptionGroup("nested", [RuntimeError("broken"), ValueError("bad value")]),
        ],
    )

    assert cli_module.exception_details(exc) == "ValueError: bad value\nRuntimeError: broken"


def test_exception_details_keeps_exception_type_for_empty_message() -> None:
    assert cli_module.exception_details(RuntimeError()) == "RuntimeError"


def test_print_error_uses_compact_message_without_debug(capsys) -> None:
    cli_module.print_error(ValueError("invalid"), debug=False)
    captured = capsys.readouterr()
    assert captured.out == "Fehler: ValueError: invalid\n"
    assert captured.err == ""


def test_print_error_emits_traceback_in_debug_mode(capsys) -> None:
    try:
        raise ValueError("invalid")
    except ValueError as exc:
        cli_module.print_error(exc, debug=True)

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ValueError: invalid" in captured.err
    assert "Traceback" in captured.err


def test_inspection_rejects_built_in_mcp_tools_before_starting_agent(tmp_path) -> None:
    server = McpServerConfig(name="os", command="ignored", built_in=True)
    with pytest.raises(ValueError, match="Built-in MCP-Tools"):
        asyncio.run(
            cli_module._inspect_mcp_tool(
                server=server,
                tool_name="read_file",
                workspace=tmp_path,
                config_file=tmp_path / "config.toml",
                admin_config=AdminConfig(),
            )
        )


def test_inspection_rejects_empty_native_tool_name(tmp_path) -> None:
    server = McpServerConfig(name="fachsoftware", command="ignored")
    with pytest.raises(ValueError, match="Toolname darf nicht leer sein"):
        asyncio.run(
            cli_module._inspect_mcp_tool(
                server=server,
                tool_name="fachsoftware__",
                workspace=tmp_path,
                config_file=tmp_path / "config.toml",
                admin_config=AdminConfig(),
            )
        )


def test_inspection_reports_missing_tool_and_available_names(tmp_path, monkeypatch) -> None:
    server = McpServerConfig(name="fachsoftware", command="ignored")

    class FakeAgent:
        def __init__(self, *_args, **_kwargs):
            self._server_tools = {
                "fachsoftware": [
                    {"function": {"name": "fachsoftware__search"}},
                    {"function": {"name": "fachsoftware__status"}},
                ]
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(cli_module, "CliAgent", FakeAgent)

    with pytest.raises(ValueError, match="Verfügbare Tools: fachsoftware__search, fachsoftware__status"):
        asyncio.run(
            cli_module._inspect_mcp_tool(
                server=server,
                tool_name="delete",
                workspace=tmp_path,
                config_file=tmp_path / "config.toml",
                admin_config=AdminConfig(),
            )
        )


def test_run_admin_inspect_prints_contract_without_policy_fragment(tmp_path, monkeypatch, capsys) -> None:
    server = McpServerConfig(name="fachsoftware", command="ignored")
    config = AppConfig(mcp_servers=(server,))
    inspection = McpToolInspection(
        server_name="fachsoftware",
        transport="stdio",
        tool_name="search",
        description="Search records",
        input_schema={"type": "object"},
        contract_sha256="abc123",
        trusted_server_found=False,
        existing_contract_sha256=None,
    )
    monkeypatch.setattr(cli_module, "load_config", lambda _path: config)
    monkeypatch.setattr(cli_module, "load_admin_config", lambda: AdminConfig())

    async def fake_inspect(**_kwargs):
        return inspection

    monkeypatch.setattr(cli_module, "_inspect_mcp_tool", fake_inspect)

    asyncio.run(
        cli_module.run_admin(
            SimpleNamespace(
                config=None,
                workspace=tmp_path,
                server="fachsoftware",
                tool="search",
                admin_command="inspect-tool",
                update=False,
            )
        )
    )

    output = capsys.readouterr().out
    assert "MCP-Server: fachsoftware (stdio)" in output
    assert "Beschreibung: Search records" in output
    assert "Contract: abc123" in output
    assert "[[mcp.trusted_servers.auto_approve_tools]]" not in output


def test_run_admin_rejects_missing_workspace_before_inspection(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "load_config", lambda _path: AppConfig())
    monkeypatch.setattr(cli_module, "load_admin_config", lambda: AdminConfig())
    missing = tmp_path / "missing"

    with pytest.raises(ValueError, match="Arbeitsordner existiert nicht"):
        asyncio.run(
            cli_module.run_admin(
                SimpleNamespace(
                    config=None,
                    workspace=missing,
                    server="fachsoftware",
                    tool="search",
                    admin_command="inspect-tool",
                    update=False,
                )
            )
        )


def test_run_admin_rejects_unknown_server(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cli_module, "load_config", lambda _path: AppConfig())
    monkeypatch.setattr(cli_module, "load_admin_config", lambda: AdminConfig())

    with pytest.raises(ValueError, match="nicht definiert"):
        asyncio.run(
            cli_module.run_admin(
                SimpleNamespace(
                    config=None,
                    workspace=tmp_path,
                    server="fachsoftware",
                    tool="search",
                    admin_command="inspect-tool",
                    update=False,
                )
            )
        )


@pytest.mark.parametrize(
    ("trusted", "existing", "message"),
    [
        (False, None, "identitätsgleichen"),
        (True, None, "bereits eine gepinnte Auto-Freigabe"),
    ],
)
def test_run_admin_update_requires_trusted_server_and_existing_pin(
    tmp_path,
    monkeypatch,
    trusted,
    existing,
    message,
) -> None:
    server = McpServerConfig(name="fachsoftware", command="ignored")
    monkeypatch.setattr(cli_module, "load_config", lambda _path: AppConfig(mcp_servers=(server,)))
    monkeypatch.setattr(cli_module, "load_admin_config", lambda: AdminConfig())

    async def fake_inspect(**_kwargs):
        return McpToolInspection(
            server_name="fachsoftware",
            transport="stdio",
            tool_name="search",
            description="",
            input_schema={},
            contract_sha256="new",
            trusted_server_found=trusted,
            existing_contract_sha256=existing,
        )

    monkeypatch.setattr(cli_module, "_inspect_mcp_tool", fake_inspect)

    with pytest.raises(ValueError, match=message):
        asyncio.run(
            cli_module.run_admin(
                SimpleNamespace(
                    config=None,
                    workspace=tmp_path,
                    server="fachsoftware",
                    tool="search",
                    admin_command="trust-tool",
                    update=True,
                )
            )
        )


def test_run_admin_update_warns_when_contract_changed(tmp_path, monkeypatch, capsys) -> None:
    server = McpServerConfig(name="fachsoftware", command="ignored")
    monkeypatch.setattr(cli_module, "load_config", lambda _path: AppConfig(mcp_servers=(server,)))
    monkeypatch.setattr(cli_module, "load_admin_config", lambda: AdminConfig())

    async def fake_inspect(**_kwargs):
        return McpToolInspection(
            server_name="fachsoftware",
            transport="stdio",
            tool_name="search",
            description="",
            input_schema={},
            contract_sha256="new-contract",
            trusted_server_found=True,
            existing_contract_sha256="old-contract",
        )

    monkeypatch.setattr(cli_module, "_inspect_mcp_tool", fake_inspect)

    asyncio.run(
        cli_module.run_admin(
            SimpleNamespace(
                config=None,
                workspace=tmp_path,
                server="fachsoftware",
                tool="search",
                admin_command="trust-tool",
                update=True,
            )
        )
    )

    output = capsys.readouterr().out
    assert "Bisheriger Contract: old-contract" in output
    assert "Tool-Contract hat sich geändert" in output
    assert "Admin-Konfiguration wurde NICHT geändert" in output
    assert "contract_sha256 = \"new-contract\"" in output


def test_run_admin_reports_unchanged_contract(tmp_path, monkeypatch, capsys) -> None:
    server = McpServerConfig(name="fachsoftware", command="ignored")
    monkeypatch.setattr(cli_module, "load_config", lambda _path: AppConfig(mcp_servers=(server,)))
    monkeypatch.setattr(cli_module, "load_admin_config", lambda: AdminConfig())

    async def fake_inspect(**_kwargs):
        return McpToolInspection(
            server_name="fachsoftware",
            transport="stdio",
            tool_name="search",
            description="",
            input_schema={},
            contract_sha256="same-contract",
            trusted_server_found=True,
            existing_contract_sha256="same-contract",
        )

    monkeypatch.setattr(cli_module, "_inspect_mcp_tool", fake_inspect)

    asyncio.run(
        cli_module.run_admin(
            SimpleNamespace(
                config=None,
                workspace=tmp_path,
                server="fachsoftware",
                tool="search",
                admin_command="trust-tool",
                update=True,
            )
        )
    )

    assert "Tool-Contract ist unverändert" in capsys.readouterr().out


def test_run_rejects_missing_workspace_before_agent_construction(tmp_path, monkeypatch) -> None:
    captured = {}
    _patch_runtime(monkeypatch, captured)
    missing = tmp_path / "missing"

    with pytest.raises(ValueError, match="Arbeitsordner existiert nicht"):
        asyncio.run(cli_module.run(_args(missing, prompt=["hello"])))

    assert "constructed" not in captured


def test_interactive_mode_ignores_empty_input_runs_prompt_and_quits(tmp_path, monkeypatch, capsys) -> None:
    captured = {}
    _patch_runtime(monkeypatch, captured)
    answers = iter(["", "hello", "quit"])
    monkeypatch.setattr(builtins, "input", lambda _prompt: next(answers))

    asyncio.run(cli_module.run(_args(tmp_path)))

    assert captured["prompts"] == ["hello"]
    output = capsys.readouterr().out
    assert "Interaktiver Modus" in output
    assert "answer:hello" in output


def test_interactive_mode_reports_agent_error_and_exits_cleanly_on_eof(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    captured = {}
    _patch_runtime(monkeypatch, captured)
    calls = iter(["boom"])

    def fake_input(_prompt):
        try:
            return next(calls)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr(builtins, "input", fake_input)

    asyncio.run(cli_module.run(_args(tmp_path)))

    assert captured["prompts"] == ["boom"]
    assert "Fehler: ValueError: agent failed" in capsys.readouterr().out


def test_interactive_mode_exits_on_keyboard_interrupt(tmp_path, monkeypatch, capsys) -> None:
    captured = {}
    _patch_runtime(monkeypatch, captured)

    def interrupt(_prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", interrupt)
    asyncio.run(cli_module.run(_args(tmp_path)))

    assert captured["prompts"] == []
    assert capsys.readouterr().out.endswith("\n")


def test_main_reports_runtime_error_and_exits_one(monkeypatch, capsys) -> None:
    async def fail(_args):
        raise ValueError("broken run")

    monkeypatch.setattr(cli_module, "run", fail)
    monkeypatch.setattr(sys, "argv", ["cli-agent", "hello"])

    with pytest.raises(SystemExit) as exc_info:
        cli_module.main()

    assert exc_info.value.code == 1
    assert "Fehler: ValueError: broken run" in capsys.readouterr().out


def test_main_dispatches_admin_subcommand(monkeypatch) -> None:
    captured = {}

    async def fake_run_admin(args):
        captured["command"] = args.admin_command
        captured["server"] = args.server
        captured["tool"] = args.tool

    monkeypatch.setattr(cli_module, "run_admin", fake_run_admin)
    monkeypatch.setattr(sys, "argv", ["cli-agent", "admin", "inspect-tool", "srv", "tool"])

    cli_module.main()

    assert captured == {"command": "inspect-tool", "server": "srv", "tool": "tool"}
