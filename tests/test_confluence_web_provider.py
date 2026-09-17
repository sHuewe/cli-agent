from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli_agent.web_context as web_context_module
from cli_agent.admin_config import WebProviderConfig, load_admin_config
from cli_agent.network_policy import NetworkConfig
from cli_agent.web_context import (
    WebContext,
    _confluence_api_url,
    _confluence_document,
    _confluence_page_reference,
    _provider_matches,
    fetch_web_context,
)
from cli_agent.web_context_agent import WebContextCliAgent


class RecordingModel:
    model = "test"
    base_url = "test://model"

    async def chat(self, messages, tools, **kwargs):
        return {"role": "assistant", "content": "ok"}


def provider(base_url: str = "https://confluence.internal/wiki") -> WebProviderConfig:
    return WebProviderConfig(
        provider_type="confluence",
        base_url=base_url,
        token_env="CONFLUENCE_PAT",
    )


def test_admin_config_loads_confluence_provider_from_machine_policy(tmp_path: Path) -> None:
    path = tmp_path / "admin.toml"
    path.write_text(
        '''
[network]
web_allowed_hosts = ["CONFLUENCE.INTERNAL."]

[[web.providers]]
type = "confluence"
base_url = "https://CONFLUENCE.INTERNAL./wiki/"
token_env = "CONFLUENCE_PAT"
'''.strip(),
        encoding="utf-8",
    )

    config = load_admin_config(path)

    assert len(config.web.providers) == 1
    configured = config.web.providers[0]
    assert configured.provider_type == "confluence"
    assert configured.base_url == "https://confluence.internal/wiki"
    assert configured.token_env == "CONFLUENCE_PAT"


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            '''
[network]
web_allowed_hosts = ["confluence.internal"]
[[web.providers]]
type = "confluence"
base_url = "http://confluence.internal/wiki"
token_env = "CONFLUENCE_PAT"
''',
            "https",
        ),
        (
            '''
[network]
web_allowed_hosts = ["confluence.internal"]
[[web.providers]]
type = "confluence"
base_url = "https://confluence.internal/wiki"
token_env = "bad-name"
''',
            "Umgebungsvariable",
        ),
        (
            '''
[network]
web_allowed_hosts = []
[[web.providers]]
type = "confluence"
base_url = "https://confluence.internal/wiki"
token_env = "CONFLUENCE_PAT"
''',
            "web_allowed_hosts",
        ),
        (
            '''
[network]
web_allowed_hosts = ["confluence.internal"]
[[web.providers]]
type = "other"
base_url = "https://confluence.internal/wiki"
token_env = "CONFLUENCE_PAT"
''',
            "confluence",
        ),
    ],
)
def test_admin_config_rejects_invalid_web_provider(
    tmp_path: Path,
    body: str,
    message: str,
) -> None:
    path = tmp_path / "admin.toml"
    path.write_text(body.strip(), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_admin_config(path)


def test_provider_matching_requires_exact_origin_and_path_boundary() -> None:
    configured = provider()

    assert _provider_matches(
        "https://confluence.internal/wiki/spaces/ABC/pages/123/Test",
        configured,
    )
    assert not _provider_matches(
        "https://confluence.internal/wiki-evil/spaces/ABC/pages/123/Test",
        configured,
    )
    assert not _provider_matches(
        "https://confluence.internal.evil/wiki/spaces/ABC/pages/123/Test",
        configured,
    )
    assert not _provider_matches(
        "https://confluence.internal:444/wiki/spaces/ABC/pages/123/Test",
        configured,
    )


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://confluence.internal/wiki/pages/viewpage.action?pageId=12345",
            ("id", "12345"),
        ),
        (
            "https://confluence.internal/wiki/spaces/ABC/pages/12345/Page+Title",
            ("id", "12345"),
        ),
        (
            "https://confluence.internal/wiki/display/ABC/Page+Title",
            ("title", ("ABC", "Page Title")),
        ),
    ],
)
def test_confluence_page_reference_supports_common_page_urls(
    url: str,
    expected: tuple[str, object],
) -> None:
    assert _confluence_page_reference(url, provider()) == expected


def test_confluence_api_url_stays_under_configured_base_url() -> None:
    configured = provider()
    url = _confluence_api_url(configured, ("id", "12345"))

    assert url.startswith("https://confluence.internal/wiki/rest/api/content/12345?")
    assert "body.view" in url
    assert "body.storage" in url


def test_confluence_document_prefers_rendered_view_and_extracts_text() -> None:
    title, content = _confluence_document(
        {
            "id": "12345",
            "title": "Internal Docs",
            "body": {
                "view": {"value": "<h1>Overview</h1><p>Rendered content</p>"},
                "storage": {"value": "<p>Storage fallback</p>"},
            },
        }
    )

    assert title == "Internal Docs"
    assert "Overview" in content
    assert "Rendered content" in content
    assert "Storage fallback" not in content


def test_matching_confluence_provider_uses_rest_api_and_environment_pat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = provider()
    captured: dict[str, object] = {}

    async def fake_get_json(url: str, **kwargs: object) -> dict[str, object]:
        captured["url"] = url
        captured.update(kwargs)
        return {
            "id": "12345",
            "title": "Internal Docs",
            "body": {"view": {"value": "<p>Authenticated page</p>"}},
        }

    monkeypatch.setenv("CONFLUENCE_PAT", "secret-token-value")
    monkeypatch.setattr(web_context_module, "_confluence_get_json", fake_get_json)

    context = asyncio.run(
        fetch_web_context(
            "https://confluence.internal/wiki/spaces/ABC/pages/12345/Internal+Docs",
            allowed_hosts=("confluence.internal",),
            providers=(configured,),
        )
    )

    assert "/wiki/rest/api/content/12345?" in str(captured["url"])
    assert captured["token"] == "secret-token-value"
    assert context.title == "Internal Docs"
    assert context.content == "Authenticated page"
    assert "secret-token-value" not in str(context.as_dict())


def test_matching_provider_never_falls_back_when_pat_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONFLUENCE_PAT", raising=False)

    with pytest.raises(ValueError, match="CONFLUENCE_PAT"):
        asyncio.run(
            fetch_web_context(
                "https://confluence.internal/wiki/spaces/ABC/pages/12345/Internal+Docs",
                allowed_hosts=("confluence.internal",),
                providers=(provider(),),
            )
        )


def test_agent_forwards_admin_providers_to_web_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configured = provider()
    captured: dict[str, object] = {}

    async def fake_fetch(url: str, **kwargs: object) -> WebContext:
        captured["url"] = url
        captured.update(kwargs)
        return WebContext(
            requested_url=url,
            final_url=url,
            title="Internal Docs",
            content="Authenticated page",
            fetched_at="2026-09-17T10:00:00+00:00",
        )

    monkeypatch.setattr("cli_agent.web_context_agent.fetch_web_context", fake_fetch)
    agent = WebContextCliAgent(
        tmp_path,
        RecordingModel(),
        (),
        network=NetworkConfig(web_allowed_hosts=("confluence.internal",)),
        web_providers=(configured,),
    )
    agent._exit_stack = SimpleNamespace()

    answer = asyncio.run(
        agent.ask(
            "add_web_context "
            "https://confluence.internal/wiki/spaces/ABC/pages/12345/Internal+Docs"
        )
    )

    assert "Web-Kontext hinzugefügt" in answer
    assert captured["allowed_hosts"] == ("confluence.internal",)
    assert captured["providers"] == (configured,)
