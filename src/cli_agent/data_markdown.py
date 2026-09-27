from __future__ import annotations

import base64
import binascii
import json
import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any

from markdown_it import MarkdownIt
from markdown_it.token import Token

MAX_MARKDOWN_TABLES = 100
MAX_MARKDOWN_COLUMNS = 200
MAX_CONTEXT_CHARS = 500

_MARKDOWN = MarkdownIt("commonmark", {"html": False}).enable("table")
_SEPARATOR_CELL = re.compile(r"^:?-{3,}:?$")
_SCALAR_OVERRIDE_MARKER = re.compile(
    r"^<!-- cli-agent:data-overrides:v1:([A-Za-z0-9_-]+) -->$"
)


class MarkdownTableError(ValueError):
    """Markdown tables could not be extracted safely."""


def _inline_text(token: Token) -> str:
    children = token.children or []
    if not children:
        return token.content.strip()

    parts: list[str] = []
    for child in children:
        if child.type in {"text", "code_inline"}:
            parts.append(child.content)
        elif child.type in {"softbreak", "hardbreak"}:
            parts.append("\n")
        elif child.type == "image":
            parts.append(child.content)
    return "".join(parts).strip()


def _normalize_headers(source_headers: list[str]) -> list[str]:
    if len(source_headers) > MAX_MARKDOWN_COLUMNS:
        raise MarkdownTableError(
            "Markdown-Tabelle überschreitet das Spaltenlimit von "
            f"{MAX_MARKDOWN_COLUMNS}."
        )

    columns: list[str] = []
    seen: set[str] = set()
    for index, source_header in enumerate(source_headers, start=1):
        base = source_header.strip() or f"column_{index}"
        candidate = base
        suffix = 2
        while candidate in seen:
            candidate = f"{base}_{suffix}"
            suffix += 1
        seen.add(candidate)
        columns.append(candidate)
    return columns


def _preceding_context(lines: list[str], start_line: int) -> str | None:
    index = start_line - 1
    while index >= 0 and not lines[index].strip():
        index -= 1

    if (
        index >= 0
        and _SCALAR_OVERRIDE_MARKER.fullmatch(lines[index].strip())
    ):
        index -= 1
        while index >= 0 and not lines[index].strip():
            index -= 1

    if index < 0:
        return None

    block: list[str] = []
    total = 0
    while index >= 0 and lines[index].strip() and len(block) < 3:
        line = lines[index].strip()
        if total + len(line) + (1 if block else 0) > MAX_CONTEXT_CHARS:
            break
        block.append(line)
        total += len(line) + (1 if block else 0)
        index -= 1

    if not block:
        return None
    return "\n".join(reversed(block))


def _ends_with_unescaped_pipe(value: str) -> bool:
    if not value.endswith("|"):
        return False
    backslashes = 0
    index = len(value) - 2
    while index >= 0 and value[index] == "\\":
        backslashes += 1
        index -= 1
    return backslashes % 2 == 0


def _split_pipe_row(line: str) -> list[str] | None:
    text = line.strip()
    if "|" not in text:
        return None
    if text.startswith("|"):
        text = text[1:]
    if _ends_with_unescaped_pipe(text):
        text = text[:-1]

    cells: list[str] = []
    current: list[str] = []
    backslashes = 0
    for character in text:
        if character == "\\":
            current.append(character)
            backslashes += 1
            continue
        if character == "|" and backslashes % 2 == 0:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
        backslashes = 0
    cells.append("".join(current).strip())
    return cells


def _normalize_table_separator_widths(markdown: str) -> str:
    """Repair only missing cells in otherwise strict Markdown separators.

    Some HTML extractors emit a pipe-table header and data rows with N cells,
    but accidentally emit only N-1 separator cells. This helper repairs that
    narrow syntax defect so a standards-compliant Markdown parser can recognize
    the table. It never changes header or data-cell contents and never
    interprets arbitrary input as a separator.
    """

    lines = markdown.splitlines()
    for index in range(1, len(lines)):
        separator_cells = _split_pipe_row(lines[index])
        if (
            separator_cells is None
            or not separator_cells
            or not all(_SEPARATOR_CELL.fullmatch(cell) for cell in separator_cells)
        ):
            continue

        header_cells = _split_pipe_row(lines[index - 1])
        if (
            header_cells is None
            or len(header_cells) <= len(separator_cells)
            or len(header_cells) > MAX_MARKDOWN_COLUMNS
        ):
            continue

        normalized_cells = separator_cells + [
            "---"
            for _ in range(len(header_cells) - len(separator_cells))
        ]
        lines[index] = "|" + "|".join(normalized_cells) + "|"
    return "\n".join(lines)


def _normalize_markdown_input(markdown: str) -> str:
    """Normalize transport artifacts before parsing Markdown tables.

    Reference contexts are serialized as JSON before they reach the model. A
    model may copy visible \\n escape sequences literally into a tool argument
    instead of emitting real line breaks. Always decode a literal newline when
    it occurs between two pipe-table rows. If the whole payload contains no
    real line break, retain the previous conservative fallback and decode its
    newline escapes. Other escape sequences remain untouched data.
    """

    normalized = markdown.replace("\r\n", "\n").replace("\r", "\n")
    had_real_newline = "\n" in normalized

    normalized = re.sub(
        r"(?<=\|)(?:\\r\\n|\\n)(?=\s*\|)",
        "\n",
        normalized,
    )

    if not had_real_newline and "\\n" in normalized:
        normalized = normalized.replace("\\r\\n", "\n")
        normalized = normalized.replace("\\n", "\n")
    return _normalize_table_separator_widths(normalized)


def _string_requires_override(value: str) -> bool:
    if (
        value == ""
        or value != value.strip()
        or any(character in value for character in "\\|\r\n")
    ):
        return True

    stripped = value.strip()
    lowered = stripped.casefold()
    if lowered in {"true", "false"}:
        return True

    try:
        if not any(character in stripped for character in ".eE"):
            int(stripped)
            return True
    except ValueError:
        pass

    try:
        number = float(stripped)
    except ValueError:
        return False
    return math.isfinite(number)


def _encode_scalar_overrides(
    columns: list[str],
    rows: list[dict[str, Any]],
) -> str | None:
    entries: list[list[Any]] = []
    for row_index, row in enumerate(rows):
        for column_index, column in enumerate(columns):
            value = row.get(column)
            if isinstance(value, str) and _string_requires_override(value):
                entries.append([row_index, column_index, "s", value])
            elif isinstance(value, Decimal):
                if not value.is_finite():
                    raise MarkdownTableError(
                        "Dezimalwerte in Markdown-Tabellen müssen endlich sein."
                    )
                entries.append(
                    [row_index, column_index, "d", str(value)]
                )

    if not entries:
        return None

    payload = json.dumps(
        entries,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return f"<!-- cli-agent:data-overrides:v1:{encoded} -->"


def _decode_scalar_overrides(
    line: str,
    *,
    width: int,
    row_count: int,
) -> tuple[dict[tuple[int, int], Any], set[tuple[int, int]]]:
    match = _SCALAR_OVERRIDE_MARKER.fullmatch(line.strip())
    if match is None:
        return {}, set()

    encoded = match.group(1)
    padding = "=" * (-len(encoded) % 4)
    try:
        payload = base64.urlsafe_b64decode(encoded + padding)
        entries = json.loads(payload.decode("utf-8"))
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ) as exc:
        raise MarkdownTableError(
            "Ungültige interne Typmetadaten in Markdown-Tabelle."
        ) from exc

    if not isinstance(entries, list):
        raise MarkdownTableError(
            "Ungültige interne Typmetadaten in Markdown-Tabelle."
        )

    overrides: dict[tuple[int, int], Any] = {}
    preserved_strings: set[tuple[int, int]] = set()
    for entry in entries:
        if not isinstance(entry, list) or len(entry) != 4:
            raise MarkdownTableError(
                "Ungültige interne Typmetadaten in Markdown-Tabelle."
            )
        row_index, column_index, kind, raw_value = entry
        if (
            isinstance(row_index, bool)
            or not isinstance(row_index, int)
            or isinstance(column_index, bool)
            or not isinstance(column_index, int)
            or not 0 <= row_index < row_count
            or not 0 <= column_index < width
            or (row_index, column_index) in overrides
        ):
            raise MarkdownTableError(
                "Ungültige interne Typmetadaten in Markdown-Tabelle."
            )

        if kind == "s":
            if not isinstance(raw_value, str):
                raise MarkdownTableError(
                    "Ungültige interne Typmetadaten in Markdown-Tabelle."
                )
            value: Any = raw_value
            preserved_strings.add((row_index, column_index))
        elif kind == "d":
            if not isinstance(raw_value, str):
                raise MarkdownTableError(
                    "Ungültige interne Typmetadaten in Markdown-Tabelle."
                )
            try:
                value = Decimal(raw_value)
            except InvalidOperation as exc:
                raise MarkdownTableError(
                    "Ungültige interne Typmetadaten in Markdown-Tabelle."
                ) from exc
            if not value.is_finite():
                raise MarkdownTableError(
                    "Ungültige interne Typmetadaten in Markdown-Tabelle."
                )
        else:
            raise MarkdownTableError(
                "Ungültige interne Typmetadaten in Markdown-Tabelle."
            )

        overrides[(row_index, column_index)] = value

    return overrides, preserved_strings


def _markdown_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    return text.replace("\n", "\\n")


def render_markdown_table(
    columns: list[str],
    rows: list[dict[str, Any]],
    *,
    preserve_scalar_types: bool = False,
) -> str:
    """Render a canonical Markdown pipe table with a fixed column count."""

    if not columns:
        raise MarkdownTableError("Markdown-Tabelle benötigt mindestens eine Spalte.")
    if len(columns) > MAX_MARKDOWN_COLUMNS:
        raise MarkdownTableError(
            "Markdown-Tabelle überschreitet das Spaltenlimit von "
            f"{MAX_MARKDOWN_COLUMNS}."
        )
    if len(columns) != len(set(columns)) or any(
        not isinstance(column, str) or not column
        for column in columns
    ):
        raise MarkdownTableError(
            "Markdown-Tabelle benötigt eindeutige, nicht-leere Spaltennamen."
        )

    lines = [
        "| " + " | ".join(_markdown_scalar(column) for column in columns) + " |",
        "|" + "|".join("---" for _ in columns) + "|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(_markdown_scalar(row.get(column)) for column in columns)
            + " |"
        )

    table = "\n".join(lines)
    if not preserve_scalar_types:
        return table
    marker = _encode_scalar_overrides(columns, rows)
    return table if marker is None else marker + "\n" + table


def extract_markdown_tables(markdown: str) -> list[dict[str, Any]]:
    markdown = _normalize_markdown_input(markdown)
    tokens = _MARKDOWN.parse(markdown)
    lines = markdown.splitlines()
    tables: list[dict[str, Any]] = []

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.type != "table_open":
            index += 1
            continue

        if len(tables) >= MAX_MARKDOWN_TABLES:
            raise MarkdownTableError(
                "Markdown-Dokument enthält mehr als "
                f"{MAX_MARKDOWN_TABLES} Tabellen."
            )

        start_line = token.map[0] if token.map is not None else 0
        section: str | None = None
        current_row: list[str] | None = None
        current_cell: str | None = None
        source_headers: list[str] | None = None
        raw_rows: list[list[str]] = []

        index += 1
        while index < len(tokens) and tokens[index].type != "table_close":
            current = tokens[index]

            if current.type == "thead_open":
                section = "head"
            elif current.type == "thead_close":
                section = None
            elif current.type == "tbody_open":
                section = "body"
            elif current.type == "tbody_close":
                section = None
            elif current.type == "tr_open":
                current_row = []
            elif current.type in {"th_open", "td_open"}:
                current_cell = ""
            elif current.type == "inline" and current_cell is not None:
                current_cell = _inline_text(current)
            elif current.type in {"th_close", "td_close"}:
                if current_row is not None:
                    current_row.append((current_cell or "").strip())
                current_cell = None
            elif current.type == "tr_close" and current_row is not None:
                if section == "head" and source_headers is None:
                    source_headers = current_row
                elif section == "body":
                    raw_rows.append(current_row)
                current_row = None

            index += 1

        if source_headers is None:
            raise MarkdownTableError(
                "Markdown-Tabelle enthält keine auswertbare Kopfzeile."
            )

        columns = _normalize_headers(source_headers)
        width = len(columns)
        rows: list[dict[str, Any]] = []
        for raw_row in raw_rows:
            normalized_row = raw_row[:width] + [""] * max(0, width - len(raw_row))
            rows.append(
                {
                    column: (value if value != "" else None)
                    for column, value in zip(columns, normalized_row, strict=True)
                }
            )

        marker_line = lines[start_line - 1] if start_line > 0 else ""
        overrides, preserved_strings = _decode_scalar_overrides(
            marker_line,
            width=width,
            row_count=len(rows),
        )
        for (row_index, column_index), value in overrides.items():
            rows[row_index][columns[column_index]] = value

        tables.append(
            {
                "index": len(tables),
                "context": _preceding_context(lines, start_line),
                "source_headers": source_headers,
                "columns": columns,
                "row_count": len(rows),
                "rows": rows,
                "preserved_string_cells": [
                    [row_index, column_index]
                    for row_index, column_index in sorted(preserved_strings)
                ],
            }
        )
        index += 1

    return tables
