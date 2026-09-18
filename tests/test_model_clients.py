from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from cli_agent.model import CONTEXT_LIMIT_MARGIN, ContextLimitReachedError, TokenUsage
from cli_agent.ollama import OllamaClient, OllamaError, ctx_large
from cli_agent.openai_client import OpenAIClient, OpenAIError


class FakeResponse:
    def __init__(self, data, *, error: Exception | None = None, headers: dict[str, str] | None = None):
        self._data = data
        self._error = error
        self.headers = httpx.Headers(headers or {})
        self.status_code = 200

    def raise_for_status(self):
        if self._error is not None:
            raise self._error

    def json(self):
        return self._data

    async def aiter_bytes(self, chunk_size: int | None = None):
        body = json.dumps(self._data, ensure_ascii=False).encode("utf-8")
        size = chunk_size or len(body) or 1
        for offset in range(0, len(body), size):
            yield body[offset : offset + size]


class _FakeStreamContext:
    def __init__(self, client, response, error):
        self.client = client
        self.response = response
        self.error = error

    async def __aenter__(self):
        if self.error is not None:
            raise self.error
        return self.response

    async def __aexit__(self, *_args):
        return None


class FakeAsyncClient:
    instances = []
    response = None
    post_error = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.posts = []
        type(self).instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if type(self).post_error is not None:
            raise type(self).post_error
        return type(self).response

    def stream(self, method, url, **kwargs):
        assert method == "POST"
        self.posts.append((url, kwargs))
        return _FakeStreamContext(self, type(self).response, type(self).post_error)


@pytest.fixture(autouse=True)
def reset_fake_async_client():
    FakeAsyncClient.instances = []
    FakeAsyncClient.response = None
    FakeAsyncClient.post_error = None


def test_openai_convert_messages_removes_internal_fields_and_serializes_tool_args() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "x",
            "thinking": "hidden",
            "tool_name": "internal",
            "tool_calls": [
                {
                    "id": "1",
                    "function": {"name": "search", "arguments": {"q": "ä"}},
                }
            ],
        }
    ]

    converted = OpenAIClient._convert_messages(messages)

    assert "thinking" not in converted[0]
    assert "tool_name" not in converted[0]
    assert converted[0]["tool_calls"][0]["function"]["arguments"] == '{"q": "ä"}'
    assert messages[0]["tool_calls"][0]["function"]["arguments"] == {"q": "ä"}


def test_openai_normalize_message_parses_valid_tool_arguments() -> None:
    message = {
        "content": None,
        "tool_calls": [
            {"id": "1", "function": {"name": "search", "arguments": '{"q":"x"}'}}
        ],
    }

    normalized = OpenAIClient._normalize_message(message)

    assert normalized["role"] == "assistant"
    assert normalized["content"] == ""
    assert normalized["tool_calls"][0]["function"]["arguments"] == {"q": "x"}


def test_openai_normalize_message_keeps_invalid_json_argument_string() -> None:
    normalized = OpenAIClient._normalize_message(
        {"tool_calls": [{"function": {"name": "x", "arguments": "not-json"}}]}
    )

    assert normalized["tool_calls"][0]["function"]["arguments"] == "not-json"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"usage": None},
        {"usage": {"prompt_tokens": "10", "completion_tokens": 2}},
        {"usage": {"prompt_tokens": 10, "completion_tokens": None}},
    ],
)
def test_openai_token_usage_rejects_incomplete_or_invalid_usage(data) -> None:
    assert OpenAIClient._token_usage(data) is None


def test_openai_constructor_normalizes_url_and_copies_headers() -> None:
    headers = {"X-Test": "value"}
    client = OpenAIClient(
        base_url="http://localhost:8000/v1/",
        model="m",
        api_key="secret",
        timeout=7.0,
        headers=headers,
    )
    headers["X-Test"] = "changed"

    assert client.base_url == "http://localhost:8000/v1"
    assert client.model == "m"
    assert client.timeout == 7.0
    assert client.headers == {"X-Test": "value"}


def test_openai_context_limit_error_without_usage_uses_generic_message() -> None:
    client = OpenAIClient(
        base_url="http://localhost:8000/v1",
        model="m",
        api_key=None,
        context_length=1000,
    )
    client._context_limit_reached = True

    with pytest.raises(ContextLimitReachedError) as exc_info:
        client._check_context_limit()

    assert "Input des letzten Modellaufrufs" not in str(exc_info.value)


def test_openai_chat_builds_request_headers_tools_and_normalizes_response(monkeypatch) -> None:
    monkeypatch.setattr("cli_agent.openai_client.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeResponse(
        {
            "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15},
            "choices": [
                {
                    "message": {
                        "content": "done",
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "function": {
                                    "name": "search",
                                    "arguments": '{"q":"x"}',
                                },
                            }
                        ],
                    }
                }
            ],
        }
    )
    client = OpenAIClient(
        base_url="http://localhost:8000/v1",
        model="m",
        api_key="secret",
        headers={"X-Custom": "v"},
    )

    result = asyncio.run(
        client.chat(
            [{"role": "user", "content": "hello"}],
            [{"type": "function", "function": {"name": "search"}}],
        )
    )

    instance = FakeAsyncClient.instances[0]
    assert instance.kwargs == {
        "timeout": 120.0,
        "follow_redirects": False,
        "trust_env": False,
    }
    url, kwargs = instance.posts[0]
    assert url == "http://localhost:8000/v1/chat/completions"
    assert kwargs["headers"]["Authorization"] == "Bearer secret"
    assert kwargs["headers"]["Content-Type"] == "application/json"
    assert kwargs["headers"]["X-Custom"] == "v"
    assert kwargs["json"]["tool_choice"] == "auto"
    assert kwargs["json"]["tools"][0]["function"]["name"] == "search"
    assert result["content"] == "done"
    assert result["tool_calls"][0]["function"]["arguments"] == {"q": "x"}
    assert client.last_usage == TokenUsage(12, 3, 15)
    assert client.usage_history == [TokenUsage(12, 3, 15)]


def test_openai_chat_without_api_key_omits_authorization_header(monkeypatch) -> None:
    monkeypatch.setattr("cli_agent.openai_client.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeResponse(
        {"choices": [{"message": {"content": "ok"}}]}
    )
    client = OpenAIClient(
        base_url="http://localhost:8000/v1",
        model="m",
        api_key=None,
    )

    asyncio.run(client.chat([{"role": "user", "content": "hello"}], []))

    headers = FakeAsyncClient.instances[0].posts[0][1]["headers"]
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"


def test_openai_chat_without_tools_omits_tool_fields_and_respects_explicit_auth_header(monkeypatch) -> None:
    monkeypatch.setattr("cli_agent.openai_client.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeResponse(
        {"choices": [{"message": {"content": "ok"}}]}
    )
    client = OpenAIClient(
        base_url="http://localhost:8000/v1",
        model="m",
        api_key="secret",
        headers={"Authorization": "Custom token"},
    )

    asyncio.run(client.chat([{"role": "user", "content": "hello"}], []))

    payload = FakeAsyncClient.instances[0].posts[0][1]["json"]
    headers = FakeAsyncClient.instances[0].posts[0][1]["headers"]
    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert headers["Authorization"] == "Custom token"


def test_openai_chat_wraps_http_error(monkeypatch) -> None:
    monkeypatch.setattr("cli_agent.openai_client.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.post_error = httpx.ConnectError("offline")
    client = OpenAIClient(
        base_url="http://localhost:8000/v1",
        model="m",
        api_key=None,
    )

    with pytest.raises(OpenAIError, match="nicht erreichbar"):
        asyncio.run(client.chat([], []))


def test_openai_chat_rejects_unexpected_response_shape(monkeypatch) -> None:
    monkeypatch.setattr("cli_agent.openai_client.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeResponse({"choices": []})
    client = OpenAIClient(
        base_url="http://localhost:8000/v1",
        model="m",
        api_key=None,
    )

    with pytest.raises(OpenAIError, match="Unerwartete Modellantwort"):
        asyncio.run(client.chat([], []))


def test_openai_chat_discards_response_when_usage_reaches_context_limit(monkeypatch) -> None:
    monkeypatch.setattr("cli_agent.openai_client.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeResponse(
        {
            "usage": {"prompt_tokens": 1500, "completion_tokens": 10},
            "choices": [{"message": {"content": "must be discarded"}}],
        }
    )
    client = OpenAIClient(
        base_url="http://localhost:8000/v1",
        model="m",
        api_key=None,
        context_length=1500 + CONTEXT_LIMIT_MARGIN,
    )

    with pytest.raises(ContextLimitReachedError, match="Modellantwort wurde verworfen"):
        asyncio.run(client.chat([], []))

    assert client._context_limit_reached is True
    assert client.usage_history == [TokenUsage(1500, 10, 1510)]


def test_ollama_convert_messages_removes_only_tool_call_id() -> None:
    messages = [{"role": "tool", "tool_call_id": "abc", "content": "result", "extra": 1}]
    converted = OllamaClient._convert_messages(messages)

    assert converted == [{"role": "tool", "content": "result", "extra": 1}]
    assert messages[0]["tool_call_id"] == "abc"


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"prompt_eval_count": "1", "eval_count": 2},
        {"prompt_eval_count": 1, "eval_count": None},
    ],
)
def test_ollama_token_usage_rejects_invalid_counts(data) -> None:
    assert OllamaClient._token_usage(data) is None


def test_ollama_context_limit_error_without_usage_is_generic() -> None:
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="m",
        context_length=1000,
    )
    client._context_limit_reached = True

    with pytest.raises(ContextLimitReachedError) as exc_info:
        client._check_context_limit()

    assert "Input des letzten Modellaufrufs" not in str(exc_info.value)


def test_ollama_chat_builds_secure_request_and_tracks_usage(monkeypatch) -> None:
    monkeypatch.setattr("cli_agent.ollama.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeResponse(
        {
            "prompt_eval_count": 22,
            "eval_count": 4,
            "message": {"role": "assistant", "content": "done"},
        }
    )
    client = OllamaClient(base_url="http://localhost:11434/", model="m", timeout=9.0)

    result = asyncio.run(
        client.chat(
            [{"role": "tool", "tool_call_id": "abc", "content": "result"}],
            [{"type": "function", "function": {"name": "search"}}],
        )
    )

    instance = FakeAsyncClient.instances[0]
    assert instance.kwargs == {
        "timeout": 9.0,
        "follow_redirects": False,
        "trust_env": False,
    }
    url, kwargs = instance.posts[0]
    assert url == "http://localhost:11434/api/chat"
    assert kwargs["json"]["options"] == {"num_ctx": ctx_large}
    assert kwargs["json"]["messages"] == [{"role": "tool", "content": "result"}]
    assert result == {"role": "assistant", "content": "done"}
    assert client.last_usage == TokenUsage(22, 4, 26)
    assert client.usage_history == [TokenUsage(22, 4, 26)]


def test_ollama_chat_wraps_http_error(monkeypatch) -> None:
    monkeypatch.setattr("cli_agent.ollama.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeResponse(
        {}, error=httpx.HTTPStatusError("500", request=SimpleNamespace(), response=SimpleNamespace())
    )
    client = OllamaClient(base_url="http://localhost:11434", model="m")

    with pytest.raises(OllamaError, match="nicht erreichbar"):
        asyncio.run(client.chat([], []))


def test_ollama_chat_rejects_missing_message(monkeypatch) -> None:
    monkeypatch.setattr("cli_agent.ollama.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeResponse({"prompt_eval_count": 1, "eval_count": 1})
    client = OllamaClient(base_url="http://localhost:11434", model="m")

    with pytest.raises(OllamaError, match="Unerwartete Ollama-Antwort"):
        asyncio.run(client.chat([], []))


def test_ollama_chat_discards_response_at_context_limit(monkeypatch) -> None:
    monkeypatch.setattr("cli_agent.ollama.httpx.AsyncClient", FakeAsyncClient)
    FakeAsyncClient.response = FakeResponse(
        {
            "prompt_eval_count": 2000,
            "eval_count": 1,
            "message": {"content": "discard"},
        }
    )
    client = OllamaClient(
        base_url="http://localhost:11434",
        model="m",
        context_length=2000 + CONTEXT_LIMIT_MARGIN,
    )

    with pytest.raises(ContextLimitReachedError, match="Modellantwort wurde verworfen"):
        asyncio.run(client.chat([], []))

    assert client._context_limit_reached is True
