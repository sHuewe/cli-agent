from __future__ import annotations

import asyncio
import gzip

import httpx
import pytest

from cli_agent.model_http import (
    ModelResponseTooLargeError,
    read_bounded_json_response,
    read_bounded_response_bytes,
    read_response_prefix,
)


async def _with_stream(response: httpx.Response, callback):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: response),
        trust_env=False,
    ) as client:
        async with client.stream("GET", "https://example.test/data") as streamed:
            return await callback(streamed)


def test_bounded_response_accepts_body_at_limit() -> None:
    body = b"1234567890"
    request = httpx.Request("GET", "https://example.test/data")
    response = httpx.Response(200, content=body, request=request)

    result = asyncio.run(
        _with_stream(
            response,
            lambda streamed: read_bounded_response_bytes(streamed, max_bytes=len(body)),
        )
    )

    assert result == body


def test_bounded_response_rejects_declared_content_length_before_reading_body() -> None:
    request = httpx.Request("GET", "https://example.test/data")
    response = httpx.Response(
        200,
        headers={"Content-Length": "101"},
        content=b"small",
        request=request,
    )

    with pytest.raises(ModelResponseTooLargeError) as exc_info:
        asyncio.run(
            _with_stream(
                response,
                lambda streamed: read_bounded_response_bytes(streamed, max_bytes=100),
            )
        )

    assert exc_info.value.declared_bytes == 101


def test_bounded_response_rejects_chunked_body_above_limit() -> None:
    class ChunkedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"12345"
            yield b"67890"
            yield b"X"

    request = httpx.Request("GET", "https://example.test/data")
    response = httpx.Response(200, stream=ChunkedStream(), request=request)

    with pytest.raises(ModelResponseTooLargeError) as exc_info:
        asyncio.run(
            _with_stream(
                response,
                lambda streamed: read_bounded_response_bytes(streamed, max_bytes=10),
            )
        )

    assert exc_info.value.observed_bytes == 11


def test_bounded_response_counts_decompressed_bytes() -> None:
    decoded = b"A" * 200
    compressed = gzip.compress(decoded)
    request = httpx.Request("GET", "https://example.test/data")
    response = httpx.Response(
        200,
        headers={"Content-Encoding": "gzip"},
        content=compressed,
        request=request,
    )

    with pytest.raises(ModelResponseTooLargeError):
        asyncio.run(
            _with_stream(
                response,
                lambda streamed: read_bounded_response_bytes(streamed, max_bytes=100),
            )
        )


def test_bounded_json_response_parses_after_bounded_read() -> None:
    request = httpx.Request("GET", "https://example.test/data")
    response = httpx.Response(
        200,
        json={"message": "ok"},
        request=request,
    )

    result = asyncio.run(
        _with_stream(
            response,
            lambda streamed: read_bounded_json_response(streamed, max_bytes=1000),
        )
    )

    assert result == {"message": "ok"}


def test_error_prefix_stops_after_requested_bytes() -> None:
    request = httpx.Request("GET", "https://example.test/data")
    response = httpx.Response(500, content=b"0123456789abcdef", request=request)

    body, truncated = asyncio.run(
        _with_stream(
            response,
            lambda streamed: read_response_prefix(streamed, max_bytes=8),
        )
    )

    assert body == b"01234567"
    assert truncated is True
