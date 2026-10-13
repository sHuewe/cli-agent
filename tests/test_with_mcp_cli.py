from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent import cli as cli_module
from cli_agent.admin_config import AdminConfig, McpPolicy, TrustedMcpServer
from cli_agent.config import AppConfig, McpServerConfig
from cli_agent.execution import (
    ExecutionDependencies,
    OneShotRunOptions,
    apply_named_mcp_overrides,
    run_once,
)


def _policy(*servers: TrustedMcpServer) -> AdminConfig:
    return AdminConfig(mcp=McpPolicy(trusted_servers=servers))


def _stdio(name: str = "validator", access: str = "none") -> TrustedMcpServer:
    return TrustedMcpServer(
        name=name,
        transport="stdio",
        command="/trusted/validator",
        args=("--stdio", "{workspace_directory}"),
        required_workspace_access=access,
    )


def _http(name: str = "fachsoftware") -> TrustedMcpServer:
    return TrustedMcpServer(
        name=name,
        transport="streamable_http",
        url="https://mcp.internal/mcp",
        headers=(("X-Company", "trusted"),),
        bearer_token_env="FACH_TOKEN",
    )


def _select(
    config: AppConfig = AppConfig(),
    *,
    names: tuple[str, ...] = ("validator",),
    access: str | None = None,
    admin: AdminConfig | None = None,
) -> AppConfig:
    return apply_named_mcp_overrides(
        config,
        admin_config=admin or _policy(_stdio()),
        server_names=names,
        workspace_access=access,
    )


def test_parser_supports_repeated_admin_mcp_selection() -> None:
    args = cli_module.build_parser().parse_args(
        ["--with-mcp", "validator", "--with-mcp", "fachsoftware", "--with-os-read"]
    )
    assert args.with_mcp == ["validator", "fachsoftware"]
    assert args.os_access == "read"


def test_no_selection_does_not_modify_config() -> None:
    config = AppConfig(mcp_servers=(McpServerConfig(name="existing"),))
    assert _select(config, names=(), admin=AdminConfig()) is config


def test_stdio_selection_is_name_only_and_does_not_copy_admin_launch_parameters() -> None:
    config = AppConfig()
    selected = _select(config)

    assert config.mcp_servers == ()
    assert selected.mcp_servers == (McpServerConfig(name="validator"),)
    assert selected.mcp_servers[0].command is None
    assert selected.mcp_servers[0].env == {}


def test_http_selection_uses_exact_admin_endpoint_and_headers() -> None:
    selected = _select(
        names=("fachsoftware",),
        admin=_policy(_http()),
    )
    assert selected.mcp_servers == (
        McpServerConfig(
            name="fachsoftware",
            transport="streamable_http",
            url="https://mcp.internal/mcp",
            headers={"X-Company": "trusted"},
        ),
    )
    # Bearer token remains in the admin policy and is resolved only at connection time.
    assert "FACH_TOKEN" not in repr(selected.mcp_servers)


def test_existing_http_server_with_equivalent_url_and_header_case_is_reused() -> None:
    existing = McpServerConfig(
        name="fachsoftware",
        transport="streamable_http",
        url="HTTPS://MCP.INTERNAL./mcp",
        headers={"x-company": "trusted"},
    )
    config = AppConfig(mcp_servers=(existing,))
    selected = _select(
        config,
        names=("fachsoftware",),
        admin=_policy(_http()),
    )
    assert selected.mcp_servers == (existing,)


def test_existing_http_server_with_different_header_value_is_rejected() -> None:
    existing = McpServerConfig(
        name="fachsoftware",
        transport="streamable_http",
        url="https://mcp.internal/mcp",
        headers={"x-company": "TRUSTED"},
    )
    with pytest.raises(ValueError, match="anderen Serveridentität"):
        _select(
            AppConfig(mcp_servers=(existing,)),
            names=("fachsoftware",),
            admin=_policy(_http()),
        )


def test_multiple_servers_extend_existing_configuration_in_order() -> None:
    original = McpServerConfig(name="existing", transport="streamable_http", url="http://localhost:1234/mcp")
    config = AppConfig(mcp_servers=(original,))
    selected = _select(
        config,
        names=("validator", "fachsoftware"),
        admin=_policy(_stdio(), _http()),
    )
    assert [server.name for server in selected.mcp_servers] == [
        "existing", "validator", "fachsoftware"
    ]
    assert config.mcp_servers == (original,)


def test_repeated_selection_is_idempotent() -> None:
    selected = _select(names=("validator", "validator"))
    assert len(selected.mcp_servers) == 1


def test_existing_identical_stdio_server_is_not_duplicated() -> None:
    config = AppConfig(mcp_servers=(McpServerConfig(name="validator"),))
    assert _select(config).mcp_servers == config.mcp_servers


def test_existing_identical_http_server_is_not_duplicated() -> None:
    config = AppConfig(mcp_servers=(
        McpServerConfig(
            name="fachsoftware",
            transport="streamable_http",
            url="https://mcp.internal/mcp",
            headers={"X-Company": "trusted"},
            compress_result=True,
        ),
    ))
    assert _select(
        config,
        names=("fachsoftware",),
        admin=_policy(_http()),
    ).mcp_servers == config.mcp_servers


@pytest.mark.parametrize(
    "server",
    [
        McpServerConfig(name="validator", transport="streamable_http", url="https://wrong/mcp"),
        McpServerConfig(name="validator", command="/user-supplied/server"),
        McpServerConfig(name="validator", built_in=True),
    ],
)
def test_conflicting_stdio_identity_is_rejected(server: McpServerConfig) -> None:
    with pytest.raises(ValueError, match="anderen Serveridentität"):
        _select(AppConfig(mcp_servers=(server,)))


@pytest.mark.parametrize(
    "server",
    [
        McpServerConfig(name="fachsoftware", transport="streamable_http", url="https://elsewhere/mcp"),
        McpServerConfig(name="fachsoftware", transport="streamable_http", url="https://mcp.internal/mcp", headers={"X-Company": "malicious"}),
        McpServerConfig(name="fachsoftware"),
    ],
)
def test_conflicting_http_identity_is_rejected(server: McpServerConfig) -> None:
    with pytest.raises(ValueError, match="anderen Serveridentität"):
        _select(
            AppConfig(mcp_servers=(server,)),
            names=("fachsoftware",),
            admin=_policy(_http()),
        )


@pytest.mark.parametrize("name", ["missing", "validator-typo"])
def test_unknown_servers_fail_closed(name: str) -> None:
    with pytest.raises(ValueError, match="nicht in"):
        _select(names=(name,))


@pytest.mark.parametrize("name", ["os", "OS", "data", "DATA"])
def test_builtin_names_cannot_be_selected(name: str) -> None:
    with pytest.raises(ValueError, match="reserviert"):
        _select(names=(name,), admin=_policy(_stdio(name)))


def test_okf_name_is_reserved_when_knowledge_server_is_configured() -> None:
    from cli_agent.config import OkfConfig

    config = AppConfig(okf=OkfConfig(repository=Path("/knowledge")))
    with pytest.raises(ValueError, match="reserviert"):
        _select(config, names=("okf",), admin=_policy(_stdio("okf")))


@pytest.mark.parametrize("name", ["", " ", "\t"])
def test_empty_server_names_are_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="nicht-leeren"):
        _select(names=(name,))


@pytest.mark.parametrize(
    ("requirement", "granted", "expected_flag"),
    [
        ("read", None, "--with-os-read"),
        ("write", None, "--with-os-write"),
        ("write", "read", "--with-os-write"),
    ],
)
def test_explicit_server_fails_with_actionable_workspace_access_error(
    requirement: str,
    granted: str | None,
    expected_flag: str,
) -> None:
    with pytest.raises(PermissionError, match=expected_flag):
        _select(
            names=("validator",),
            admin=_policy(_stdio(access=requirement)),
            access=granted,
        )


@pytest.mark.parametrize(
    ("requirement", "granted"),
    [
        ("none", None),
        ("read", "read"),
        ("read", "write"),
        ("write", "write"),
    ],
)
def test_server_starts_when_required_access_is_granted(
    requirement: str,
    granted: str | None,
) -> None:
    assert _select(
        admin=_policy(_stdio(access=requirement)),
        access=granted,
    ).mcp_servers == (McpServerConfig(name="validator"),)


def test_one_shot_execution_passes_selected_servers_into_agent(tmp_path: Path) -> None:
    captured: dict[str, object] = {}
    config = AppConfig()

    class FakeAgent:
        def __init__(self, _workspace, _model, mcp_servers, **kwargs):
            captured["servers"] = mcp_servers
            captured["policy"] = kwargs["mcp_policy"]
            captured["workspace_access"] = kwargs["workspace_access"]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def ask(self, prompt):
            return "0" if prompt == "tokens" else "ok"

    admin = _policy(_stdio(access="read"), _http())
    result = asyncio.run(
        run_once(
            OneShotRunOptions(
                workspace=tmp_path,
                prompt="Inspect this project",
                with_mcp=("validator", "fachsoftware"),
                workspace_access="read",
            ),
            dependencies=ExecutionDependencies(
                load_config=lambda _path: config,
                load_admin_config=lambda: admin,
                configure_logging=lambda _config: None,
                create_model_client=lambda *_args, **_kwargs: SimpleNamespace(),
                agent_type=FakeAgent,
            ),
        )
    )

    assert result.answer == "ok"
    assert [server.name for server in captured["servers"]] == [
        "os", "validator", "fachsoftware"
    ]
    assert captured["policy"] == admin.mcp
    assert captured["workspace_access"] == "read"


def test_cli_one_shot_forwards_selected_names(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli_module, "load_config", lambda _path: AppConfig())
    monkeypatch.setattr(cli_module, "load_admin_config", lambda: _policy(_stdio()))
    seen: dict[str, object] = {}

    async def fake_run_once(options, *, dependencies):
        seen["names"] = options.with_mcp
        seen["access"] = options.workspace_access
        return SimpleNamespace(
            answer="ok",
            usage="tokens",
            web_context_statuses=(),
        )

    monkeypatch.setattr(cli_module, "run_once", fake_run_once)
    args = cli_module.build_parser().parse_args(
        ["--workspace", str(tmp_path), "--with-mcp", "validator", "Inspect"]
    )
    asyncio.run(cli_module.run(args))
    assert seen == {"names": ("validator",), "access": None}


def test_cli_interactive_mode_selects_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    monkeypatch.setattr(cli_module, "load_config", lambda _path: AppConfig())
    monkeypatch.setattr(cli_module, "load_admin_config", lambda: _policy(_stdio()))
    monkeypatch.setattr(cli_module, "configure_logging", lambda _config: None)
    monkeypatch.setattr(
        cli_module,
        "create_model_client",
        lambda *_args, **_kwargs: SimpleNamespace(),
    )
    captured: dict[str, object] = {}

    class FakeAgent:
        def __init__(self, _workspace, _model, mcp_servers, **_kwargs):
            captured["servers"] = mcp_servers

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(cli_module, "WebContextCliAgent", FakeAgent)
    monkeypatch.setattr(builtins, "input", lambda _prompt: "quit")
    args = cli_module.build_parser().parse_args(
        ["--workspace", str(tmp_path), "--with-mcp", "validator"]
    )
    asyncio.run(cli_module.run(args))
    assert captured["servers"] == (McpServerConfig(name="validator"),)
