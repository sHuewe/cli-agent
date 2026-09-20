from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..filesystem_security import regular_file_has_multiple_links
from .workspace import WorkspacePathError, WorkspaceRoot
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

        repository = cls(
            workspace=workspace,
            max_read_bytes=max_read_bytes,
            max_index_entries=max_index_entries,
        )
        try:
            root_index = workspace.resolve("index.md")
        except WorkspacePathError as exc:
            raise OkfRepositoryError(
                "Der konfigurierte Pfad ist kein gültiges OKF-Repository: "
                "Im Repository-Root muss eine index.md vorhanden sein."
            ) from exc
        if not root_index.is_file():
            raise OkfRepositoryError(
                "Der konfigurierte Pfad ist kein gültiges OKF-Repository: "
                "Im Repository-Root muss eine index.md vorhanden sein."
            )
        # Validate only the explicit root marker. Do not walk the configured
        # directory tree merely to decide whether this is an OKF repository.
        repository._read_text(root_index)
        return repository

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
            links = self._safe_internal_links(content, index_path)
            return {
                "directory": relative_directory,
                "source": self.workspace.relative(index_path),
                "content": content,
                "entries": [],
                "internal_links": links,
                "warnings": warnings,
            }

        entries: list[dict[str, Any]] = []
        inspected_candidates = 0
        visible_entries = sorted(
            (entry for entry in directory.iterdir() if not entry.name.startswith(".")),
            key=lambda entry: (not entry.is_dir(), entry.name.casefold()),
        )
        for entry in visible_entries:
            relative_path = self.workspace.relative(entry)
            try:
                safe_entry = self.workspace.resolve(relative_path)
            except WorkspacePathError:
                warnings.append(
                    "Unsicherer oder nach außen führender Link wurde ausgelassen."
                )
                continue

            is_directory = safe_entry.is_dir()
            is_markdown = (
                safe_entry.is_file()
                and safe_entry.suffix.casefold() == ".md"
                and safe_entry.name != "index.md"
            )
            if not is_directory and not is_markdown:
                continue

            if inspected_candidates >= self.max_index_entries:
                warnings.append(
                    "Index-Prüfung wurde nach "
                    f"{self.max_index_entries} Kandidaten abgeschnitten."
                )
                break
            inspected_candidates += 1

            if is_directory:
                entries.append(
                    {
                        "kind": "directory",
                        "path": relative_path,
                        "next_tool": "knowledge_index",
                    }
                )
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

            metadata = self._concept_metadata(safe_entry)
            if metadata is None:
                # Non-OKF Markdown is intentionally invisible to the knowledge
                # tools. Count it toward the inspection budget so an untrusted
                # directory cannot trigger unbounded full-file parsing.
                continue
            entries.append(self._concept_summary(relative_path, metadata))

        return {
            "directory": relative_directory,
            "source": "synthesized",
            "content": None,
            "entries": entries,
            "internal_links": [],
            "warnings": warnings,
        }

    def knowledge_read(self, path: str) -> dict[str, Any]:
        """Read one validated OKF concept, index, or log document."""
        file_path = self._resolve(path)
        if not file_path.is_file():
            raise OkfRepositoryError(f"Pfad ist keine Datei: {path!r}")
        if file_path.suffix.casefold() != ".md":
            raise OkfRepositoryError(
                "knowledge_read liest ausschließlich OKF-Markdown-Dateien (.md)."
            )

        relative_path = self.workspace.relative(file_path)
        if file_path.name in {"index.md", "log.md"}:
            content = self._read_text(file_path)
            kind = "index" if file_path.name == "index.md" else "log"
            summary: dict[str, Any] | None = None
        else:
            content = self._read_text(file_path)
            try:
                metadata = self._parse_frontmatter(content)
            except OkfRepositoryError as exc:
                raise OkfRepositoryError(
                    "knowledge_read verweigert nicht konformes Markdown; "
                    "OKF-Concepts benötigen gültiges YAML-Frontmatter mit "
                    "nicht-leerem Feld 'type'."
                ) from exc
            kind = "concept"
            summary = self._concept_summary(relative_path, metadata)

        return {
            "path": relative_path,
            "kind": kind,
            "summary": self._json_safe(summary),
            "warning": None,
            "content": content,
            "internal_links": self._safe_internal_links(content, file_path),
        }

    def _safe_internal_links(
        self,
        content: str,
        source_path: Path,
    ) -> list[dict[str, Any]]:
        links = self._extract_internal_links(content, source_path)
        safe_links: list[dict[str, Any]] = []
        validation_cache: dict[Path, bool] = {}

        # Explicit indexes and concepts are bounded just like synthesized
        # indexes. This prevents a small Markdown file with many links from
        # triggering an unbounded number of full-file validation reads.
        for link in links[: self.max_index_entries]:
            if link.get("exists") is False:
                safe_links.append(link)
                continue

            path = link.get("path")
            if not isinstance(path, str):
                continue
            try:
                target = self.workspace.resolve(path)
            except WorkspacePathError:
                continue

            if target.is_dir():
                safe_links.append(link)
                continue

            is_okf = validation_cache.get(target)
            if is_okf is None:
                is_okf = self._is_okf_document(target)
                validation_cache[target] = is_okf
            if is_okf:
                safe_links.append(link)
        return safe_links

    def _is_okf_document(self, file_path: Path) -> bool:
        if not file_path.is_file() or file_path.suffix.casefold() != ".md":
            return False
        if file_path.name in {"index.md", "log.md"}:
            return True
        return self._concept_metadata(file_path) is not None

    def _concept_metadata(self, file_path: Path) -> dict[str, Any] | None:
        if (
            not file_path.is_file()
            or file_path.suffix.casefold() != ".md"
            or file_path.name in {"index.md", "log.md"}
        ):
            return None
        try:
            return self._parse_frontmatter(self._read_text(file_path))
        except OkfRepositoryError:
            return None

    def _resolve(self, path: str) -> Path:
        try:
            return self.workspace.resolve(path)
        except WorkspacePathError as exc:
            raise OkfRepositoryError(str(exc)) from exc

    def _read_text(self, file_path: Path) -> str:
        try:
            if regular_file_has_multiple_links(file_path):
                raise OkfRepositoryError(
                    "OKF-Dateien mit mehreren Hardlinks werden aus "
                    "Sicherheitsgründen nicht gelesen: "
                    f"{self.workspace.relative(file_path)}"
                )
            size = file_path.stat().st_size
        except OkfRepositoryError:
            raise
        except OSError as exc:
            raise OkfRepositoryError(
                "Dateigröße konnte nicht sicher gelesen werden: "
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
                "OKF-Datei ist kein gültiger UTF-8-Text: "
                f"{self.workspace.relative(file_path)}"
            ) from exc
        except OSError as exc:
            raise OkfRepositoryError(
                "OKF-Datei konnte nicht gelesen werden: "
                f"{self.workspace.relative(file_path)}"
            ) from exc
