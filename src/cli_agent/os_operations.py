from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath

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

SENSITIVE_FILENAMES = frozenset(
    {
        ".aws",
        ".azure",
        ".docker",
        ".git-credentials",
        ".netrc",
        ".npmrc",
        ".pypirc",
        ".ssh",
        "credentials",
        "credentials.json",
        "secrets",
        "secrets.json",
    }
)
SENSITIVE_DIRECTORY_NAMES = frozenset(
    {
        ".aws",
        ".azure",
        ".cli-agent",
        ".docker",
        ".git",
        ".ssh",
    }
)
SENSITIVE_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})
MAX_READ_FILE_BYTES = 1_000_000


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
    ) -> Workspace:
        resolved = directory.resolve()
        if not resolved.is_dir():
            raise WorkspaceError(f"Projekt-Workspace existiert nicht: {resolved}")
        return cls(directory=resolved, config=config)

    def resolve_path(self, path: str, *, must_exist: bool = True) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise WorkspaceError("Der Pfad darf nicht leer sein.")

        raw_path = path.strip()
        candidate = Path(raw_path)
        windows_path = PureWindowsPath(raw_path)
        if candidate.is_absolute() or windows_path.is_absolute() or windows_path.drive:
            raise WorkspaceError("Der Pfad muss relativ zum Projekt-Workspace sein.")

        # Check the lexical path before resolving it. Resolving first would
        # normalize ".." away and make the explicit prohibition ineffective.
        if ".." in PurePath(raw_path).parts or ".." in windows_path.parts:
            raise WorkspaceError("Der Pfad darf '..' nicht enthalten.")

        resolved = (self.directory / candidate).resolve(strict=must_exist)
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
    def _is_sensitive_file(path: Path) -> bool:
        name = path.name.casefold()
        return (
            any(part.casefold() in SENSITIVE_DIRECTORY_NAMES for part in path.parts)
            or name.startswith(".env")
            or name in SENSITIVE_FILENAMES
            or path.suffix.casefold() in SENSITIVE_SUFFIXES
            or ".log." in name
            or name.endswith(".log")
        )

    @staticmethod
    def _existing_line_ending(path: Path) -> str:
        """Return the dominant line ending, defaulting to LF on ties."""
        if not path.exists():
            return "\n"

        data = path.read_bytes()
        crlf_count = data.count(b"\r\n")
        lf_count = data.count(b"\n") - crlf_count
        cr_count = data.count(b"\r") - crlf_count

        if crlf_count > lf_count and crlf_count > cr_count:
            return "\r\n"
        if cr_count > lf_count and cr_count > crlf_count:
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
        if self._is_sensitive_file(file_path):
            raise WorkspaceError(
                "Das Lesen von Secret-/Credential-Dateien ist über den "
                "Workspace-OS-Server nicht erlaubt."
            )
        if not self._is_text_file(file_path):
            raise WorkspaceError(
                f"Dateityp darf nicht als Text gelesen werden: {path!r}"
            )

        try:
            if file_path.stat().st_size > MAX_READ_FILE_BYTES:
                raise WorkspaceError(
                    f"Datei überschreitet das Leselimit von "
                    f"{MAX_READ_FILE_BYTES} Bytes: {path!r}"
                )
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
        if self._is_sensitive_file(src_path):
            raise WorkspaceError(
                "Das Kopieren von Secret-/Credential-Dateien über den "
                "Workspace-OS-Server ist nicht erlaubt."
            )

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
            relative = dir_path.relative_to(self.directory).as_posix()
            return f"Ordner existiert bereits: {relative!r}"

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
        return f"Datei geschrieben: {relative} ({file_path.stat().st_size} Bytes)"
