from __future__ import annotations

import codecs
import datetime
import fnmatch
import io
import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath

from cli_agent.settings.config import McpServerConfig
from cli_agent.security.filesystem_security import (
    path_entry_is_symlink_or_reparse,
    regular_file_has_multiple_links,
)
from cli_agent.context.pdf_text import PdfTextError, read_pdf_text

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
        "credentials",
        "secrets",
    }
)
SENSITIVE_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})
MAX_READ_FILE_BYTES = 1_000_000
MAX_READ_RANGE_SCAN_BYTES = 64_000_000
READ_RANGE_CHUNK_BYTES = 64_000
MAX_SEARCH_RESULTS = 200
MAX_SEARCH_TEXT_LENGTH = 4_096
MAX_SEARCH_LINE_CHARS = 4_000
MAX_SEARCH_SCAN_FILES = 10_000
MAX_SEARCH_SCAN_BYTES = 64_000_000
MAX_FIND_RESULTS = 500
MAX_FIND_SCAN_FILES = 10_000


def _validate_latin1_bytes(data: bytes) -> None:
    """Reject recognizable binary formats before the permissive Latin-1 fallback."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise UnicodeError("UTF-16-Dateien werden nicht unterstützt.")
    if any(
        byte < 32 and byte not in (9, 10, 13) or 127 <= byte <= 159
        for byte in data
    ):
        raise UnicodeError("Datei enthält Steuerzeichen oder Binärdaten.")


def _decode_workspace_text(data: bytes) -> tuple[str, str]:
    """Prefer UTF-8, falling back to ISO-8859-1 only for plausible text."""
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        _validate_latin1_bytes(data)
        return data.decode("iso-8859-1"), "iso-8859-1"


class WorkspaceError(RuntimeError):
    """An OS operation could not be performed inside the workspace."""


@dataclass
class _RangeTextSelection:
    """Bounded collection of a requested range from one decoded text stream."""

    start: int
    end: int | None
    selected_parts: list[str] = None
    selected_bytes: int = 0
    line_number: int = 1
    pending_cr: bool = False
    done: bool = False
    oversized: bool = False

    def __post_init__(self) -> None:
        self.selected_parts = []

    def feed(self, text: str, *, final: bool = False) -> None:
        if self.done:
            return
        if self.pending_cr:
            text = "\r" + text
            self.pending_cr = False
        if not final and text.endswith("\r"):
            text = text[:-1]
            self.pending_cr = True
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        for fragment in text.splitlines(keepends=True):
            in_range = (
                self.line_number >= self.start
                and (self.end is None or self.line_number <= self.end)
            )
            if in_range:
                self.selected_bytes += len(fragment.encode("utf-8"))
                if self.selected_bytes > MAX_READ_FILE_BYTES:
                    self.oversized = True
                elif not self.oversized:
                    self.selected_parts.append(fragment)
            if fragment.endswith("\n"):
                if self.end is not None and self.line_number >= self.end:
                    self.done = True
                    return
                self.line_number += 1


class _TraversalBudgetReached(RuntimeError):
    """Internal signal that a bounded recursive walk inspected too many entries."""


@dataclass(frozen=True)
class AllowedPath:
    """A narrowly scoped exception to a protected workspace subtree."""
    path: Path
    access: str = "read"



__all__ = [name for name in globals() if not name.startswith("__")]
