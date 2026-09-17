from __future__ import annotations

import asyncio

import pytest

import cli_agent.web_context as web_context_module
from cli_agent.admin_config import WebProviderConfig
from cli_agent.web_context import (
    _confluence_get_json,
    _validate_confluence_credential_url,
)


class _RedirectResponse:
    is_redirect = True
    encoding = "utf-8"

    def __init__(self, location: str) -> None:
        self.headers = {"location": location}

    def raise_for_status(self) -> None:
        raise AssertionError("redirect response must not be treated as final")

    async def aiter_bytes(self):
        if False:
            yield b""


class _StreamContext:
    def __init__(self, location: str) -> None:
        self.location = location

    async def __aenter__(self):
        return _RedirectResponse(self.location)

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingClient:
    calls: list[tuple[str, str, dict[str, str]]] = []
    redirect_location = "https://other.internal/steal"

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method: str, url: str, *, headers: dict[str, str]):
        type(self).calls.append((method, url, headers.copy()))
        return _StreamContext(type(self).redirect_location)


def _provider() -> WebProviderConfig:
    return WebProviderConfig(
        provider_type="confluence",
        base_url="https://confluence.internal/wiki",
        token_env="CONFLUENCE_PAT",
    )


@pytest.mark.parametrize(
    "redirect_location",
    [
        "https://other.internal/steal",
        "https://confluence.internal/not-confluence/steal",
        "https://confluence.internal/wiki/../other-app",
        "https://confluence.internal/wiki/%2e%2e/other-app",
        "https://confluence.internal/wiki/%2E%2E/other-app",
        "https://confluence.internal/wiki/%252e%252e/other-app",
        "https://confluence.internal/wiki%2f..%2fother-app",
        "https://confluence.internal/wiki%5c..%5cother-app",
        "/wiki/rest/api/content/456",
    ],
)
def test_confluence_pat_is_never_forwarded_to_redirect_target(
    monkeypatch: pytest.MonkeyPatch,
    redirect_location: str,
) -> None:
    _RecordingClient.calls.clear()
    _RecordingClient.redirect_location = redirect_location
    monkeypatch.setattr(web_context_module.httpx, "AsyncClient", _RecordingClient)

    with pytest.raises(ValueError, match="Redirect.*abgelehnt") as exc_info:
        asyncio.run(
            _confluence_get_json(
                "https://confluence.internal/wiki/rest/api/content/123",
                provider=_provider(),
                allowed_hosts=("confluence.internal", "other.internal"),
                token="super-secret-pat",
            )
        )

    # The PAT is used only for the original, internally constructed REST URL.
    # No redirect target receives a second authenticated request.
    assert len(_RecordingClient.calls) == 1
    method, requested_url, headers = _RecordingClient.calls[0]
    assert method == "GET"
    assert requested_url == "https://confluence.internal/wiki/rest/api/content/123"
    assert headers["Authorization"] == "Bearer super-secret-pat"
    assert "super-secret-pat" not in str(exc_info.value)


@pytest.mark.parametrize(
    "url",
    [
        "https://confluence.internal/wiki/../other-app",
        "https://confluence.internal/wiki/./rest/api/content/123",
        "https://confluence.internal/wiki/%2e%2e/other-app",
        "https://confluence.internal/wiki/%2E/other-app",
        "https://confluence.internal/wiki/%252e%252e/other-app",
        "https://confluence.internal/wiki%2f..%2fother-app",
        "https://confluence.internal/wiki%5c..%5cother-app",
        "https://confluence.internal/wiki\\..\\other-app",
    ],
)
def test_confluence_credential_url_rejects_ambiguous_paths(url: str) -> None:
    with pytest.raises(ValueError):
        _validate_confluence_credential_url(
            url,
            provider=_provider(),
            allowed_hosts=("confluence.internal",),
        )


def test_confluence_credential_url_accepts_generated_rest_path() -> None:
    url = (
        "https://confluence.internal/wiki/rest/api/content/123"
        "?expand=body.view%2Cbody.storage"
    )

    assert (
        _validate_confluence_credential_url(
            url,
            provider=_provider(),
            allowed_hosts=("confluence.internal",),
        )
        == url
    )
