"""Automatic MCP result metadata for bounded, session-scoped data artifacts."""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from decimal import Decimal, DecimalException, localcontext
from typing import Mapping, Sequence

from mcp.types import CallToolResult, TextContent

from .mcp_result_artifacts import METADATA_FOR_LLM_KEY, ONLY_METADATA_TO_LLM_KEY


# Small complete results remain directly visible, while every result also
# receives a session path so subsequent file-based tools can reuse it.
INLINE_RESULT_BYTES = 5_000
# Reserve space for the verified workspace_path added by the agent. The data
# server does not know that session-specific path when creating its metadata.
MAX_SUMMARY_METADATA_BYTES = 5_000
_ARTIFACT_PATH_RESERVE_BYTES = 256
_MAX_SUMMARY_COLUMNS = 24
_MAX_PREVIEW_ROWS = 5
_MAX_PREVIEW_BYTES = 1_800
_MAX_DISTINCT_VALUES = 5
_ISO_TIMESTAMP = re.compile(
    r"\A\d{4}-\d{2}-\d{2}(?:[Tt ]\d{2}:\d{2}(?::\d{2}(?:[.,](\d+))?)?"
    r"(?:Z|[+-]\d{2}:\d{2})?)?\Z"
)


def _metadata_fits(metadata: dict[str, object]) -> bool:
    # Match the artifact store's actual JSON formatting, including spaces.
    return len(json.dumps(metadata, ensure_ascii=False, allow_nan=False).encode("utf-8")) <= (
        MAX_SUMMARY_METADATA_BYTES - _ARTIFACT_PATH_RESERVE_BYTES
    )


def _json_scalar(value: object) -> object:
    # The deterministic Markdown data tools preserve Decimal scalars. Label
    # them explicitly here instead of silently changing their precision/type.
    if isinstance(value, Decimal):
        return {"decimal": str(value)}
    return value


def _timestamp_key(value: str) -> tuple[bool, datetime, Decimal] | None:
    # Only claim a time range for recognized ISO timestamps. datetime's parser
    # truncates fractions after six digits, so compare any remaining digits as
    # a decimal fraction of a microsecond, not by their original spelling.
    match = _ISO_TIMESTAMP.fullmatch(value)
    if match is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        aware = parsed.tzinfo is not None and parsed.utcoffset() is not None
        if aware:
            parsed = parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None
    fractional = match.group(1) or ""
    extra = fractional[6:]
    submicroseconds = Decimal("0." + extra) if extra else Decimal(0)
    return aware, parsed, submicroseconds


def _column_statistics(column: str, rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    null_count = 0
    kind: str | None = None
    numeric_count = 0
    numeric_sum = Decimal(0)
    mean_available = True
    minimum: object = None
    maximum: object = None
    minimum_key: object = None
    maximum_key: object = None
    time_zone_aware: bool | None = None
    distinct: dict[tuple[type, object], int] | None = {}
    timestamp_column = any(term in column.casefold() for term in ("time", "date", "datum", "zeit"))

    for row in rows:
        value = row.get(column)
        if value is None:
            null_count += 1
            continue

        if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
            current_kind = "numeric"
            current_key = Decimal(str(value))
        elif timestamp_column and isinstance(value, str) and len(value) <= 80 and (
            time_key := _timestamp_key(value)
        ) is not None:
            current_kind = "datetime"
            aware, parsed, submicroseconds = time_key
            if time_zone_aware is not None and time_zone_aware != aware:
                kind = "mixed"
                continue
            time_zone_aware = aware
            current_key = (parsed, submicroseconds)
        else:
            current_kind = "categorical"
            current_key = None

        if kind is None:
            kind = current_kind
        elif kind != current_kind:
            kind = "mixed"
        if kind == "mixed":
            continue

        if kind in {"numeric", "datetime"}:
            if minimum_key is None or current_key < minimum_key:
                minimum_key, minimum = current_key, value
            if maximum_key is None or current_key > maximum_key:
                maximum_key, maximum = current_key, value
        if kind == "numeric":
            numeric_count += 1
            if mean_available:
                try:
                    with localcontext() as context:
                        context.prec = 50
                        numeric_sum += current_key
                except (DecimalException, ValueError):
                    mean_available = False
        elif kind == "categorical" and distinct is not None:
            # Never turn high-cardinality identifiers into misleading partial
            # frequency lists. Only expose a complete small value domain.
            if not isinstance(value, (str, bool)) or (isinstance(value, str) and len(value) > 120):
                distinct = None
            else:
                identity = (type(value), value)
                distinct[identity] = distinct.get(identity, 0) + 1
                if len(distinct) > _MAX_DISTINCT_VALUES:
                    distinct = None

    stats: dict[str, object] = {"column": column, "null_count": null_count}
    if kind in {"numeric", "datetime"}:
        stats["kind"] = kind
        stats["min"] = _json_scalar(minimum)
        stats["max"] = _json_scalar(maximum)
        if kind == "numeric" and numeric_count and mean_available:
            try:
                mean = float(numeric_sum / numeric_count)
                if math.isfinite(mean):
                    stats["mean"] = mean
            except (DecimalException, OverflowError, ValueError):
                pass
    elif kind == "categorical" and distinct is not None:
        stats["kind"] = "categorical"
        stats["value_counts"] = [
            {"value": value, "count": count}
            for (_, value), count in distinct.items()
        ]
    return stats


def add_large_result_summary(
    metadata: dict[str, object], columns: list[str], rows: Sequence[Mapping[str, object]],
) -> None:
    """Add complete-column statistics and whole preview rows within a hard budget.

    Statistics describe *all* rows, never just the preview. If the budget is
    too tight, entire statistics or preview rows are omitted, with explicit
    completeness flags so that a model cannot mistake a subset for the whole.
    """
    metadata.update({
        "column_stats": [],
        "stats_columns_complete": False,
        "sample_rows": [],
        "sample_rows_complete": False,
    })
    if not _metadata_fits(metadata):
        metadata["column_count"] = len(columns)
        metadata["columns_complete"] = False
        metadata["columns"] = []
        for column in columns:
            candidate = [*metadata["columns"], column]
            metadata["columns"] = candidate
            if not _metadata_fits(metadata):
                metadata["columns"] = candidate[:-1]
                break

    for row in rows[:_MAX_PREVIEW_ROWS]:
        # Do not disclose half a row or truncate a string field mid-value.
        sample = {column: _json_scalar(row.get(column)) for column in columns}
        candidate = [*metadata["sample_rows"], sample]
        metadata["sample_rows"] = candidate
        if not _metadata_fits(metadata) or len(json.dumps(
            candidate, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")) > _MAX_PREVIEW_BYTES:
            metadata["sample_rows"] = candidate[:-1]
            break
    metadata["sample_rows_complete"] = len(metadata["sample_rows"]) == len(rows)

    for column in columns[:_MAX_SUMMARY_COLUMNS]:
        stats = _column_statistics(column, rows)
        candidate = [*metadata["column_stats"], stats]
        metadata["column_stats"] = candidate
        if not _metadata_fits(metadata):
            metadata["column_stats"] = candidate[:-1]
            metadata["stats_columns_complete"] = False
    metadata["stats_columns_complete"] = len(metadata["column_stats"]) == len(columns)


def markdown_result(
    text: str, columns: list[str], row_count: int, *, operation: str,
    rows: Sequence[Mapping[str, object]] | None = None,
) -> CallToolResult:
    metadata: dict[str, object] = {
        "operation": operation,
        "row_count": row_count,
        "columns": columns,
        "format": "markdown",
    }
    if len(text.encode("utf-8")) <= INLINE_RESULT_BYTES:
        metadata["result"] = text
    elif rows is not None:
        add_large_result_summary(metadata, columns, rows)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        **{
            "_meta": {
                ONLY_METADATA_TO_LLM_KEY: True,
                METADATA_FOR_LLM_KEY: metadata,
            }
        },
    )
