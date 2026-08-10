from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import yaml
from yaml.constructor import ConstructorError
from yaml.events import AliasEvent

from .workspace import WorkspacePathError, WorkspaceRoot

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
class OkfRepository:
    workspace: WorkspaceRoot
    max_read_bytes: int = 256_000
    max_index_entries: int = 200

    @classmethod
    def from_directory(
        cls,
        directory: Path,
        *,
        max_read_bytes: int = 256_000,
        max_index_entries: int = 200,
    ) -> OkfRepository:
        if max_read_bytes < 1:
            raise OkfRepositoryError("max_read_bytes muss größer als 0 sein.")
        if max_index_entries < 1:
            raise OkfRepositoryError("max_index_entries muss größer als 0 sein.")
        try:
            workspace = WorkspaceRoot.from_directory(directory)
        except WorkspacePathError as exc:
            raise OkfRepositoryError(str(exc)) from exc
        return cls(
            workspace=workspace,
            max_read_bytes=max_read_bytes,
            max_index_entries=max_index_entries,
        )

    def knowledge_index(self, path: str = ".") -> dict[str, Any]:
        """Read or synthesize the progressive index for one OKF directory."""
        directory = self._resolve(path)
        if not directory.is_dir():
            raise OkfRepositoryError(f"Pfad ist kein OKF-Ordner: {path!r}")

        relative_directory = self.workspace.relative(directory)
        unresolved_index_path = directory / "index.md"
        warnings: list[str] = []

        if unresolved_index_path.exists():
            index_path = self._resolve(self.workspace.relative(unresolved_index_path))
        else:
            index_path = unresolved_index_path

        if index_path.is_file():
            content = self._read_text(index_path)
            links = self._extract_internal_links(content, index_path)
            return {
                "directory": relative_directory,
                "source": self.workspace.relative(index_path),
                "content": content,
                "entries": [],
                "internal_links": links,
                "warnings": warnings,
            }

        entries: list[dict[str, Any]] = []
        visible_entries = sorted(
            (entry for entry in directory.iterdir() if not entry.name.startswith(".")),
            key=lambda entry: (not entry.is_dir(), entry.name.casefold()),
        )
        for entry in visible_entries:
            if len(entries) >= self.max_index_entries:
                warnings.append(
                    "Index wurde nach "
                    f"{self.max_index_entries} Einträgen abgeschnitten."
                )
                break

            relative_path = self.workspace.relative(entry)
            try:
                safe_entry = self.workspace.resolve(relative_path)
            except WorkspacePathError:
                warnings.append(
                    f"Unsicherer oder nach außen führender Link wurde ausgelassen: "
                    f"{relative_path}"
                )
                continue

            if safe_entry.is_dir():
                entries.append(
                    {
                        "kind": "directory",
                        "path": relative_path,
                        "next_tool": "knowledge_index",
                    }
                )
                continue

            if not safe_entry.is_file() or safe_entry.suffix.casefold() != ".md":
                continue
            if safe_entry.name == "index.md":
                continue
            if safe_entry.name == "log.md":
                entries.append(
                    {
                        "kind": "log",
                        "path": relative_path,
                        "next_tool": "knowledge_read",
                    }
                )
                continue

            try:
                metadata = self._parse_frontmatter(self._read_text(safe_entry))
                entries.append(self._concept_summary(relative_path, metadata))
            except OkfRepositoryError as exc:
                entries.append(
                    {
                        "kind": "concept",
                        "path": relative_path,
                        "title": safe_entry.stem,
                        "next_tool": "knowledge_read",
                        "warning": str(exc),
                    }
                )

        return {
            "directory": relative_directory,
            "source": "synthesized",
            "content": None,
            "entries": entries,
            "internal_links": [],
            "warnings": warnings,
        }

    def knowledge_read(self, path: str) -> dict[str, Any]:
        """Read one OKF concept, index, or log document."""
        file_path = self._resolve(path)
        if not file_path.is_file():
            raise OkfRepositoryError(f"Pfad ist keine Datei: {path!r}")
        if file_path.suffix.casefold() != ".md":
            raise OkfRepositoryError(
                "knowledge_read liest ausschließlich OKF-Markdown-Dateien (.md)."
            )

        content = self._read_text(file_path)
        relative_path = self.workspace.relative(file_path)
        warning: str | None = None
        if file_path.name == "index.md":
            kind = "index"
            summary: dict[str, Any] | None = None
        elif file_path.name == "log.md":
            kind = "log"
            summary = None
        else:
            kind = "concept"
            try:
                metadata = self._parse_frontmatter(content)
                summary = self._concept_summary(relative_path, metadata)
            except OkfRepositoryError as exc:
                summary = {
                    "kind": "concept",
                    "path": relative_path,
                    "title": file_path.stem,
                }
                warning = str(exc)

        return {
            "path": relative_path,
            "kind": kind,
            "summary": self._json_safe(summary),
            "warning": warning,
            "content": content,
            "internal_links": self._extract_internal_links(content, file_path),
        }

    def _resolve(self, path: str) -> Path:
        try:
            return self.workspace.resolve(path)
        except WorkspacePathError as exc:
            raise OkfRepositoryError(str(exc)) from exc

    def _read_text(self, file_path: Path) -> str:
        try:
            size = file_path.stat().st_size
        except OSError as exc:
            raise OkfRepositoryError(
                f"Dateigröße konnte nicht gelesen werden: "
                f"{self.workspace.relative(file_path)}"
            ) from exc
        if size > self.max_read_bytes:
            raise OkfRepositoryError(
                f"OKF-Datei ist mit {size} Bytes größer als das konfigurierte "
                f"Limit von {self.max_read_bytes} Bytes: "
                f"{self.workspace.relative(file_path)}"
            )
        try:
            return file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise OkfRepositoryError(
                f"OKF-Datei ist kein gültiger UTF-8-Text: "
                f"{self.workspace.relative(file_path)}"
            ) from exc
        except OSError as exc:
            raise OkfRepositoryError(
                f"OKF-Datei konnte nicht gelesen werden: "
                f"{self.workspace.relative(file_path)}"
            ) from exc

    @staticmethod
    def _parse_frontmatter(content: str) -> dict[str, Any]:
        match = FRONTMATTER_PATTERN.match(content)
        if match is None:
            raise OkfRepositoryError(
                "OKF-Konzept besitzt kein gültig begrenztes YAML-Frontmatter."
            )
        try:
            parsed = yaml.load(match.group("yaml"), Loader=_NoAliasSafeLoader)
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
                today = datetime.now(timezone.utc).astimezone().date()
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
