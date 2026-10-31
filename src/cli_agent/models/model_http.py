from __future__ import annotations

import json
from typing import Any

import httpx


MAX_MODEL_RESPONSE_BYTES = 100_000_000
MODEL_RESPONSE_CHUNK_BYTES = 64 * 1024
MAX_MODEL_ERROR_BODY_BYTES = 32_000


class ModelResponseTooLargeError(RuntimeError):
    """A model endpoint returned more response data than the client accepts."""

    def __init__(
        self,
        *,
        max_bytes: int,
        observed_bytes: int | None = None,
        declared_bytes: int | None = None,
    ) -> None:
        self.max_bytes = max_bytes
        self.observed_bytes = observed_bytes
        self.declared_bytes = declared_bytes
        super().__init__(f"Modellantwort überschreitet das Größenlimit von {max_bytes} Bytes.")


def _declared_content_length(response: httpx.Response) -> int | None:
    raw_value = response.headers.get("content-length")
    if raw_value is None:
        return None
    try:
        value = int(raw_value)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None


async def read_bounded_response_bytes(
    response: httpx.Response,
    *,
    max_bytes: int = MAX_MODEL_RESPONSE_BYTES,
) -> bytes:
    """Read a response without allowing the decoded body to grow unbounded.

    ``Content-Length`` is used only as an early rejection signal. The actual
    streamed, HTTP-decoded bytes are counted as well, so chunked responses and
    compressed responses cannot bypass the application-level limit merely by
    omitting or understating ``Content-Length``.
    """

    if max_bytes < 1:
        raise ValueError("max_bytes muss größer als 0 sein.")

    declared = _declared_content_length(response)
    if declared is not None and declared > max_bytes:
        raise ModelResponseTooLargeError(
            max_bytes=max_bytes,
            declared_bytes=declared,
        )

    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=MODEL_RESPONSE_CHUNK_BYTES):
        next_size = len(body) + len(chunk)
        if next_size > max_bytes:
            raise ModelResponseTooLargeError(
                max_bytes=max_bytes,
                observed_bytes=next_size,
                declared_bytes=declared,
            )
        body.extend(chunk)
    return bytes(body)


async def read_bounded_json_response(
    response: httpx.Response,
    *,
    max_bytes: int = MAX_MODEL_RESPONSE_BYTES,
) -> Any:
    body = await read_bounded_response_bytes(response, max_bytes=max_bytes)
    return json.loads(body)


async def read_response_prefix(
    response: httpx.Response,
    *,
    max_bytes: int = MAX_MODEL_ERROR_BODY_BYTES,
) -> tuple[bytes, bool]:
    """Read at most ``max_bytes`` decoded bytes for diagnostic error details."""

    if max_bytes < 1:
        raise ValueError("max_bytes muss größer als 0 sein.")

    body = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=min(MODEL_RESPONSE_CHUNK_BYTES, max_bytes + 1)):
        remaining = max_bytes - len(body)
        if len(chunk) > remaining:
            body.extend(chunk[:remaining])
            return bytes(body), True
        body.extend(chunk)
        if len(body) == max_bytes:
            return bytes(body), True
    return bytes(body), False
