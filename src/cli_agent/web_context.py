from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, unquote_plus, urlencode, urljoin, urlsplit

import httpx
from trafilatura import bare_extraction

from .admin_config import WebProviderConfig
from .network_policy import validate_http_url

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


class _TitleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.in_title = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "title":
            self.in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.parts.append(data)


def _html_title(raw_text: str) -> str | None:
    parser = _TitleParser()
    parser.feed(raw_text)
    return _normalize_text(" ".join(parser.parts)) or None


def _validate_web_url(
    url: str,
    *,
    allowed_hosts: tuple[str, ...] | list[str] = (),
) -> str:
    return validate_http_url(
        url,
        allowed_hosts=allowed_hosts,
        purpose="Web-Kontext",
    )


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

        title = _html_title(raw_text) or _normalize_text(document.title or "") or None
        content = _normalize_text(document.text or "")
        return title, content

    return None, _normalize_text(raw_text)


def _origin(url: str) -> tuple[str, str, int | None]:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    port = parsed.port
    if port is None:
        if parsed.scheme.lower() == "https":
            port = 443
        elif parsed.scheme.lower() == "http":
            port = 80
    return parsed.scheme.lower(), hostname, port


def _provider_matches(url: str, provider: WebProviderConfig) -> bool:
    if _origin(url) != _origin(provider.base_url):
        return False
    requested_path = urlsplit(url).path or "/"
    base_path = urlsplit(provider.base_url).path.rstrip("/") or "/"
    if base_path == "/":
        return True
    return requested_path == base_path or requested_path.startswith(base_path + "/")


def _validate_confluence_credential_url(
    url: str,
    *,
    provider: WebProviderConfig,
    allowed_hosts: tuple[str, ...] | list[str],
) -> str:
    """Validate the exact URL before attaching a Confluence PAT.

    Authenticated provider paths must be unambiguous to both the client and any
    reverse proxy/server.  Reject traversal, backslashes and percent-encoded path
    octets rather than trying to guess how another HTTP component will normalize
    them.  Query encoding remains allowed and is used by the Confluence REST API.
    """

    validated = _validate_web_url(url, allowed_hosts=allowed_hosts)
    if not _provider_matches(validated, provider):
        raise ValueError(
            "Confluence-REST-Request würde den administrativ konfigurierten "
            "Provider-Namensraum verlassen."
        )

    path = urlsplit(validated).path or "/"
    if "\\" in path or "%" in path:
        raise ValueError(
            "Confluence-REST-Pfad ist für einen credential-behafteten Request "
            "nicht eindeutig kanonisch."
        )
    if any(segment in {".", ".."} for segment in path.split("/")):
        raise ValueError(
            "Confluence-REST-Pfad enthält nicht erlaubte Dot-Segmente."
        )
    return validated


def _matching_provider(
    url: str,
    providers: tuple[WebProviderConfig, ...] | list[WebProviderConfig],
) -> WebProviderConfig | None:
    matches = [provider for provider in providers if _provider_matches(url, provider)]
    if not matches:
        return None
    return max(matches, key=lambda provider: len(urlsplit(provider.base_url).path))


def _confluence_relative_path(url: str, provider: WebProviderConfig) -> str:
    requested_path = urlsplit(url).path or "/"
    base_path = urlsplit(provider.base_url).path.rstrip("/") or "/"
    if base_path == "/":
        return requested_path
    relative = requested_path[len(base_path) :]
    return relative or "/"


def _confluence_page_reference(
    url: str,
    provider: WebProviderConfig,
) -> tuple[str, tuple[str, str] | str]:
    parsed = urlsplit(url)
    query = parse_qs(parsed.query)
    page_ids = [value.strip() for value in query.get("pageId", []) if value.strip()]
    if page_ids:
        page_id = page_ids[0]
        if not page_id.isdigit():
            raise ValueError("Confluence pageId muss numerisch sein.")
        return "id", page_id

    path = _confluence_relative_path(url, provider)
    segments = [unquote(segment) for segment in path.split("/") if segment]
    for index, segment in enumerate(segments[:-1]):
        if segment.lower() == "pages" and segments[index + 1].isdigit():
            return "id", segments[index + 1]

    if len(segments) >= 3 and segments[0].lower() == "display":
        space_key = segments[1].strip()
        encoded_title = "/".join(path.split("/")[3:])
        title = unquote_plus(encoded_title).strip()
        if not space_key or not title:
            raise ValueError("Confluence-URL enthält keinen gültigen Space/Page-Titel.")
        return "title", (space_key, title)

    raise ValueError(
        "Die konfigurierte Confluence-URL enthält keine unterstützte Seitenreferenz "
        "(pageId, /pages/<id>/... oder /display/<space>/<title>)."
    )


def _confluence_api_url(
    provider: WebProviderConfig,
    reference: tuple[str, tuple[str, str] | str],
) -> str:
    reference_type, value = reference
    api_root = provider.base_url.rstrip("/") + "/rest/api/content"
    expand = "body.view,body.storage"
    if reference_type == "id":
        return f"{api_root}/{quote(str(value), safe='')}?{urlencode({'expand': expand})}"
    if reference_type != "title" or not isinstance(value, tuple):
        raise ValueError("Ungültige Confluence-Seitenreferenz.")

    space_key, title = value
    return f"{api_root}?{urlencode({'type': 'page', 'spaceKey': space_key, 'title': title, 'expand': expand})}"


async def _read_response_bytes(response: httpx.Response) -> bytes:
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > MAX_WEB_RESPONSE_BYTES:
            raise ValueError(
                "Die Web-Antwort überschreitet das erlaubte Limit von "
                f"{MAX_WEB_RESPONSE_BYTES} Bytes."
            )
        body.extend(chunk)
    return bytes(body)


async def _confluence_get_json(
    url: str,
    *,
    provider: WebProviderConfig,
    allowed_hosts: tuple[str, ...] | list[str],
    token: str,
) -> dict[str, object]:
    timeout = httpx.Timeout(WEB_REQUEST_TIMEOUT_SECONDS)
    request_url = _validate_confluence_credential_url(
        url,
        provider=provider,
        allowed_hosts=allowed_hosts,
    )

    async with httpx.AsyncClient(
        follow_redirects=False,
        timeout=timeout,
        trust_env=False,
    ) as client:
        async with client.stream(
            "GET",
            request_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {token}",
                "User-Agent": "cli-agent/1.0",
            },
        ) as response:
            if response.is_redirect:
                raise ValueError(
                    "Confluence-REST-Redirect wurde aus Credential-Sicherheitsgründen "
                    "abgelehnt; der PAT wird nicht an ein Redirect-Ziel weitergegeben."
                )

            response.raise_for_status()
            media_type = response.headers.get("content-type", "").split(";", 1)[0]
            if media_type.strip().lower() != "application/json":
                raise ValueError(
                    "Confluence REST API lieferte keinen JSON-Inhalt; erhalten: "
                    f"{media_type or '(kein Content-Type)'}."
                )
            body = await _read_response_bytes(response)
            try:
                parsed = json.loads(body.decode(response.encoding or "utf-8", errors="strict"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Confluence REST API lieferte ungültiges JSON.") from exc
            if not isinstance(parsed, dict):
                raise ValueError("Confluence REST API lieferte kein JSON-Objekt.")
            return parsed


def _confluence_document(payload: dict[str, object]) -> tuple[str | None, str]:
    item: dict[str, object]
    results = payload.get("results")
    if isinstance(results, list):
        if not results:
            raise ValueError("Confluence-Seite wurde über die REST API nicht gefunden.")
        if len(results) > 1:
            raise ValueError(
                "Confluence-Seitenauflösung über Space und Titel ist nicht eindeutig."
            )
        candidate = results[0]
        if not isinstance(candidate, dict):
            raise ValueError("Confluence REST API lieferte ein ungültiges Seitenergebnis.")
        item = candidate
    else:
        item = payload

    title_value = item.get("title")
    title = _normalize_text(title_value) if isinstance(title_value, str) else None

    body = item.get("body")
    if not isinstance(body, dict):
        raise ValueError("Confluence REST API lieferte keinen Seiteninhalt.")

    saw_representation = False
    for representation in ("view", "storage"):
        representation_value = body.get(representation)
        if not isinstance(representation_value, dict):
            continue
        value = representation_value.get("value")
        if not isinstance(value, str) or not value.strip():
            continue
        saw_representation = True
        extracted_title, content = _extract_web_content(
            (
                "<html><head><title>"
                f"{escape(title or '')}"
                "</title></head><body>"
                f"{value}"
                "</body></html>"
            ),
            "text/html",
        )
        if content:
            return title or extracted_title, content

    if saw_representation:
        raise ValueError("Die Confluence-Seite enthält keinen verwertbaren Textinhalt.")
    raise ValueError("Confluence REST API lieferte keinen verwertbaren Seiteninhalt.")


async def _fetch_confluence_context(
    requested_url: str,
    *,
    provider: WebProviderConfig,
    allowed_hosts: tuple[str, ...] | list[str],
) -> WebContext:
    token = os.environ.get(provider.token_env, "")
    if not token:
        raise ValueError(
            "Confluence-PAT fehlt. Setze die administrativ konfigurierte "
            f"Umgebungsvariable {provider.token_env!r}."
        )

    reference = _confluence_page_reference(requested_url, provider)
    api_url = _confluence_api_url(provider, reference)
    payload = await _confluence_get_json(
        api_url,
        provider=provider,
        allowed_hosts=allowed_hosts,
        token=token,
    )
    title, content = _confluence_document(payload)

    truncated = len(content) > MAX_WEB_CONTEXT_CHARS
    if truncated:
        content = content[:MAX_WEB_CONTEXT_CHARS].rstrip()

    return WebContext(
        requested_url=requested_url,
        final_url=requested_url,
        title=title,
        content=content,
        fetched_at=datetime.now(UTC).isoformat(),
        truncated=truncated,
    )


async def fetch_web_context(
    url: str,
    *,
    allowed_hosts: tuple[str, ...] | list[str] = (),
    providers: tuple[WebProviderConfig, ...] | list[WebProviderConfig] = (),
) -> WebContext:
    requested_url = _validate_web_url(url, allowed_hosts=allowed_hosts)
    provider = _matching_provider(requested_url, providers)
    if provider is not None:
        if provider.provider_type == "confluence":
            return await _fetch_confluence_context(
                requested_url,
                provider=provider,
                allowed_hosts=allowed_hosts,
            )
        raise ValueError(f"Nicht unterstützter Web-Provider: {provider.provider_type!r}.")

    timeout = httpx.Timeout(WEB_REQUEST_TIMEOUT_SECONDS)

    async with httpx.AsyncClient(
        follow_redirects=False,
        max_redirects=MAX_WEB_REDIRECTS,
        timeout=timeout,
        trust_env=False,
    ) as client:
        current_url = requested_url
        for redirect_count in range(MAX_WEB_REDIRECTS + 1):
            current_url = _validate_web_url(
                current_url,
                allowed_hosts=allowed_hosts,
            )
            async with client.stream(
                "GET",
                current_url,
                headers={
                    "Accept": (
                        "text/html, application/xhtml+xml, text/plain;q=0.9, "
                        "text/*;q=0.8"
                    ),
                    "User-Agent": "cli-agent/1.0",
                },
            ) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("Web-Kontext-Redirect enthält kein Ziel.")
                    if redirect_count >= MAX_WEB_REDIRECTS:
                        raise ValueError("Zu viele Redirects im Web-Kontext.")
                    current_url = urljoin(current_url, location)
                    continue

                response.raise_for_status()
                final_url = _validate_web_url(
                    str(response.url),
                    allowed_hosts=allowed_hosts,
                )

                media_type = response.headers.get("content-type", "").split(";", 1)[0]
                media_type = media_type.strip().lower()
                if not (
                    media_type.startswith("text/")
                    or media_type == "application/xhtml+xml"
                ):
                    raise ValueError(
                        "Web-Kontext benötigt einen textuellen HTTP-Inhalt; "
                        f"erhalten: {media_type or '(kein Content-Type)'}."
                    )

                body = await _read_response_bytes(response)
                encoding = response.encoding or "utf-8"
                raw_text = body.decode(encoding, errors="replace")
                break
        else:
            raise ValueError("Web-Kontext konnte nicht geladen werden.")

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
        fetched_at=datetime.now(UTC).isoformat(),
        truncated=truncated,
    )