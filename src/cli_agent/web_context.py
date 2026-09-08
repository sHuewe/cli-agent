from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from trafilatura import bare_extraction


MAX_WEB_RESPONSE_BYTES = 5_000_000
MAX_WEB_CONTEXT_CHARS = 500_000
WEB_REQUEST_TIMEOUT_SECONDS = 20.0
MAX_WEB_REDIRECTS = 5


@dataclass(frozen=True)
class WebContext:
    requested_url: str
    final_url: str
    title: str | None
    content: str
    fetched_at: str
    truncated: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "title": self.title,
            "content": self.content,
            "fetched_at": self.fetched_at,
            "truncated": self.truncated,
        }


def _validate_web_url(url: str) -> str:
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("Web-Kontext unterstützt nur http:// und https:// URLs.")
    if not parsed.hostname:
        raise ValueError("Die URL für den Web-Kontext enthält keinen Hostnamen.")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Credentials in Web-Kontext-URLs werden nicht unterstützt.")

    # Private networks and localhost are intentionally allowed here. The URL is
    # fetched only in response to the explicit add_web_context command.
    return value


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    result: list[str] = []
    previous_blank = False
    for line in lines:
        if not line:
            if result and not previous_blank:
                result.append("")
            previous_blank = True
            continue
        result.append(line)
        previous_blank = False
    return "\n".join(result).strip()


def _extract_web_content(
    raw_text: str,
    media_type: str,
    *,
    url: str | None = None,
) -> tuple[str | None, str]:
    if media_type in {"text/html", "application/xhtml+xml"}:
        document = bare_extraction(
            raw_text,
            url=url,
            include_comments=False,
            include_tables=True,
            include_images=False,
            include_links=False,
            deduplicate=True,
            with_metadata=True,
        )
        if document is None:
            return None, ""

        title = _normalize_text(document.title or "") or None
        content = _normalize_text(document.text or "")
        return title, content

    return None, _normalize_text(raw_text)


async def fetch_web_context(url: str) -> WebContext:
    requested_url = _validate_web_url(url)
    timeout = httpx.Timeout(WEB_REQUEST_TIMEOUT_SECONDS)

    async with httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=MAX_WEB_REDIRECTS,
        timeout=timeout,
    ) as client:
        async with client.stream(
            "GET",
            requested_url,
            headers={
                "Accept": (
                    "text/html, application/xhtml+xml, text/plain;q=0.9, "
                    "text/*;q=0.8"
                ),
                "User-Agent": "cli-agent/1.0",
            },
        ) as response:
            response.raise_for_status()
            final_url = _validate_web_url(str(response.url))

            media_type = response.headers.get("content-type", "").split(";", 1)[0]
            media_type = media_type.strip().lower()
            if not (media_type.startswith("text/") or media_type == "application/xhtml+xml"):
                raise ValueError(
                    "Web-Kontext benötigt einen textuellen HTTP-Inhalt; "
                    f"erhalten: {media_type or '(kein Content-Type)'}."
                )

            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > MAX_WEB_RESPONSE_BYTES:
                    raise ValueError(
                        "Die Web-Antwort überschreitet das erlaubte Limit von "
                        f"{MAX_WEB_RESPONSE_BYTES} Bytes."
                    )
                body.extend(chunk)

            encoding = response.encoding or "utf-8"
            raw_text = body.decode(encoding, errors="replace")

    title, content = _extract_web_content(
        raw_text,
        media_type,
        url=final_url,
    )
    if not content:
        raise ValueError("Die Webseite enthält keinen verwertbaren Textinhalt.")

    truncated = len(content) > MAX_WEB_CONTEXT_CHARS
    if truncated:
        content = content[:MAX_WEB_CONTEXT_CHARS].rstrip()

    return WebContext(
        requested_url=requested_url,
        final_url=final_url,
        title=title,
        content=content,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        truncated=truncated,
    )
