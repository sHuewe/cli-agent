from __future__ import annotations

import base64
import binascii
import io
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
_TYPED_CELL_PREFIX = "cli-agent:data:v1:"
_LEGACY_TYPED_STRING_PREFIX = "string:"
_LEGACY_TYPED_DECIMAL_PREFIX = "decimal:"


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

    if index < 0:
        return None

    # A pipe row immediately before another table belongs to tabular data,
    # not prose context. Re-emitting it could create a parseable extra table
    # and shift downstream table_index values.
    if _split_pipe_row(lines[index]) is not None:
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


def _enforce_markdown_row_limit(
    markdown: str,
    *,
    max_rows: int,
) -> None:
    """Reject oversized pipe-table input before MarkdownIt tokenization.

    The scan is deliberately conservative and streaming. It recognizes the
    same strict separator rows that the normalizer accepts, including the
    narrowly supported short-separator repair. The budget applies to the total
    number of materialized body rows across the document because the parser
    extracts all tables before table_index is applied.
    """

    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 0:
        raise MarkdownTableError(
            "Markdown-Zeilenlimit muss eine nicht-negative ganze Zahl sein."
        )

    total_rows = 0
    previous_line: str | None = None
    in_table = False

    for raw_line in io.StringIO(markdown):
        line = raw_line.rstrip("\r\n")
        cells = _split_pipe_row(line)

        if in_table:
            if cells is not None and line.strip():
                total_rows += 1
                if total_rows > max_rows:
                    raise MarkdownTableError(
                        "Markdown-Tabellen überschreiten das Zeilenlimit von "
                        f"{max_rows}."
                    )
                previous_line = line
                continue
            in_table = False

        separator_cells = cells
        if (
            separator_cells is not None
            and separator_cells
            and all(
                _SEPARATOR_CELL.fullmatch(cell)
                for cell in separator_cells
            )
            and previous_line is not None
        ):
            header_cells = _split_pipe_row(previous_line)
            if (
                header_cells is not None
                and separator_cells
                and len(separator_cells) <= len(header_cells)
                and len(header_cells) <= MAX_MARKDOWN_COLUMNS
            ):
                in_table = True

        previous_line = line


def _normalize_markdown_input(
    markdown: str,
    *,
    max_rows: int | None = None,
) -> str:
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

    if max_rows is not None:
        _enforce_markdown_row_limit(
            normalized,
            max_rows=max_rows,
        )

    return _normalize_table_separator_widths(normalized)


def _string_requires_type_annotation(value: str) -> bool:
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


def _markdown_code_span(content: str) -> str:
    longest_run = max(
        (len(match.group(0)) for match in re.finditer(r"`+", content)),
        default=0,
    )
    fence = "`" * (longest_run + 1)
    return f"{fence}{content}{fence}"


def _encode_typed_cell(kind: str, value: str) -> str:
    """Encode a typed Markdown cell using only table-safe ASCII.

    URL-safe base64 avoids every Markdown pipe-table delimiter/escape concern
    (pipes, backslashes, backticks and physical newlines) and makes the
    round-trip contract independent of CommonMark code-span whitespace rules.
    """

    payload = json.dumps(
        [kind, value],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return _markdown_code_span(_TYPED_CELL_PREFIX + encoded)


def _decode_typed_cell(token: Token) -> tuple[str, str] | None:
    children = token.children or []
    if len(children) != 1 or children[0].type != "code_inline":
        return None

    content = children[0].content
    if not content.startswith(_TYPED_CELL_PREFIX):
        return None

    encoded = content[len(_TYPED_CELL_PREFIX):]
    if not encoded:
        return None
    padding = "=" * (-len(encoded) % 4)
    try:
        raw = base64.b64decode(
            encoded + padding,
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("utf-8"))
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
    ):
        return None

    if (
        not isinstance(payload, list)
        or len(payload) != 2
        or not isinstance(payload[0], str)
        or not isinstance(payload[1], str)
    ):
        return None
    return payload[0], payload[1]


def _inline_header(token: Token) -> str:
    decoded = _decode_typed_cell(token)
    if decoded is not None:
        kind, value = decoded
        if kind == "h":
            return value
    return _inline_text(token)


def _inline_scalar(token: Token) -> tuple[Any, bool]:
    decoded = _decode_typed_cell(token)
    if decoded is not None:
        kind, payload = decoded
        if kind == "s":
            return payload, True
        if kind == "d":
            try:
                value = Decimal(payload)
            except InvalidOperation:
                pass
            else:
                if value.is_finite():
                    return value, False

    # Backwards-compatible reader for typed cells produced by earlier
    # development revisions. New output always uses the versioned codec above.
    children = token.children or []
    if len(children) == 1 and children[0].type == "code_inline":
        content = children[0].content
        if content.startswith(_LEGACY_TYPED_STRING_PREFIX):
            payload = content[len(_LEGACY_TYPED_STRING_PREFIX):]
            try:
                value = json.loads(payload)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(value, str):
                    return value, True

        if content.startswith(_LEGACY_TYPED_DECIMAL_PREFIX):
            payload = content[len(_LEGACY_TYPED_DECIMAL_PREFIX):]
            try:
                value = Decimal(payload)
            except InvalidOperation:
                pass
            else:
                if value.is_finite():
                    return value, False

    return _inline_text(token), False


def _markdown_header(value: str) -> str:
    if any(character in value for character in "\\|`\r\n"):
        return _encode_typed_cell("h", value)
    return _markdown_scalar(value)


def _markdown_scalar(
    value: Any,
    *,
    preserve_scalar_type: bool = False,
) -> str:
    if value is None:
        return ""

    if (
        preserve_scalar_type
        and isinstance(value, str)
        and _string_requires_type_annotation(value)
    ):
        return _encode_typed_cell("s", value)

    if preserve_scalar_type and isinstance(value, Decimal):
        if not value.is_finite():
            raise MarkdownTableError(
                "Dezimalwerte in Markdown-Tabellen müssen endlich sein."
            )
        return _encode_typed_cell("d", str(value))

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
    """Render a canonical Markdown pipe table with a fixed column count.

    When scalar preservation is enabled, values whose plain Markdown spelling
    is ambiguous are encoded visibly as typed inline-code cells. The type is
    therefore part of the displayed table instead of hidden metadata that an
    untrusted document could spoof independently of the visible value.
    """

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
        "| " + " | ".join(_markdown_header(column) for column in columns) + " |",
        "|" + "|".join("---" for _ in columns) + "|",
    ]
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                _markdown_scalar(
                    row.get(column),
                    preserve_scalar_type=preserve_scalar_types,
                )
                for column in columns
            )
            + " |"
        )

    return "\n".join(lines)


def extract_markdown_tables(
    markdown: str,
    *,
    max_rows: int | None = None,
) -> list[dict[str, Any]]:
    markdown = _normalize_markdown_input(
        markdown,
        max_rows=max_rows,
    )
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
        current_row: list[Any] | None = None
        current_row_preserved_strings: set[int] | None = None
        current_cell: Any | None = None
        current_cell_preserved_string = False
        source_headers: list[str] | None = None
        raw_rows: list[list[Any]] = []
        raw_preserved_strings: list[set[int]] = []

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
                current_row_preserved_strings = set()
            elif current.type in {"th_open", "td_open"}:
                current_cell = ""
                current_cell_preserved_string = False
            elif current.type == "inline" and current_cell is not None:
                if section == "body":
                    (
                        current_cell,
                        current_cell_preserved_string,
                    ) = _inline_scalar(current)
                else:
                    current_cell = _inline_header(current)
            elif current.type in {"th_close", "td_close"}:
                if current_row is not None:
                    value = current_cell
                    if (
                        isinstance(value, str)
                        and not current_cell_preserved_string
                    ):
                        value = value.strip()
                    if (
                        current_cell_preserved_string
                        and current_row_preserved_strings is not None
                    ):
                        current_row_preserved_strings.add(len(current_row))
                    current_row.append(value)
                current_cell = None
                current_cell_preserved_string = False
            elif current.type == "tr_close" and current_row is not None:
                if section == "head" and source_headers is None:
                    source_headers = [
                        str(value)
                        for value in current_row
                    ]
                elif section == "body":
                    raw_rows.append(current_row)
                    raw_preserved_strings.append(
                        set(current_row_preserved_strings or ())
                    )
                current_row = None
                current_row_preserved_strings = None

            index += 1

        if source_headers is None:
            raise MarkdownTableError(
                "Markdown-Tabelle enthält keine auswertbare Kopfzeile."
            )

        columns = _normalize_headers(source_headers)
        width = len(columns)
        rows: list[dict[str, Any]] = []
        preserved_strings: set[tuple[int, int]] = set()
        for row_index, raw_row in enumerate(raw_rows):
            normalized_row = raw_row[:width] + [""] * max(0, width - len(raw_row))
            preserved_columns = (
                raw_preserved_strings[row_index]
                if row_index < len(raw_preserved_strings)
                else set()
            )
            normalized_record: dict[str, Any] = {}
            for column_index, (column, value) in enumerate(
                zip(columns, normalized_row, strict=True)
            ):
                if column_index in preserved_columns:
                    normalized_record[column] = value
                    preserved_strings.add((row_index, column_index))
                elif isinstance(value, str) and value == "":
                    normalized_record[column] = None
                else:
                    normalized_record[column] = value
            rows.append(normalized_record)

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
