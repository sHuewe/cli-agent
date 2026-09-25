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
