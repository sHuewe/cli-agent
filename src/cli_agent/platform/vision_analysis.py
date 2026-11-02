"""Optional workspace image analysis through an explicitly approved Vision API."""
from __future__ import annotations

import base64
import os
import re
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import httpx

from cli_agent.models.model_factory import _validate_api_key_env
from cli_agent.platform.workspace_core import WorkspaceError
from cli_agent.security.network_policy import LOCAL_HOSTS, validate_http_url
from cli_agent.settings.admin_config import load_admin_config
from cli_agent.settings.config import ModelConfig

if TYPE_CHECKING:
    from cli_agent.platform.os_operations import Workspace

MAX_IMAGE_BYTES = 8_000_000
MAX_QUESTION_CHARS = 4_000
MAX_ANSWER_CHARS = 32_000
MAX_RESPONSE_BYTES = 256_000
VISION_TIMEOUT_SECONDS = 90.0


@dataclass(frozen=True)
class VisionConfig:
    model: str
    base_url: str
    api_key: str | None


def vision_config_from_environment() -> VisionConfig | None:
    """Return an opt-in configuration, enforcing the admin model-host policy."""
    model = os.getenv("VISION_MODEL", "").strip()
    if not model:
        return None
    if len(model) > 200:
        raise ValueError("VISION_MODEL ist zu lang.")
    base_url = os.getenv("VISION_BASE_URL", "https://openrouter.ai/api/v1").strip()
    parsed = urlsplit(base_url)
    if parsed.query or parsed.fragment:
        raise ValueError("VISION_BASE_URL darf keine Query oder Fragment enthalten.")
    admin = load_admin_config()
    base_url = validate_http_url(
        base_url,
        allowed_hosts=admin.network.model_allowed_hosts,
        purpose="Vision-Modell",
    ).rstrip("/")
    key_env = os.getenv("VISION_API_KEY_ENV", "VISION_API_KEY").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_env):
        raise ValueError("VISION_API_KEY_ENV muss ein Umgebungsvariablenname sein.")
    # Reuse the same credential policy as the ordinary OpenAI-compatible model.
    key = os.getenv(key_env)
    if key:
        _validate_api_key_env(
            ModelConfig(
                provider="openai",
                model=model,
                base_url=base_url,
                api_key_env=key_env,
            ),
            admin.model_credentials,
        )
    elif (parsed.hostname or "").lower().rstrip(".") not in LOCAL_HOSTS:
        raise ValueError(f"Die Vision-Credential-Variable {key_env!r} ist nicht gesetzt.")
    return VisionConfig(model=model, base_url=base_url, api_key=key)


def _image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _read_image(workspace: Workspace, path: str) -> tuple[str, bytes]:
    file_path = workspace.resolve_readable_file(path, direct=True)
    suffix_mime = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }.get(file_path.suffix.casefold())
    if suffix_mime is None:
        raise WorkspaceError("Bildanalyse unterstützt nur PNG, JPEG und WebP.")
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(file_path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            file_stat = os.fstat(handle.fileno())
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
                raise WorkspaceError("Bild muss eine reguläre Datei ohne Hardlinks sein.")
            if file_stat.st_size > MAX_IMAGE_BYTES:
                raise WorkspaceError(f"Bild überschreitet das Limit von {MAX_IMAGE_BYTES} Bytes.")
            data = handle.read(MAX_IMAGE_BYTES + 1)
    except OSError as exc:
        raise WorkspaceError("Bild konnte nicht sicher gelesen werden.") from exc
    if len(data) > MAX_IMAGE_BYTES:
        raise WorkspaceError(f"Bild überschreitet das Limit von {MAX_IMAGE_BYTES} Bytes.")
    if not data or _image_mime(data) != suffix_mime:
        raise WorkspaceError("Bildinhalt entspricht keinem unterstützten Bildformat.")
    return suffix_mime, data


def analyze_image(
    workspace: Workspace,
    path: str,
    question: str,
    vision: VisionConfig,
) -> str:
    """Ask a concrete question about one allowed workspace image."""
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION_CHARS:
        raise WorkspaceError(f"question muss 1 bis {MAX_QUESTION_CHARS} Zeichen enthalten.")
    mime, data = _read_image(workspace, path)
    encoded = base64.b64encode(data).decode("ascii")
    payload = {
        "model": vision.model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}},
            ],
        }],
    }
    headers = {"Content-Type": "application/json"}
    if vision.api_key:
        headers["Authorization"] = f"Bearer {vision.api_key}"
    try:
        with httpx.Client(
            timeout=VISION_TIMEOUT_SECONDS,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            with client.stream(
                "POST",
                vision.base_url + "/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                if not 200 <= response.status_code < 300:
                    raise WorkspaceError(f"Vision-Modell antwortete mit HTTP {response.status_code}.")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > MAX_RESPONSE_BYTES:
                        raise WorkspaceError("Antwort des Vision-Modells ist zu groß.")
                    chunks.append(chunk)
        import json
        reply = json.loads(b"".join(chunks))
        content = reply["choices"][0]["message"]["content"]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
        raise WorkspaceError("Vision-Modell lieferte keine gültige Antwort.") from exc
    if isinstance(content, list):
        if not all(isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str) for item in content):
            raise WorkspaceError("Vision-Antwort enthält nicht unterstützte Inhalte.")
        content = "\n".join(item["text"] for item in content)
    if not isinstance(content, str) or not content.strip():
        raise WorkspaceError("Vision-Modell hat keinen Text zurückgegeben.")
    if len(content) > MAX_ANSWER_CHARS:
        raise WorkspaceError("Vision-Antwort überschreitet das Textlimit.")
    return content
