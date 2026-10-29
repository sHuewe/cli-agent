"""Session-scoped workspace artifacts for opt-in MCP tool results.

The MCP server controls the metadata shown to the model, but never a target
filename or a filesystem operation. Only files created by this store are
eligible for cleanup.
"""

from __future__ import annotations

import json
import logging
import os
import stat
import uuid
from pathlib import Path
from typing import Any

from .config import McpServerConfig
from .os_operations import Workspace, WorkspaceError

logger = logging.getLogger("cli_agent.mcp_result_artifacts")

ONLY_METADATA_TO_LLM_KEY = "io.github.shuewe.cli-agent/only_metadata_to_llm"
METADATA_FOR_LLM_KEY = "io.github.shuewe.cli-agent/meta_data_for_llm"
MAX_ARTIFACT_BYTES = 20_000_000
MAX_SESSION_ARTIFACT_BYTES = 200_000_000
MAX_MODEL_METADATA_CHARS = 100_000


class ArtifactError(RuntimeError):
    """An opted-in tool result cannot safely be represented as an artifact."""


def _protected_paths(servers: tuple[McpServerConfig, ...]) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Honor the path restrictions of all configured built-in workspace MCPs."""
    protected: list[Path] = []
    mutation_protected: list[Path] = []
    for server in servers:
        if server.name not in {"os", "data"} or not server.built_in:
            continue
        arguments = tuple(server.literal_args)
        for index, value in enumerate(arguments[:-1]):
            if value == "--protected-path":
                protected.append(Path(arguments[index + 1]))
            elif value == "--mutation-protected-path":
                mutation_protected.append(Path(arguments[index + 1]))
    return tuple(protected), tuple(mutation_protected)


class McpResultArtifacts:
    def __init__(self, workspace_directory: Path, servers: tuple[McpServerConfig, ...]) -> None:
        protected, mutation_protected = _protected_paths(servers)
        self.workspace = Workspace.from_directory(
            workspace_directory,
            McpServerConfig(name="artifact", command="unused", built_in=True),
            protected_paths=protected,
            mutation_protected_paths=mutation_protected,
        )
        self._directory: Path | None = None
        self._directory_identity: tuple[int, int] | None = None
        self._files: dict[Path, tuple[int, int]] = {}
        self._bytes_written = 0

    def _ensure_directory(self) -> Path:
        if self._directory is not None:
            relative = self._directory.relative_to(self.workspace.directory).as_posix()
            safe_path = self.workspace.resolve_direct_path(relative)
            stat = safe_path.stat(follow_symlinks=False)
            if (stat.st_dev, stat.st_ino) != self._directory_identity:
                raise ArtifactError("Ablageordner wurde während der Session ersetzt.")
            return self._directory
        # No pre-existing workspace directory is created into or removed.
        for _ in range(3):
            relative = f"mcp-data-{uuid.uuid4().hex[:16]}"
            path = self.workspace.resolve_direct_path(relative, must_exist=False)
            self.workspace._reject_protected_mutation(path)
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                continue
            stat = path.stat(follow_symlinks=False)
            self._directory = path
            self._directory_identity = (stat.st_dev, stat.st_ino)
            return path
        raise ArtifactError("Kein eindeutiger Ablageordner für MCP-Daten verfügbar.")

    def save(self, raw_result_text: str, *, structured: bool, text_format: str | None = None) -> str:
        try:
            encoded = raw_result_text.encode("utf-8")
        except UnicodeError as exc:
            raise ArtifactError("MCP-Datensatz enthält ungültigen Unicode-Text.") from exc
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise ArtifactError("MCP-Datensatz überschreitet das Dateigrößenlimit.")
        if self._bytes_written + len(encoded) > MAX_SESSION_ARTIFACT_BYTES:
            raise ArtifactError("MCP-Daten dieser Session überschreiten das Gesamtkontingent.")

        directory = self._ensure_directory()
        # Only a fixed extension may be selected; MCP metadata never controls
        # the directory or basename. Text Markdown can be consumed by Data MCP.
        suffix = ".json" if structured else ".md" if text_format == "markdown" else ".txt"
        relative = (directory / f"r-{uuid.uuid4().hex[:16]}{suffix}")
        relative_name = relative.relative_to(self.workspace.directory).as_posix()
        path = self.workspace.resolve_writable_file(relative_name)
        try:
            # Exclusive creation prevents overwriting an existing user file.
            with path.open("xb") as stream:
                stat = os.fstat(stream.fileno())
                self._files[path] = (stat.st_dev, stat.st_ino)
                stream.write(encoded)
            self._bytes_written += len(encoded)
        except OSError as exc:
            raise ArtifactError("MCP-Datensatz konnte nicht im Workspace gespeichert werden.") from exc
        logger.info("mcp_result_artifact_created path=%s bytes=%d", relative_name, len(encoded))
        return relative_name

    def metadata_result(self, result: Any, raw_result_text: str) -> str | None:
        meta = getattr(result, "meta", None)
        if not isinstance(meta, dict) or meta.get(ONLY_METADATA_TO_LLM_KEY) is not True:
            return None

        metadata = meta.get(METADATA_FOR_LLM_KEY)
        if not isinstance(metadata, dict):
            raise ArtifactError(f"MCP-_meta.{METADATA_FOR_LLM_KEY} muss ein JSON-Objekt sein.")
        try:
            metadata_text = json.dumps(metadata, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise ArtifactError("MCP-Metadaten enthalten ungültige JSON-Werte.") from exc
        if len(metadata_text) > MAX_MODEL_METADATA_CHARS:
            raise ArtifactError("MCP-Metadaten für das LLM sind zu groß.")

        structured_payload = getattr(result, "structuredContent", None)
        export_format = metadata.get("export_format")
        if export_format in {"csv", "json", "markdown"}:
            if not isinstance(structured_payload, dict) or set(structured_payload) != {"export_content"}:
                raise ArtifactError("Export-Artefakt enthält kein gültiges Exportformat.")
            raw_export = structured_payload["export_content"]
            if not isinstance(raw_export, str):
                raise ArtifactError("Export-Artefakt muss Text enthalten.")
            path = self.save_export(raw_export, export_format)
        else:
            path = self.save(
                raw_result_text,
                structured=structured_payload is not None,
                text_format="markdown" if metadata.get("format") == "markdown" else None,
            )
        # workspace_path is reserved for the agent's verified, relative path.
        model_metadata = {**metadata, "workspace_path": path}
        return json.dumps(model_metadata, ensure_ascii=False, allow_nan=False)

    def save_export(self, content: str, suffix: str) -> str:
        """Store generated export bytes, without exposing them in the model response."""
        if suffix not in {"csv", "json", "markdown"}:
            raise ArtifactError("Nicht unterstütztes Exportformat.")
        raw = content.encode("utf-8")
        if len(raw) > MAX_ARTIFACT_BYTES or self._bytes_written + len(raw) > MAX_SESSION_ARTIFACT_BYTES:
            raise ArtifactError("Export überschreitet das Artefaktlimit.")
        directory = self._ensure_directory()
        extension = "md" if suffix == "markdown" else suffix
        relative = (directory / f"export-{uuid.uuid4().hex[:16]}.{extension}").relative_to(self.workspace.directory).as_posix()
        path = self.workspace.resolve_writable_file(relative)
        with path.open("xb") as handle:
            identity = os.fstat(handle.fileno())
            self._files[path] = (identity.st_dev, identity.st_ino)
            handle.write(raw)
        self._bytes_written += len(raw)
        return relative

    def read_registered(self, relative: str) -> bytes:
        """Read an unchanged artifact owned by this session; reject arbitrary paths."""
        if not isinstance(relative, str):
            raise ArtifactError("Ungültiger Artefaktpfad.")
        path = self.workspace.resolve_direct_path(relative)
        identity = self._files.get(path)
        if identity is None:
            raise ArtifactError("Datei ist kein registriertes Session-Artefakt.")
        with path.open("rb") as handle:
            stat = os.fstat(handle.fileno())
            if (stat.st_dev, stat.st_ino) != identity or not stat.S_ISREG(stat.st_mode):
                raise ArtifactError("Session-Artefakt wurde verändert.")
            payload = handle.read(MAX_ARTIFACT_BYTES + 1)
        if len(payload) > MAX_ARTIFACT_BYTES:
            raise ArtifactError("Artefakt überschreitet das Downloadlimit.")
        return payload

    def cleanup(self) -> None:
        """Remove only unchanged files created by this session (no recursive delete)."""
        for path, identity in tuple(self._files.items()):
            try:
                relative = path.relative_to(self.workspace.directory).as_posix()
                safe_path = self.workspace.resolve_direct_path(relative)
                stat = safe_path.stat(follow_symlinks=False)
                if (stat.st_dev, stat.st_ino) != identity:
                    logger.warning("mcp_result_artifact_replaced path=%s", relative)
                    continue
                self.workspace.delete_file(relative)
            except FileNotFoundError:
                pass
            except (OSError, RuntimeError, ValueError):
                logger.exception("mcp_result_artifact_cleanup_failed path=%s", path.name)
        self._files.clear()
        self._bytes_written = 0

        if self._directory is not None:
            try:
                relative = self._directory.relative_to(self.workspace.directory).as_posix()
                safe_path = self.workspace.resolve_direct_path(relative)
                stat = safe_path.stat(follow_symlinks=False)
                if (stat.st_dev, stat.st_ino) == self._directory_identity:
                    safe_path.rmdir()  # Only remove the empty directory we created.
            except FileNotFoundError:
                pass
            except (OSError, RuntimeError, ValueError):
                logger.warning("mcp_result_artifact_directory_not_empty_or_unavailable")
            finally:
                self._directory = None
                self._directory_identity = None
