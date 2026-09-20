from __future__ import annotations

import asyncio
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator

import pytest

import cli_agent.web_context as web_context_module
from cli_agent.web_context import fetch_web_context


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send(self, status: int, body: bytes = b"", **headers: str) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers.items():
            self.send_header(name.replace("_", "-"), value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
        if self.path == "/start":
            self._send(302, Location="/final")
            return
        if self.path == "/final":
            self._send(
                200,
                b"transport test content",
                Content_Type="text/plain; charset=utf-8",
            )
            return
        if self.path.startswith("/redirect/"):
            number = int(self.path.rsplit("/", 1)[1])
            self._send(302, Location=f"/redirect/{number + 1}")
            return
        if self.path == "/oversized":
            self._send(
                200,
                b"x" * 64,
                Content_Type="text/plain; charset=utf-8",
            )
            return
        self._send(404, b"not found", Content_Type="text/plain")


@contextmanager
def _http_server() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_fetch_web_context_follows_real_local_redirect() -> None:
    with _http_server() as base_url:
        context = asyncio.run(
            fetch_web_context(
                f"{base_url}/start",
                allowed_hosts=("127.0.0.1",),
            )
        )

    assert context.requested_url == f"{base_url}/start"
    assert context.final_url == f"{base_url}/final"
    assert context.content == "transport test content"
    assert context.truncated is False


def test_fetch_web_context_revalidates_redirect_target() -> None:
    with _http_server() as base_url:
        port = base_url.rsplit(":", 1)[1]
        redirecting_url = f"{base_url}/start"

        original = _Handler.do_GET

        def redirect_to_localhost(self: _Handler) -> None:
            if self.path == "/start":
                self._send(302, Location=f"http://localhost:{port}/final")
                return
            original(self)

        _Handler.do_GET = redirect_to_localhost
        try:
            with pytest.raises(ValueError, match="Host.*nicht erlaubt"):
                asyncio.run(
                    fetch_web_context(
                        redirecting_url,
                        allowed_hosts=("127.0.0.1",),
                    )
                )
        finally:
            _Handler.do_GET = original


def test_fetch_web_context_enforces_redirect_limit_on_real_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_context_module, "MAX_WEB_REDIRECTS", 2)

    with _http_server() as base_url:
        with pytest.raises(ValueError, match="Zu viele Redirects"):
            asyncio.run(
                fetch_web_context(
                    f"{base_url}/redirect/0",
                    allowed_hosts=("127.0.0.1",),
                )
            )


def test_fetch_web_context_enforces_streaming_response_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_context_module, "MAX_WEB_RESPONSE_BYTES", 16)

    with _http_server() as base_url:
        with pytest.raises(ValueError, match="16 Bytes"):
            asyncio.run(
                fetch_web_context(
                    f"{base_url}/oversized",
                    allowed_hosts=("127.0.0.1",),
                )
            )
