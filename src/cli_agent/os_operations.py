from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .config import McpServerConfig
from .filesystem_security import regular_file_has_multiple_links

TEXT_SUFFIXES = frozenset(
    {
        ".adoc",
        ".asm",
        ".bash",
        ".bat",
        ".c",
        ".cc",
        ".cfg",
        ".cjs",
        ".cmake",
        ".conf",
        ".cpp",
        ".cs",
        ".csproj",
        ".css",
        ".csv",
        ".cxx",
        ".dart",
        ".editorconfig",
        ".env",
        ".fish",
        ".fs",
        ".fsi",
        ".fsproj",
        ".fsx",
        ".gitattributes",
        ".gitignore",
        ".go",
        ".gql",
        ".gradle",
        ".graphql",
        ".groovy",
        ".h",
        ".hcl",
        ".hh",
        ".hpp",
        ".html",
        ".htm",
        ".hxx",
        ".ini",
        ".ipynb",
        ".j2",
        ".java",
        ".js",
        ".json",
        ".jsp",
        ".jsx",
        ".kt",
        ".kts",
        ".less",
        ".lock",
        ".log",
        ".log.1",
        ".log.2",
        ".lua",
        ".m",
        ".md",
        ".mjs",
        ".mm",
        ".mod",
        ".php",
        ".pl",
        ".pm",
        ".properties",
        ".proto",
        ".py",
        ".ps1",
        ".r",
        ".rb",
        ".rs",
        ".rst",
        ".s",
        ".sass",
        ".sc",
        ".scala",
        ".scss",
        ".sh",
        ".sln",
        ".slnx",
        ".sql",
        ".sum",
        ".svelte",
        ".swift",
        ".targets",
        ".tex",
        ".tf",
        ".tfvars",
        ".toml",
        ".ts",
        ".tsx",
        ".txt",
        ".vb",
        ".vbproj",
        ".vue",
        ".xml",
        ".yaml",
        ".yml",
        ".zsh",
    }
)

TEXT_FILENAMES = frozenset(
    {
        ".dockerignore",
        ".editorconfig",
        ".env",
        ".gitattributes",
        ".gitignore",
        "changelog",
        "dockerfile",
        "gemfile",
        "gradlew",
        "jenkinsfile",
        "license",
        "makefile",
        "mvnw",
        "notice",
        "procfile",
        "rakefile",
        "readme",
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
MAX_PDF_FILE_BYTES = 20_000_000
MAX_PDF_PAGES = 200
MAX_PDF_TEXT_CHARS = 1_000_000
PDF_HEADER_SCAN_BYTES = 1024


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
    def _is_pdf_file(path: Path) -> bool:
        return path.suffix.casefold() == ".pdf"

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

    @classmethod
    def _reject_sensitive_mutation(cls, path: Path) -> None:
        if cls._is_sensitive_file(path):
            raise WorkspaceError(
                "Das Ändern von Secret-/Credential- oder internen "
                "Workspace-Dateien ist über den Workspace-OS-Server nicht erlaubt."
            )

    @staticmethod
    def _reject_hardlinked_file(path: Path) -> None:
        try:
            hardlinked = regular_file_has_multiple_links(path)
        except OSError as exc:
            raise WorkspaceError(
                f"Dateimetadaten konnten nicht sicher geprüft werden: {path.name!r}"
            ) from exc
        if hardlinked:
            raise WorkspaceError(
                "Dateien mit mehreren Hardlinks werden vom Workspace-OS-Server "
                "aus Sicherheitsgründen nicht verarbeitet."
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

    @staticmethod
    def _read_text_file(file_path: Path, path: str) -> str:
        try:
            if file_path.stat().st_size > MAX_READ_FILE_BYTES:
                raise WorkspaceError(
                    f"Datei überschreitet das Leselimit von "
                    f"{MAX_READ_FILE_BYTES} Bytes: {path!r}"
                )
            res = file_path.read_text(encoding="utf-8")
            if not res:
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

    @staticmethod
    def _read_pdf(file_path: Path, path: str) -> str:
        try:
            file_size = file_path.stat().st_size
            if file_size > MAX_PDF_FILE_BYTES:
                raise WorkspaceError(
                    f"PDF-Datei überschreitet das Leselimit von "
                    f"{MAX_PDF_FILE_BYTES} Bytes: {path!r}"
                )

            with file_path.open("rb") as handle:
                header = handle.read(PDF_HEADER_SCAN_BYTES)
                if b"%PDF-" not in header:
                    raise WorkspaceError(
                        f"Datei hat keine gültige PDF-Signatur: {path!r}"
                    )
                handle.seek(0)

                reader = PdfReader(handle, strict=True)
                if reader.is_encrypted:
                    raise WorkspaceError(
                        f"Verschlüsselte PDF-Dateien werden nicht unterstützt: {path!r}"
                    )

                page_count = len(reader.pages)
                if page_count > MAX_PDF_PAGES:
                    raise WorkspaceError(
                        f"PDF-Datei überschreitet das Seitenlimit von "
                        f"{MAX_PDF_PAGES} Seiten: {path!r}"
                    )

                parts: list[str] = []
                text_chars = 0
                for page_number, page in enumerate(reader.pages, start=1):
                    page_text = (page.extract_text() or "").strip()
                    if not page_text:
                        continue
                    section = f"--- PDF-Seite {page_number} ---\n{page_text}"
                    text_chars += len(section)
                    if parts:
                        text_chars += 2
                    if text_chars > MAX_PDF_TEXT_CHARS:
                        raise WorkspaceError(
                            f"Extrahierter PDF-Text überschreitet das Leselimit von "
                            f"{MAX_PDF_TEXT_CHARS} Zeichen: {path!r}"
                        )
                    parts.append(section)

            if not parts:
                raise WorkspaceError(
                    "PDF-Datei enthält keinen direkt extrahierbaren Text. "
                    "OCR für bildbasierte oder gescannte PDFs ist nicht aktiviert."
                )
            return "\n\n".join(parts)
        except WorkspaceError:
            raise
        except (
            PdfReadError,
            OSError,
            ValueError,
            TypeError,
            KeyError,
            IndexError,
            RecursionError,
        ) as exc:
            raise WorkspaceError(
                f"PDF-Datei konnte nicht sicher gelesen werden: {path!r}"
            ) from exc

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
        self._reject_hardlinked_file(file_path)
        if self._is_sensitive_file(file_path):
            raise WorkspaceError(
                "Das Lesen von Secret-/Credential-Dateien ist über den "
                "Workspace-OS-Server nicht erlaubt."
            )

        if self._is_pdf_file(file_path):
            return self._read_pdf(file_path, path)
        if self._is_text_file(file_path):
            return self._read_text_file(file_path, path)
        raise WorkspaceError(f"Dateityp darf nicht gelesen werden: {path!r}")

    def delete_file(self, path: str) -> str:
        file_path = self.resolve_path(path)
        if not file_path.is_file():
            raise WorkspaceError(f"Pfad ist keine Datei: {path!r}")
        self._reject_hardlinked_file(file_path)
        self._reject_sensitive_mutation(file_path)

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
        self._reject_hardlinked_file(src_path)
        if self._is_sensitive_file(src_path):
            raise WorkspaceError(
                "Das Kopieren von Secret-/Credential-Dateien über den "
                "Workspace-OS-Server ist nicht erlaubt."
            )

        dst_path = self.resolve_path(path_dst, must_exist=False)
        self._reject_sensitive_mutation(dst_path)
        if dst_path.exists() and not dst_path.is_file():
            raise WorkspaceError(f"Zielpfad ist keine Datei: {path_dst!r}")
        if dst_path.exists():
            self._reject_hardlinked_file(dst_path)
        if not dst_path.parent.is_dir():
            raise WorkspaceError(
                f"Zielordner existiert nicht: "
                f"{dst_path.parent.relative_to(self.directory).as_posix()!r}"
            )

        try:
            shutil.copy2(src_path, dst_path)
        except OSError as exc:
            raise WorkspaceError(
                f"Datei konnte nicht kopiert werden: {path_src!r} -> {path_dst!r}: {exc}"
            ) from exc

        relative = dst_path.relative_to(self.directory).as_posix()
        return f"Datei kopiert: {relative} "

    def make_directory(self, path: str) -> str:
        dir_path = self.resolve_path(path, must_exist=False)
        self._reject_sensitive_mutation(dir_path)
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
        self._reject_sensitive_mutation(file_path)
        if file_path.exists() and not file_path.is_file():
            raise WorkspaceError(f"Pfad ist keine Datei: {path!r}")
        if file_path.exists():
            self._reject_hardlinked_file(file_path)
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
