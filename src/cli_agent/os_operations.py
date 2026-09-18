from __future__ import annotations

import datetime
import fnmatch
import io
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath

from .config import McpServerConfig
from .filesystem_security import regular_file_has_multiple_links
from .pdf_text import PdfTextError, read_pdf_text

TEXT_SUFFIXES = frozenset(
    {
        ".adoc",
        ".asm",
        ".bash",
        ".bat",
        ".c",
        ".cc",
        ".cfg",
        ".clj",
        ".cljs",
        ".cljc",
        ".coffee",
        ".cmd",
        ".conf",
        ".cpp",
        ".cs",
        ".cshtml",
        ".css",
        ".csv",
        ".cts",
        ".cxx",
        ".dart",
        ".diff",
        ".dockerignore",
        ".editorconfig",
        ".env",
        ".erl",
        ".ex",
        ".exs",
        ".fish",
        ".fs",
        ".fsi",
        ".fsx",
        ".gitignore",
        ".go",
        ".gradle",
        ".graphql",
        ".gql",
        ".groovy",
        ".gvy",
        ".h",
        ".hcl",
        ".hh",
        ".hpp",
        ".hrl",
        ".hs",
        ".html",
        ".htm",
        ".hxx",
        ".ini",
        ".ipynb",
        ".java",
        ".js",
        ".json",
        ".json5",
        ".jsonc",
        ".jsx",
        ".jsp",
        ".jspx",
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
        ".ml",
        ".mli",
        ".mm",
        ".mts",
        ".ndjson",
        ".nim",
        ".org",
        ".patch",
        ".php",
        ".pl",
        ".pm",
        ".pro",
        ".proto",
        ".properties",
        ".ps1",
        ".psd1",
        ".psm1",
        ".py",
        ".pyi",
        ".r",
        ".rb",
        ".rmd",
        ".razor",
        ".rs",
        ".rst",
        ".sass",
        ".sc",
        ".scala",
        ".scss",
        ".sh",
        ".sql",
        ".sol",
        ".svelte",
        ".svg",
        ".swift",
        ".tex",
        ".text",
        ".tf",
        ".tfvars",
        ".toml",
        ".tsv",
        ".ts",
        ".tsx",
        ".txt",
        ".v",
        ".vb",
        ".vhd",
        ".vhdl",
        ".vue",
        ".xml",
        ".xhtml",
        ".yaml",
        ".yml",
        ".zig",
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
        ".npmignore",
        ".prettierignore",
        "dockerfile",
        "gemfile",
        "makefile",
        "procfile",
        "rakefile",
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
MAX_SEARCH_RESULTS = 200
MAX_SEARCH_TEXT_LENGTH = 4_096
MAX_SEARCH_LINE_CHARS = 4_000
MAX_SEARCH_SCAN_FILES = 10_000
MAX_SEARCH_SCAN_BYTES = 64_000_000
MAX_FIND_RESULTS = 500
MAX_FIND_SCAN_FILES = 10_000


class WorkspaceError(RuntimeError):
    """An OS operation could not be performed inside the workspace."""


class _TraversalBudgetReached(RuntimeError):
    """Internal signal that a bounded recursive walk inspected too many entries."""


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

    def resolve_direct_path(self, path: str, *, must_exist: bool = True) -> Path:
        """Resolve a path but reject symlink/junction indirection.

        Operations that must act on the path the caller named rather than on a
        canonicalized target use this stricter resolver. Comparing the lexical
        absolute path with the canonical path also rejects indirection in parent
        components.
        """
        resolved = self.resolve_path(path, must_exist=must_exist)
        lexical = (self.directory / Path(path.strip())).absolute()
        if os.path.normcase(str(lexical)) != os.path.normcase(str(resolved)):
            raise WorkspaceError(
                "Symlinks oder Junctions sind für diese Dateioperation nicht erlaubt."
            )
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

    @classmethod
    def _reject_sensitive_read(cls, path: Path) -> None:
        if cls._is_sensitive_file(path):
            raise WorkspaceError(
                "Das Lesen von Secret-/Credential-Dateien ist über den "
                "Workspace-OS-Server nicht erlaubt."
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
    def _limit(value: int, *, name: str, maximum: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
            raise WorkspaceError(f"{name} muss zwischen 1 und {maximum} liegen.")
        return value

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

    def _safe_walk_files(self, root: Path, *, max_entries: int | None = None):
        """Yield regular files without following filesystem indirection.

        Every directory entry is counted before filtering so recursive callers
        can bound traversal work even for empty directories, sensitive paths,
        aliases, hardlinks, and other entries that will never be yielded.
        Directory/file symlinks, junction-like paths that resolve elsewhere,
        sensitive paths, and hardlinked files are skipped. This keeps recursive
        read operations within the same security boundary as read_file().
        """
        if root.is_file():
            self._reject_sensitive_read(root)
            self._reject_hardlinked_file(root)
            yield root
            return
        if not root.is_dir():
            raise WorkspaceError("Suchpfad ist weder Datei noch Ordner.")
        self._reject_sensitive_read(root)

        pending = [root]
        inspected_entries = 0
        while pending:
            current_path = pending.pop()
            try:
                with os.scandir(current_path) as entries:
                    for item in entries:
                        inspected_entries += 1
                        if max_entries is not None and inspected_entries > max_entries:
                            raise _TraversalBudgetReached

                        entry = Path(item.path)
                        try:
                            resolved = entry.resolve(strict=True)
                            resolved.relative_to(self.directory)
                        except (OSError, ValueError):
                            continue
                        # Do not recursively traverse symlinks, junctions or
                        # other path indirection even when their target happens
                        # to be inside the workspace.
                        if resolved != entry.absolute():
                            continue
                        if self._is_sensitive_file(resolved):
                            continue
                        if resolved.is_dir():
                            pending.append(resolved)
                            continue
                        if not resolved.is_file():
                            continue
                        try:
                            if regular_file_has_multiple_links(resolved):
                                continue
                        except OSError:
                            continue
                        yield resolved
            except _TraversalBudgetReached:
                raise
            except OSError:
                # Match os.walk's previous default behaviour: inaccessible
                # directories are skipped rather than failing the whole search.
                continue

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
        self._reject_sensitive_read(file_path)
        if file_path.suffix.lower() == ".pdf":
            try:
                return read_pdf_text(file_path)
            except PdfTextError as exc:
                raise WorkspaceError(str(exc)) from exc
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

    def search_text(
        self,
        path: str,
        text: str,
        max_results: int = 50,
    ) -> str:
        if not isinstance(text, str) or not text:
            raise WorkspaceError("text darf nicht leer sein.")
        if "\n" in text or "\r" in text:
            raise WorkspaceError("text darf keine Zeilenumbrüche enthalten.")
        if len(text) > MAX_SEARCH_TEXT_LENGTH:
            raise WorkspaceError(
                f"text darf höchstens {MAX_SEARCH_TEXT_LENGTH} Zeichen lang sein."
            )
        limit = self._limit(max_results, name="max_results", maximum=MAX_SEARCH_RESULTS)
        root = self.resolve_direct_path(path)
        matches: list[dict[str, object]] = []
        truncated = False
        scan_budget_reached = False
        scanned_files = 0
        scanned_bytes = 0

        try:
            for file_path in self._safe_walk_files(root, max_entries=MAX_SEARCH_SCAN_FILES):
                if scanned_files >= MAX_SEARCH_SCAN_FILES:
                    truncated = True
                    scan_budget_reached = True
                    break
                scanned_files += 1
                if not self._is_text_file(file_path):
                    continue

                remaining_bytes = MAX_SEARCH_SCAN_BYTES - scanned_bytes
                if remaining_bytes <= 0:
                    truncated = True
                    scan_budget_reached = True
                    break

                try:
                    with file_path.open("rb") as handle:
                        data = handle.read(min(MAX_READ_FILE_BYTES, remaining_bytes) + 1)
                    scanned_bytes += len(data)
                    if scanned_bytes > MAX_SEARCH_SCAN_BYTES:
                        truncated = True
                        scan_budget_reached = True
                        break
                    if len(data) > MAX_READ_FILE_BYTES:
                        continue
                    # Decode the complete bounded file before publishing any
                    # matches. A later invalid byte must invalidate the entire
                    # file rather than leave earlier partial results behind.
                    content = data.decode("utf-8")
                    with io.StringIO(content, newline=None) as handle:
                        for line_number, line in enumerate(handle, start=1):
                            match_start = line.find(text)
                            if match_start < 0:
                                continue
                            line_text = line.rstrip("\r\n")
                            # Keep the whole literal match, including queries up to
                            # MAX_SEARCH_TEXT_LENGTH, and centre context around it.
                            width = max(MAX_SEARCH_LINE_CHARS, len(text))
                            text_truncated = len(line_text) > width
                            excerpt_start = 0
                            if text_truncated:
                                excerpt_start = min(
                                    max(0, match_start - (width - len(text)) // 2),
                                    len(line_text) - width,
                                )
                                line_text = line_text[excerpt_start:excerpt_start + width]
                            match: dict[str, object] = {
                                "path": file_path.relative_to(self.directory).as_posix(),
                                "line": line_number,
                                "column": match_start + 1,
                                "text": line_text,
                            }
                            if text_truncated:
                                match["text_truncated"] = True
                                match["text_start_column"] = excerpt_start + 1
                            matches.append(match)
                            # Probe for one additional result so truncated is true
                            # only when a result was actually omitted.
                            if len(matches) > limit:
                                truncated = True
                                break
                except UnicodeDecodeError:
                    continue
                except OSError as exc:
                    raise WorkspaceError(
                        f"Datei konnte bei der Textsuche nicht sicher gelesen werden: "
                        f"{file_path.name!r}: {exc}"
                    ) from exc
                if truncated:
                    break
        except _TraversalBudgetReached:
            truncated = True
            scan_budget_reached = True

        result: dict[str, object] = {
            "matches": matches[:limit],
            "truncated": truncated,
        }
        if scan_budget_reached:
            result["truncation_reason"] = "scan_budget"
        return json.dumps(result, ensure_ascii=False, indent=2)

    def find_files(
        self,
        path: str,
        pattern: str,
        max_results: int = 100,
    ) -> str:
        if not isinstance(pattern, str) or not pattern:
            raise WorkspaceError("pattern darf nicht leer sein.")
        limit = self._limit(max_results, name="max_results", maximum=MAX_FIND_RESULTS)
        root = self.resolve_direct_path(path)
        matches: list[str] = []
        truncated = False
        scan_budget_reached = False
        scanned_files = 0

        try:
            for file_path in self._safe_walk_files(root, max_entries=MAX_FIND_SCAN_FILES):
                if scanned_files >= MAX_FIND_SCAN_FILES:
                    truncated = True
                    scan_budget_reached = True
                    break
                scanned_files += 1
                relative = file_path.relative_to(self.directory).as_posix()
                if not (
                    fnmatch.fnmatchcase(file_path.name, pattern)
                    or fnmatch.fnmatchcase(relative, pattern)
                ):
                    continue
                matches.append(relative)
                # Probe for one additional result so exact-limit result sets are
                # reported as complete.
                if len(matches) > limit:
                    truncated = True
                    break
        except _TraversalBudgetReached:
            truncated = True
            scan_budget_reached = True

        result: dict[str, object] = {
            "files": matches[:limit],
            "truncated": truncated,
        }
        if scan_budget_reached:
            result["truncation_reason"] = "scan_budget"
        return json.dumps(result, ensure_ascii=False, indent=2)

    def file_info(self, path: str) -> str:
        resolved = self.resolve_path(path)
        self._reject_sensitive_read(resolved)
        if resolved.is_file():
            self._reject_hardlinked_file(resolved)
            kind = "file"
        elif resolved.is_dir():
            kind = "directory"
        else:
            raise WorkspaceError(f"Pfad ist keine reguläre Datei oder Ordner: {path!r}")
        try:
            stat = resolved.stat()
        except OSError as exc:
            raise WorkspaceError(
                f"Dateimetadaten konnten nicht gelesen werden: {path!r}: {exc}"
            ) from exc
        result = {
            "path": resolved.relative_to(self.directory).as_posix() or ".",
            "type": kind,
            "size_bytes": stat.st_size if kind == "file" else None,
            "modified": datetime.datetime.fromtimestamp(
                stat.st_mtime,
                tz=datetime.UTC,
            ).isoformat(),
        }
        return json.dumps(result, ensure_ascii=False, indent=2)

    def delete_file(self, path: str) -> str:
        file_path = self.resolve_direct_path(path)
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
        src_path = self.resolve_direct_path(path_src)
        if not src_path.is_file():
            raise WorkspaceError(f"Quellpfad ist keine Datei: {path_src!r}")
        self._reject_hardlinked_file(src_path)
        if self._is_sensitive_file(src_path):
            raise WorkspaceError(
                "Das Kopieren von Secret-/Credential-Dateien über den "
                "Workspace-OS-Server ist nicht erlaubt."
            )

        dst_path = self.resolve_direct_path(path_dst, must_exist=False)
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

    def move_file(self, path_src: str, path_dst: str) -> str:
        src_path = self.resolve_direct_path(path_src)
        if not src_path.is_file():
            raise WorkspaceError(f"Quellpfad ist keine Datei: {path_src!r}")
        self._reject_hardlinked_file(src_path)
        self._reject_sensitive_mutation(src_path)

        dst_path = self.resolve_direct_path(path_dst, must_exist=False)
        self._reject_sensitive_mutation(dst_path)
        if dst_path.exists() and not dst_path.is_file():
            raise WorkspaceError(f"Zielpfad ist keine Datei: {path_dst!r}")
        if dst_path.exists():
            self._reject_hardlinked_file(dst_path)
        if not dst_path.parent.is_dir():
            raise WorkspaceError(
                f"Zielordner existiert nicht: {path_dst!r}"
            )
        if src_path == dst_path:
            relative = src_path.relative_to(self.directory).as_posix()
            return f"Datei befindet sich bereits am Ziel: {relative}"

        try:
            src_path.replace(dst_path)
        except OSError as exc:
            raise WorkspaceError(
                f"Datei konnte nicht verschoben werden: {path_src!r} -> {path_dst!r}: {exc}"
            ) from exc

        relative = dst_path.relative_to(self.directory).as_posix()
        return f"Datei verschoben: {relative}"

    def make_directory(self, path: str) -> str:
        dir_path = self.resolve_direct_path(path, must_exist=False)
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
        file_path = self.resolve_direct_path(path, must_exist=False)
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
