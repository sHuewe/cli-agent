from __future__ import annotations

import csv
import json
import math
import statistics
import io
from decimal import Decimal, DecimalException, localcontext
from pathlib import Path
from typing import Any

from cli_agent.data.data_calculator import (
    DECIMAL_PRECISION,
    CalculatorError,
    calculate_expression,
)
from cli_agent.data.data_markdown import (
    MarkdownTableError,
    extract_markdown_tables,
    render_markdown_table,
)
from cli_agent.data.data_result_metadata import DECIMAL_SCALAR_KEY, SCALAR_ENCODING
from cli_agent.platform.os_operations import Workspace, WorkspaceError

DATA_SUFFIXES = frozenset({".csv", ".tsv", ".json", ".jsonl", ".ndjson", ".md", ".markdown"})
DATA_FORMATS = frozenset({"csv", "tsv", "json", "jsonl", "ndjson", "markdown"})
MAX_DATA_FILE_BYTES = 100_000_000
MAX_DATA_PAYLOAD_CHARS = 2_000_000
MAX_DATA_ROWS = 1_000_000
MAX_CSV_FIELD_CHARS = MAX_DATA_FILE_BYTES
MAX_FULL_RESULT_BYTES = 16_000_000
MAX_SAMPLE_ROWS = 50
DEFAULT_DISTINCT_VALUES_LIMIT = 20
MAX_DISTINCT_VALUES_LIMIT = 100
MAX_SCHEMA_DISTINCT_VALUE_CHARS = 100_000
MAX_SCHEMA_DISTINCT_TOTAL_CHARS = 1_000_000
# Keep schema results comfortably below the general 10M MCP tool-result guard.
# This budget is applied to the complete serialized inspect_schema result, not
# just to distinct-value payloads.
MAX_SCHEMA_RESULT_CHARS = 8_000_000
MAX_VALUE_COUNT_ROWS = 200
MAX_DECIMAL_WORKING_PRECISION = 10_000
FILTER_OPERATORS = frozenset(
    {
        "eq",
        "ne",
        "lt",
        "lte",
        "gt",
        "gte",
        "in",
        "not_in",
        "is_null",
        "not_null",
        "contains",
    }
)
AGGREGATION_FUNCTIONS = frozenset(
    {"count", "sum", "mean", "min", "max", "median", "nunique", "std"}
)

Scalar = str | int | float | Decimal | bool | None
Record = dict[str, Scalar]


class DataOperationError(RuntimeError):
    """A tabular data operation could not be completed safely."""


def _reject_json_constant(value: str) -> Any:
    raise DataOperationError(
        f"JSON enthält eine nicht-endliche Zahl: {value}."
    )


def _json_scalar(value: Any) -> Scalar:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DataOperationError(
                "JSON enthält eine nicht-endliche Zahl."
            )
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise DataOperationError(
                "JSON enthält eine nicht-endliche Zahl."
            )
        return value
    raise DataOperationError(
        "Tabellarische Daten dürfen nur skalare JSON-Werte enthalten."
    )


def _configure_csv_field_limit() -> None:
    """Allow fields up to the already-enforced dataset size boundary."""

    current = csv.field_size_limit()
    if current < MAX_CSV_FIELD_CHARS:
        csv.field_size_limit(MAX_CSV_FIELD_CHARS)


def _validate_column_name(value: Any, *, source: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
    ):
        raise DataOperationError(
            f"{source} enthält einen ungültigen Spaltennamen. "
            "Spaltennamen müssen nicht-leer sein und dürfen keine führenden "
            "oder nachgestellten Leerzeichen enthalten."
        )
    return value


def _infer_csv_scalar(value: str | None) -> Scalar:
    if value is None or value == "":
        return None
    stripped = value.strip()
    if stripped == "":
        return value
    lowered = stripped.casefold()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    try:
        if not any(character in stripped for character in ".eE"):
            return int(stripped)
    except ValueError:
        pass
    try:
        number = float(stripped)
    except ValueError:
        return value
    # Keep the familiar float inference for ordinary CSV values, but do not
    # irreversibly round unusually precise decimal cells at the boundary.
    digits = [character for character in stripped if character.isdigit()]
    if len(digits) > 17 or not math.isfinite(number):
        try:
            precise = Decimal(stripped)
        except DecimalException:
            return value
        return precise if precise.is_finite() else value
    return number if math.isfinite(number) else value


def _dtype_name(values: list[Scalar]) -> str:
    present = [value for value in values if value is not None]
    if not present:
        return "null"
    kinds = {
        "bool"
        if isinstance(value, bool)
        else "int"
        if isinstance(value, int)
        else "float"
        if isinstance(value, float)
        else "decimal"
        if isinstance(value, Decimal)
        else "string"
        for value in present
    }
    if kinds <= {"int"}:
        return "int"
    if kinds <= {"int", "float"}:
        return "float"
    if kinds <= {"int", "float", "decimal"}:
        return "decimal"
    if len(kinds) == 1:
        return next(iter(kinds))
    return "mixed"


def _scalar_identity(value: Scalar) -> tuple[str, Scalar]:
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, (int, float, Decimal)):
        return ("number", value)
    if value is None:
        return ("null", None)
    return ("string", value)


def _typed_equal(left: Scalar, right: Scalar) -> bool:
    return _scalar_identity(left) == _scalar_identity(right)


def _filter_comparison_values(left: Scalar, right: Scalar) -> tuple[Scalar, Scalar]:
    """Compare numeric filter operands by their decimal *values*.

    JSON readers can preserve exact fractional values as Decimal, while MCP
    filter arguments arrive as floats. Decimal('0.1') != float(0.1) and even
    their ordering differs, so normalize both operands before comparison.
    Keep bool separate from numeric types (True is not the number 1).
    """
    numeric = (int, float, Decimal)
    if (
        isinstance(left, numeric) and not isinstance(left, bool)
        and isinstance(right, numeric) and not isinstance(right, bool)
    ):
        return _decimal_value(left), _decimal_value(right)
    return left, right


def _require_finite_float_result(
    value: float,
    *,
    function: str,
    column: str,
) -> float:
    if not math.isfinite(value):
        raise DataOperationError(
            f"Aggregation {function!r} erzeugt kein endliches Ergebnis "
            f"für Spalte {column!r}."
        )
    return value


def _json_dumps_precise(value: Any, *, indent: int | None = None) -> str:
    def render(current: Any, level: int) -> str:
        if isinstance(current, Decimal):
            if not current.is_finite():
                raise ValueError("Decimal JSON values must be finite.")
            return str(current)
        if current is None or isinstance(current, (str, bool, int, float)):
            return json.dumps(
                current,
                ensure_ascii=False,
                allow_nan=False,
            )
        if isinstance(current, (list, tuple)):
            if not current:
                return "[]"
            rendered = [render(item, level + 1) for item in current]
            if indent is None:
                return "[" + ",".join(rendered) + "]"
            padding = " " * (indent * (level + 1))
            closing = " " * (indent * level)
            return "[\n" + padding + (",\n" + padding).join(rendered) + "\n" + closing + "]"
        if isinstance(current, dict):
            if not all(isinstance(key, str) for key in current):
                raise TypeError("JSON object keys must be strings.")
            if not current:
                return "{}"
            separator = ":" if indent is None else ": "
            rendered = [
                json.dumps(key, ensure_ascii=False)
                + separator
                + render(item, level + 1)
                for key, item in current.items()
            ]
            if indent is None:
                return "{" + ",".join(rendered) + "}"
            padding = " " * (indent * (level + 1))
            closing = " " * (indent * level)
            return "{\n" + padding + (",\n" + padding).join(rendered) + "\n" + closing + "}"
        raise TypeError(f"Nicht JSON-serialisierbarer Wert: {type(current).__name__}")

    return render(value, 0)


def _json_scalar_char_count(value: Scalar) -> int:
    """Return the serialized JSON character count for one scalar value."""

    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("Decimal JSON values must be finite.")
        return len(str(value))
    return len(
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
        )
    )


def _decimal_value(value: int | float | Decimal) -> Decimal:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    return Decimal(str(value))


def _decimal_working_precision(values: list[Decimal]) -> int:
    if not values:
        return DECIMAL_PRECISION

    highest_adjusted = max(
        value.adjusted() if not value.is_zero() else 0
        for value in values
    )
    lowest_exponent = min(
        value.as_tuple().exponent
        for value in values
    )
    span = max(1, highest_adjusted - lowest_exponent + 2)
    required = max(DECIMAL_PRECISION, span + DECIMAL_PRECISION)
    if required > MAX_DECIMAL_WORKING_PRECISION:
        raise DataOperationError(
            "Dezimalwerte benötigen eine zu hohe Arbeitspräzision "
            f"(maximal {MAX_DECIMAL_WORKING_PRECISION} Stellen)."
        )
    return required



# Re-export every shared parser and comparison helper, including private ones.
# This intentionally centralizes constraints for the format and query engines.
__all__ = [name for name in globals() if not name.startswith("__")]
