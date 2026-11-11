from __future__ import annotations

import asyncio

import httpx
import pytest

from cli_agent.models.model import ModelRetryPolicy, RetryingModelClient
from cli_agent.models.openai_client import (
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
        "cli_agent.models.openai_client.httpx.AsyncClient",
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
        "cli_agent.models.openai_client.httpx.AsyncClient",
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
    assert "characters truncated" in detail
    assert len(detail) < MAX_HTTP_ERROR_DETAIL_CHARS + 100



class QueueingAsyncClient(RejectingAsyncClient):
    responses: list[httpx.Response] = []
    calls = 0

    def stream(self, method, url, **kwargs):
        type(self).calls += 1
        assert type(self).responses, "Unexpected additional model request"
        return _ResponseContext(type(self).responses.pop(0))


def _client() -> OpenAIClient:
    return OpenAIClient(
        base_url="https://openrouter.ai/api/v1",
        model="openai/gpt-oss-120b",
        api_key="secret",
        allowed_hosts=("openrouter.ai",),
    )


@pytest.mark.parametrize("status", [408, 504, 524])
def test_server_timeout_is_retried_as_one_model_call(monkeypatch, status):
    monkeypatch.setattr(
        "cli_agent.models.openai_client.httpx.AsyncClient",
        QueueingAsyncClient,
    )
    QueueingAsyncClient.calls = 0
    QueueingAsyncClient.responses = [
        _response(status, '{"error":{"message":"provider timed out"}}'),
        _response(200, '{"choices":[{"message":{"content":"ok"}}]}'),
    ]
    raw = _client()
    wrapper = RetryingModelClient(
        raw, ModelRetryPolicy(
            max_attempts=3,
            initial_delay_seconds=0,
            max_delay_seconds=0,
        ),
    )
    messages = [{"role": "user", "content": "question"}]
    assert asyncio.run(wrapper.chat(messages, []))["content"] == "ok"
    assert QueueingAsyncClient.calls == 2
    assert raw.usage_history == [None]
    assert messages == [{"role": "user", "content": "question"}]


def test_http_408_does_not_retry_when_disabled(monkeypatch):
    monkeypatch.setattr(
        "cli_agent.models.openai_client.httpx.AsyncClient",
        QueueingAsyncClient,
    )
    QueueingAsyncClient.calls = 0
    QueueingAsyncClient.responses = [
        _response(408, '{"error":{"message":"timed out"}}'),
    ]
    wrapper = RetryingModelClient(
        _client(), ModelRetryPolicy(max_attempts=1),
    )
    with pytest.raises(OpenAIError, match="HTTP 408") as exc:
        asyncio.run(wrapper.chat([], []))
    assert exc.value.retryable
    assert QueueingAsyncClient.calls == 1


def test_http_400_context_rejection_is_not_retried(monkeypatch):
    monkeypatch.setattr(
        "cli_agent.models.openai_client.httpx.AsyncClient",
        QueueingAsyncClient,
    )
    QueueingAsyncClient.calls = 0
    QueueingAsyncClient.responses = [
        _response(400, '{"error":{"message":"context window exceeded"}}'),
    ]
    wrapper = RetryingModelClient(
        _client(), ModelRetryPolicy(
            max_attempts=3, initial_delay_seconds=0, max_delay_seconds=0
        ),
    )
    with pytest.raises(OpenAIError, match="HTTP 400") as exc:
        asyncio.run(wrapper.chat([], []))
    assert exc.value.retryable is False
    assert QueueingAsyncClient.calls == 1


def test_http_200_embedded_provider_timeout_is_retried(monkeypatch):
    monkeypatch.setattr(
        "cli_agent.models.openai_client.httpx.AsyncClient",
        QueueingAsyncClient,
    )
    QueueingAsyncClient.calls = 0
    QueueingAsyncClient.responses = [
        _response(200, '{"error":{"code":408,"message":"Gateway timeout"}}'),
        _response(200, '{"choices":[{"message":{"content":"ok"}}]}'),
    ]
    raw = _client()
    wrapper = RetryingModelClient(
        raw, ModelRetryPolicy(
            max_attempts=3, initial_delay_seconds=0, max_delay_seconds=0
        ),
    )
    assert asyncio.run(wrapper.chat([], []))["content"] == "ok"
    assert QueueingAsyncClient.calls == 2
    assert raw.usage_history == [None]


def test_http_200_embedded_bad_request_is_not_retried(monkeypatch):
    monkeypatch.setattr(
        "cli_agent.models.openai_client.httpx.AsyncClient",
        QueueingAsyncClient,
    )
    QueueingAsyncClient.calls = 0
    QueueingAsyncClient.responses = [
        _response(200, '{"error":{"code":400,"message":"bad prompt"}}'),
    ]
    wrapper = RetryingModelClient(
        _client(), ModelRetryPolicy(
            max_attempts=3, initial_delay_seconds=0, max_delay_seconds=0
        ),
    )
    with pytest.raises(OpenAIError) as exc:
        asyncio.run(wrapper.chat([], []))
    assert exc.value.retryable is False
    assert QueueingAsyncClient.calls == 1
