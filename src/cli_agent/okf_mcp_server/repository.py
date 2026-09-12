from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..workspace import WorkspacePathError, WorkspaceRoot
from .repository_support import OkfRepositoryError, RepositoryMetadataMixin


@dataclass(frozen=True)
class OkfRepository(RepositoryMetadataMixin):
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
