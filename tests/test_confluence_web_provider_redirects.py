from __future__ import annotations

import asyncio

import pytest

import cli_agent.web_context as web_context_module
from cli_agent.admin_config import WebProviderConfig
from cli_agent.web_context import _confluence_get_json


class _RedirectResponse:
    is_redirect = True
    encoding = "utf-8"
    headers = {"location": "https://other.internal/steal"}

    def raise_for_status(self) -> None:
        raise AssertionError("redirect response must not be treated as final")

    async def aiter_bytes(self):
        if False:
            yield b""


class _StreamContext:
    async def __aenter__(self):
        return _RedirectResponse()

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _RecordingClient:
    calls: list[tuple[str, str, dict[str, str]]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method: str, url: str, *, headers: dict[str, str]):
        type(self).calls.append((method, url, headers.copy()))
        return _StreamContext()


def test_confluence_pat_is_not_forwarded_to_cross_origin_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _RecordingClient.calls.clear()
    monkeypatch.setattr(web_context_module.httpx, "AsyncClient", _RecordingClient)
    configured = WebProviderConfig(
        provider_type="confluence",
        base_url="https://confluence.internal/wiki",
        token_env="CONFLUENCE_PAT",
    )

    with pytest.raises(ValueError, match="anderen Origin") as exc_info:
        asyncio.run(
            _confluence_get_json(
                "https://confluence.internal/wiki/rest/api/content/123",
                provider=configured,
                # Deliberately allowlist both hosts. Provider credential binding must
                # still be stricter than the generic Web allowlist.
                allowed_hosts=("confluence.internal", "other.internal"),
                token="super-secret-pat",
            )
        )

    assert len(_RecordingClient.calls) == 1
    method, requested_url, headers = _RecordingClient.calls[0]
    assert method == "GET"
    assert requested_url.startswith("https://confluence.internal/")
    assert headers["Authorization"] == "Bearer super-secret-pat"
    assert "super-secret-pat" not in str(exc_info.value)
