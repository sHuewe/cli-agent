from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePath
import shutil

from .config import McpServerConfig


TEXT_SUFFIXES = frozenset(
    {
        ".bat",
        ".c",
        ".cfg",
        ".conf",
        ".cpp",
        ".css",
        ".csv",
        ".env",
        ".gitignore",
        ".h",
        ".hpp",
        ".html",
        ".ini",
        ".java",
        ".js",
        ".json",
        ".kt",
        ".kts",
        ".log",
        ".log.1",
        ".log.2",
        ".md",
        ".properties",
        ".py",
        ".sh",
        ".sql",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".xml",
        ".yaml",
        ".yml",
    }
)

TEXT_FILENAMES = frozenset(
    {
        ".env",
        ".gitignore",
        "dockerfile",
        "makefile",
    }
)


class WorkspaceError(RuntimeError):
    """An OS operation could not be performed inside the workspace."""


@dataclass(frozen=True)
class Workspace:
    directory: Path
    config: McpServerConfig

    @classmethod
    def from_directory(
        cls,
        directory: Path,
        config: McpServerConfig,
    ) -> "Workspace":
        resolved = directory.resolve()
        if not resolved.is_dir():
            raise WorkspaceError(
                f"Projekt-Workspace existiert nicht: {resolved}"
            )
        return cls(directory=resolved, config=config)

    def resolve_path(self, path: str, *, must_exist: bool = True) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise WorkspaceError("Der Pfad darf nicht leer sein.")

        candidate = Path(path)
        if candidate.is_absolute():
            raise WorkspaceError(
                "Der Pfad muss relativ zum Projekt-Workspace sein."
            )

        # Check the lexical path before resolving it. Resolving first would
        # normalize ".." away and make the explicit prohibition ineffective.
        if ".." in PurePath(path).parts:
            raise WorkspaceError("Der Pfad darf '..' nicht enthalten.")

        resolved = (self.directory / candidate).resolve(
            strict=must_exist
        )
        try:
            resolved.relative_to(self.directory)
        except ValueError as exc:
            # Also blocks symlinks that point outside the workspace.
            raise WorkspaceError(
                "Der Pfad verweist außerhalb des Projekt-Workspaces."
            ) from exc

        return resolved

    @staticmethod
    def _is_text_file(path: Path) -> bool:
        name = path.name.lower()
        return name in TEXT_FILENAMES or path.suffix.lower() in TEXT_SUFFIXES

    @staticmethod
    def _existing_line_ending(path: Path) -> str:
        """Return the line ending used by an existing file, defaulting to LF."""
        if not path.exists():
            return "\n"

        data = path.read_bytes()
        if b"\r\n" in data:
            return "\r\n"
        if b"\n" in data:
            return "\n"
        if b"\r" in data:
            return "\r"
        return "\n"

    def list_files(self, path: str) -> str:
        directory = self.resolve_path(path)
        if not directory.is_dir():
            raise WorkspaceError(f"Pfad ist kein Ordner: {path!r}")

        entries = sorted(
            directory.iterdir(),
            key=lambda entry: (not entry.is_dir(), entry.name.lower()),
        )
        if not entries:
            return "(Ordner ist leer)"

        lines: list[str] = []
        for entry in entries:
            relative = entry.relative_to(self.directory).as_posix()
            kind = "directory" if entry.is_dir() else "file"
            lines.append(f"{kind}\t{relative}")
        return "\n".join(lines)

    def read_file(self, path: str) -> str:
        file_path = self.resolve_path(path)
        if not file_path.is_file():
            raise WorkspaceError(f"Pfad ist keine Datei: {path!r}")
        if not self._is_text_file(file_path):
            raise WorkspaceError(
                f"Dateityp darf nicht als Text gelesen werden: {path!r}"
            )

        try:
            res = file_path.read_text(encoding="utf-8")
            if not res or len(res) == 0:
                res = "(Empty file)"
            return res
        except UnicodeDecodeError as exc:
            raise WorkspaceError(
                f"Datei ist nicht als UTF-8-Text lesbar: {path!r}"
            ) from exc
        except OSError as exc:
            raise WorkspaceError(
                f"Datei konnte nicht gelesen werden: {path!r}: {exc}"
            ) from exc

    def delete_file(self, path: str) -> str:
        file_path = self.resolve_path(path)
        if not file_path.is_file():
            raise WorkspaceError(f"Pfad ist keine Datei: {path!r}")

        try:
            file_path.unlink()
        except OSError as exc:
            raise WorkspaceError(
                f"Datei konnte nicht gelöscht werden: {path!r}: {exc}"
            ) from exc

        relative = file_path.relative_to(self.directory).as_posix()
        return f"Datei gelöscht: {relative}"

    def copy_file(self, path_src: str, path_dst: str) -> str:
        src_path = self.resolve_path(path_src)
        if not src_path.is_file():
            raise WorkspaceError(f"Quellpfad ist keine Datei: {path_src!r}")

        dst_path = self.resolve_path(path_dst, must_exist=False)
        if dst_path.exists() and not dst_path.is_file():
            raise WorkspaceError(f"Zielpfad ist keine Datei: {path_dst!r}")
        if not dst_path.parent.is_dir():
            raise WorkspaceError(
                f"Zielordner existiert nicht: "
                f"{dst_path.parent.relative_to(self.directory).as_posix()!r}"
            )

        shutil.copy2(src_path, dst_path)

        relative = dst_path.relative_to(self.directory).as_posix()
        return f"Datei kopiert: {relative} "

    def make_directory(self, path: str) -> str:
        dir_path = self.resolve_path(path, must_exist=False)
        if dir_path.exists() and not dir_path.is_dir():
            raise WorkspaceError(f"Pfad ist kein Ordner: {path!r}")
        if dir_path.exists():
            return f"Ordner existiert bereits: {dir_path.relative_to(self.directory).as_posix()!r}"

        try:
            dir_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise WorkspaceError(
                f"Ordner konnte nicht erstellt werden: {path!r}: {exc}"
            ) from exc

        relative = dir_path.relative_to(self.directory).as_posix()
        return f"Ordner erstellt: {relative}"

    def write_file(self, path: str, content: str) -> str:
        file_path = self.resolve_path(path, must_exist=False)
        if file_path.exists() and not file_path.is_file():
            raise WorkspaceError(f"Pfad ist keine Datei: {path!r}")
        if not self._is_text_file(file_path):
            raise WorkspaceError(
                f"Dateityp darf nicht als Text geschrieben werden: {path!r}"
            )
        if not file_path.parent.is_dir():
            raise WorkspaceError(
                f"Zielordner existiert nicht: "
                f"{file_path.parent.relative_to(self.directory).as_posix()!r}"
            )

        try:
            newline = self._existing_line_ending(file_path)
            normalized_content = content.replace("\r\n", "\n").replace("\r", "\n")
            file_path.write_text(
                normalized_content,
                encoding="utf-8",
                newline=newline,
            )
        except OSError as exc:
            raise WorkspaceError(
                f"Datei konnte nicht geschrieben werden: {path!r}: {exc}"
            ) from exc

        relative = file_path.relative_to(self.directory).as_posix()
        return (
            f"Datei geschrieben: {relative} "
            f"({file_path.stat().st_size} Bytes)"
        )
