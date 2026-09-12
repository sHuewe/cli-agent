from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent

from ..workspace import WorkspacePathError, WorkspaceRoot

FRONTMATTER_PATTERN = re.compile(
    r"\A---[ \t]*\r?\n(?P<yaml>.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.DOTALL,
)
MARKDOWN_LINK_PATTERN = re.compile(r"(?<!!)\[(?P<label>[^\]]+)\]\((?P<target>[^)]+)\)")


class OkfRepositoryError(RuntimeError):
    """An OKF repository operation failed."""


class _NoAliasSafeLoader(yaml.SafeLoader):
    """Safe YAML loader that also rejects alias expansion."""

    def compose_node(self, parent: Any, index: Any) -> Any:
        if self.check_event(AliasEvent):
            event = self.get_event()
            raise ConstructorError(
                None,
                None,
                "YAML aliases are not supported in OKF frontmatter",
                event.start_mark,
            )
        return super().compose_node(parent, index)


@dataclass(frozen=True)
class RepositoryMetadataMixin:
    """Markdown, frontmatter, and link helpers for :class:`OkfRepository`."""

    workspace: WorkspaceRoot
    max_read_bytes: int = 256_000
    max_index_entries: int = 200

    @staticmethod
    def _parse_frontmatter(content: str) -> dict[str, Any]:
        match = FRONTMATTER_PATTERN.match(content)
        if match is None:
            raise OkfRepositoryError(
                "OKF-Konzept besitzt kein gültig begrenztes YAML-Frontmatter."
            )
        try:
            # The custom SafeLoader rejects aliases; Bandit cannot infer that
            # guarantee from the generic yaml.load call.
            parsed = yaml.load(  # nosec B506
                match.group("yaml"),
                Loader=_NoAliasSafeLoader,
            )
        except yaml.YAMLError as exc:
            raise OkfRepositoryError(
                f"YAML-Frontmatter konnte nicht gelesen werden: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise OkfRepositoryError(
                "YAML-Frontmatter muss eine Zuordnung von Schlüsseln zu Werten sein."
            )
        concept_type = parsed.get("type")
        if not isinstance(concept_type, str) or not concept_type.strip():
            raise OkfRepositoryError(
                "OKF-Konzept besitzt kein nicht-leeres Frontmatter-Feld 'type'."
            )
        return parsed

    def _extract_internal_links(
        self,
        content: str,
        source_path: Path,
    ) -> list[dict[str, Any]]:
        links: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for match in MARKDOWN_LINK_PATTERN.finditer(content):
            raw_target = self._strip_markdown_link_title(match.group("target"))
            if not raw_target or raw_target.startswith("#"):
                continue

            parsed = urlsplit(raw_target)
            if parsed.scheme or parsed.netloc:
                continue
            decoded_path = unquote(parsed.path)
            if not decoded_path:
                continue

            if decoded_path.startswith("/"):
                candidate = self.workspace.directory / decoded_path.lstrip("/")
            else:
                candidate = source_path.parent / decoded_path
            try:
                resolved = candidate.resolve(strict=False)
                relative = self.workspace.relative(resolved)
            except (OSError, RuntimeError, WorkspacePathError):
                continue

            if resolved.is_dir():
                kind = "directory"
                next_tool = "knowledge_index"
            elif Path(relative).suffix.casefold() == ".md":
                kind = "document"
                next_tool = "knowledge_read"
            else:
                # The read-only knowledge tools intentionally expose Markdown
                # documents and OKF directories only.
                continue
            key = (match.group("label"), relative)
            if key in seen:
                continue
            seen.add(key)
            exists = resolved.exists()
            links.append(
                {
                    "label": match.group("label"),
                    "path": relative,
                    "kind": kind,
                    "exists": exists,
                    "next_tool": next_tool,
                }
            )
        return links

    @staticmethod
    def _strip_markdown_link_title(target: str) -> str:
        value = target.strip()
        if value.startswith("<") and value.endswith(">"):
            return value[1:-1]
        # OKF index links normally have no title. Supporting the standard
        # optional title keeps a title from becoming part of the file path.
        title_match = re.match(r"^(\S+)(?:\s+[\"'].*[\"'])$", value)
        return title_match.group(1) if title_match else value

    @classmethod
    def _concept_summary(
        cls,
        relative_path: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        title = metadata.get("title")
        if not isinstance(title, str) or not title.strip():
            title = Path(relative_path).stem
        description = metadata.get("description")
        if not isinstance(description, str):
            description = None
        elif len(description) > 500:
            description = description[:497] + "..."

        tags = metadata.get("tags")
        if not isinstance(tags, list):
            tags = []
        tags = [str(tag) for tag in tags[:20]]

        status = metadata.get("status", "stable")
        stale_after = metadata.get("stale_after")
        stale_after_text = cls._scalar_text(stale_after)
        is_stale = False
        if stale_after_text:
            try:
                today = datetime.now(UTC).astimezone().date()
                is_stale = today >= date.fromisoformat(stale_after_text)
            except ValueError:
                pass

        return {
            "kind": "concept",
            "path": relative_path,
            "concept_id": relative_path.removesuffix(".md"),
            "type": str(metadata["type"]),
            "title": title,
            "description": description,
            "tags": tags,
            "status": str(status),
            "stale_after": stale_after_text,
            "is_stale": is_stale,
            "trust_tier": cls._trust_tier(metadata.get("verified")),
            "next_tool": "knowledge_read",
        }

    @staticmethod
    def _trust_tier(verified: Any) -> str:
        if not verified:
            return "unverified"
        events = verified if isinstance(verified, list) else [verified]
        actors = [
            event.get("by")
            for event in events
            if isinstance(event, dict) and isinstance(event.get("by"), str)
        ]
        if any(actor.startswith("human:") for actor in actors):
            return "human-reviewed"
        return "machine-confirmed"

    @staticmethod
    def _scalar_text(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return str(value)

    @classmethod
    def _json_safe(cls, value: Any) -> Any:
        if isinstance(value, dict):
            return {str(key): cls._json_safe(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._json_safe(item) for item in value]
        if isinstance(value, tuple):
            return [cls._json_safe(item) for item in value]
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        return str(value)
