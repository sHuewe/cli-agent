from __future__ import annotations

import asyncio

import httpx
import pytest

from cli_agent.openai_client import (
    MAX_HTTP_ERROR_DETAIL_CHARS,
    OpenAIClient,
    OpenAIError,
    _safe_http_error_detail,
)


class _ResponseContext:
    def __init__(self, response: httpx.Response):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return None


class RejectingAsyncClient:
    response: httpx.Response | None = None

    def __init__(self, **_kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def stream(self, method, url, **_kwargs):
        assert method == "POST"
        assert url.endswith("/chat/completions")
        assert self.response is not None
        return _ResponseContext(self.response)


def _response(status: int, text: str) -> httpx.Response:
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return httpx.Response(status, text=text, request=request)


def test_openai_http_status_error_includes_status_and_provider_body(monkeypatch) -> None:
    monkeypatch.setattr(
        "cli_agent.openai_client.httpx.AsyncClient",
        RejectingAsyncClient,
    )
    RejectingAsyncClient.response = _response(
        400,
        '{"error":{"message":"This request exceeds the model context length"}}',
    )
    client = OpenAIClient(
        base_url="https://openrouter.ai/api/v1",
        model="openai/gpt-oss-120b",
        api_key="secret",
        allowed_hosts=("openrouter.ai",),
    )

    with pytest.raises(OpenAIError) as exc_info:
        asyncio.run(client.chat([{"role": "user", "content": "large prompt"}], []))

    message = str(exc_info.value)
    assert "HTTP 400" in message
    assert "hat die Anfrage" in message
    assert "context length" in message
    assert "nicht erreichbar" not in message
    assert exc_info.value.retryable is False


def test_openai_http_status_error_without_body_still_reports_status(monkeypatch) -> None:
    monkeypatch.setattr(
        "cli_agent.openai_client.httpx.AsyncClient",
        RejectingAsyncClient,
    )
    RejectingAsyncClient.response = _response(429, "")
    client = OpenAIClient(
        base_url="https://openrouter.ai/api/v1",
        model="m",
        api_key="secret",
        allowed_hosts=("openrouter.ai",),
    )

    with pytest.raises(OpenAIError, match="HTTP 429") as exc_info:
        asyncio.run(client.chat([], []))

    assert exc_info.value.retryable is True


def test_http_error_detail_is_bounded_and_terminal_safe() -> None:
    response = _response(
        400,
        "bad\x00detail\r\n" + "x" * (MAX_HTTP_ERROR_DETAIL_CHARS + 500),
    )

    detail = _safe_http_error_detail(response)

    assert "\\x00" in detail
    assert "\x00" not in detail
    assert "\r" not in detail
    assert "Zeichen gekürzt" in detail
    assert len(detail) < MAX_HTTP_ERROR_DETAIL_CHARS + 100
