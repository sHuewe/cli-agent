from __future__ import annotations

import csv
import json
import math
import statistics
import io
from decimal import Decimal, DecimalException, localcontext
from pathlib import Path
from typing import Any

from .data_calculator import (
    DECIMAL_PRECISION,
    CalculatorError,
    calculate_expression,
)
from .data_markdown import (
    MarkdownTableError,
    extract_markdown_tables,
    render_markdown_table,
)
from .os_operations import Workspace, WorkspaceError

DATA_SUFFIXES = frozenset({".csv", ".tsv", ".jsonl", ".ndjson", ".md", ".markdown"})
DATA_FORMATS = frozenset({"csv", "tsv", "json", "jsonl", "ndjson", "markdown"})
MAX_DATA_FILE_BYTES = 100_000_000
MAX_DATA_PAYLOAD_CHARS = 2_000_000
MAX_DATA_ROWS = 1_000_000
MAX_CSV_FIELD_CHARS = MAX_DATA_FILE_BYTES
MAX_RESULT_ROWS = 1_000
MAX_SAMPLE_ROWS = 20
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


class DataOperations:
    """Bounded, declarative operations for workspace-local tabular data.

    The filesystem boundary is delegated to the same Workspace instance used
    by the OS MCP server. This class deliberately does not evaluate Python,
    pandas expressions, SQL, regexes, lambdas, or model-provided code.
    """

    def __init__(self, workspace: Workspace | None) -> None:
        self.workspace = workspace

    @staticmethod
    def calculate(expression: str) -> str:
        try:
            result = calculate_expression(expression)
        except CalculatorError as exc:
            raise DataOperationError(str(exc)) from exc
        return json.dumps(
            {
                "expression": expression,
                "result": result,
            },
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _validate_suffix(path: Path) -> str:
        suffix = path.suffix.casefold()
        if suffix not in DATA_SUFFIXES:
            raise DataOperationError(
                "Nicht unterstütztes Datenformat. Erlaubt sind .csv, .tsv, "
                ".jsonl, .ndjson, .md und .markdown."
            )
        return suffix

    @staticmethod
    def _validate_limit(value: int, *, maximum: int, name: str = "limit") -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 1 <= value <= maximum
        ):
            raise DataOperationError(
                f"{name} muss zwischen 1 und {maximum} liegen."
            )
        return value

    @staticmethod
    def _validate_non_negative_limit(
        value: int,
        *,
        maximum: int,
        name: str,
    ) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= maximum
        ):
            raise DataOperationError(
                f"{name} muss zwischen 0 und {maximum} liegen."
            )
        return value

    @staticmethod
    def extract_markdown_tables(markdown: str) -> str:
        if not isinstance(markdown, str):
            raise DataOperationError("markdown muss ein String sein.")
        if len(markdown) > MAX_DATA_PAYLOAD_CHARS:
            raise DataOperationError(
                "Markdown überschreitet das Limit von "
                f"{MAX_DATA_PAYLOAD_CHARS} Zeichen."
            )
        try:
            tables = extract_markdown_tables(
                markdown,
                max_rows=MAX_DATA_ROWS,
            )
            if not tables:
                return "No Markdown tables found."
            rendered: list[str] = []
            for table in tables:
                rendered.append(f"## Table {table['index']}")
                if table.get("context"):
                    rendered.append(f"Context: {table['context']}")
                rows = list(table["rows"])
                preserve_scalar_types = bool(
                    table.get("preserved_string_cells")
                ) or any(
                    isinstance(value, Decimal)
                    for row in rows
                    for value in row.values()
                )
                rendered.append(
                    render_markdown_table(
                        list(table["columns"]),
                        rows,
                        preserve_scalar_types=preserve_scalar_types,
                    )
                )
            return "\n\n".join(rendered)
        except MarkdownTableError as exc:
            raise DataOperationError(str(exc)) from exc

    @staticmethod
    def _read_table(table: dict[str, Any]) -> tuple[list[str], list[Record]]:
        if not isinstance(table, dict):
            raise DataOperationError("table muss ein Objekt sein.")
        columns = table.get("columns")
        raw_rows = table.get("rows")
        if (
            not isinstance(columns, list)
            or not columns
            or not all(isinstance(column, str) and column for column in columns)
            or len(columns) != len(set(columns))
        ):
            raise DataOperationError(
                "table.columns muss eine nicht-leere Liste eindeutiger "
                "Spaltennamen sein."
            )
        if not isinstance(raw_rows, list):
            raise DataOperationError("table.rows muss eine Liste sein.")
        if len(raw_rows) > MAX_DATA_ROWS:
            raise DataOperationError(
                "Tabellendaten überschreiten das Zeilenlimit von "
                f"{MAX_DATA_ROWS}."
            )
        rows: list[Record] = []
        for index, raw_row in enumerate(raw_rows, start=1):
            if not isinstance(raw_row, dict):
                raise DataOperationError(
                    f"table.rows Eintrag {index} muss ein Objekt sein."
                )
            extras = set(raw_row) - set(columns)
            if extras:
                raise DataOperationError(
                    f"table.rows Eintrag {index} enthält unbekannte Spalten: "
                    + ", ".join(sorted(str(value) for value in extras))
                )
            rows.append(
                {
                    column: _json_scalar(raw_row.get(column))
                    for column in columns
                }
            )
        return list(columns), rows

    @classmethod
    def _read_markdown_text(
        cls,
        data: str,
        *,
        table_index: int,
    ) -> tuple[list[str], list[Record]]:
        if (
            isinstance(table_index, bool)
            or not isinstance(table_index, int)
            or table_index < 0
        ):
            raise DataOperationError(
                "table_index muss eine nicht-negative ganze Zahl sein."
            )
        try:
            tables = extract_markdown_tables(
                data,
                max_rows=MAX_DATA_ROWS,
            )
        except MarkdownTableError as exc:
            raise DataOperationError(str(exc)) from exc
        if table_index >= len(tables):
            raise DataOperationError(
                f"Markdown enthält keine Tabelle mit Index {table_index}; "
                f"gefunden: {len(tables)}."
            )
        table = tables[table_index]
        columns, rows = cls._read_table(table)
        preserved_strings = {
            tuple(cell)
            for cell in table.get("preserved_string_cells", [])
        }
        return columns, [
            {
                column: (
                    value
                    if (row_index, column_index) in preserved_strings
                    or not isinstance(value, str)
                    else _infer_csv_scalar(value)
                )
                for column_index, (column, value) in enumerate(row.items())
            }
            for row_index, row in enumerate(rows)
        ]

    def _read_records(
        self,
        path: str | None = None,
        *,
        data: str | None = None,
        data_format: str | None = None,
        table_index: int = 0,
    ) -> tuple[list[str], list[Record]]:
        source_count = sum(source is not None for source in (path, data))
        if source_count != 1:
            raise DataOperationError(
                "Genau eine Datenquelle muss angegeben werden: path oder data."
            )

        if data is not None:
            if not isinstance(data, str):
                raise DataOperationError("data muss ein String sein.")
            if len(data) > MAX_DATA_PAYLOAD_CHARS:
                raise DataOperationError(
                    "Inline-Daten überschreiten das Limit von "
                    f"{MAX_DATA_PAYLOAD_CHARS} Zeichen."
                )
            if not isinstance(data_format, str):
                raise DataOperationError(
                    "Für Inline-Daten muss data_format angegeben werden."
                )
            normalized_format = data_format.casefold()
            if normalized_format not in DATA_FORMATS:
                raise DataOperationError(
                    "Nicht unterstütztes Inline-Datenformat. Erlaubt sind "
                    "csv, tsv, json, jsonl, ndjson und markdown."
                )
            if normalized_format == "markdown":
                return self._read_markdown_text(
                    data,
                    table_index=table_index,
                )
            if table_index != 0:
                raise DataOperationError(
                    "table_index ist nur für Markdown-Daten zulässig."
                )
            if normalized_format in {"csv", "tsv"}:
                return self._read_delimited_text(
                    data,
                    delimiter="," if normalized_format == "csv" else "\t",
                )
            if normalized_format == "json":
                return self._read_json_array(data)
            return self._read_json_lines_text(data)

        if data_format is not None:
            raise DataOperationError(
                "data_format darf nur zusammen mit data verwendet werden."
            )
        if self.workspace is None:
            raise DataOperationError(
                "Dateizugriff ist für den Data-MCP nicht aktiviert. "
                "Verwende Inline-Daten oder starte cli-agent zusätzlich mit "
                "--with-os-read bzw. --with-os-write."
            )

        file_path = self.workspace.resolve_readable_file(path, direct=True)
        suffix = self._validate_suffix(file_path)
        try:
            size = file_path.stat().st_size
        except OSError as exc:
            raise DataOperationError(
                f"Dateigröße konnte nicht gelesen werden: {path!r}: {exc}"
            ) from exc
        if size > MAX_DATA_FILE_BYTES:
            raise DataOperationError(
                "Datendatei überschreitet das Limit von "
                f"{MAX_DATA_FILE_BYTES} Bytes: {path!r}"
            )

        if suffix in {".md", ".markdown"}:
            try:
                text = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise DataOperationError(
                    f"Datendatei ist nicht als UTF-8 lesbar: {file_path.name!r}"
                ) from exc
            except OSError as exc:
                raise DataOperationError(
                    f"Datendatei konnte nicht gelesen werden: "
                    f"{file_path.name!r}: {exc}"
                ) from exc
            return self._read_markdown_text(
                text,
                table_index=table_index,
            )

        if table_index != 0:
            raise DataOperationError(
                "table_index ist nur für Markdown-Daten zulässig."
            )
        if suffix in {".csv", ".tsv"}:
            return self._read_delimited(
                file_path,
                delimiter="," if suffix == ".csv" else "\t",
            )
        return self._read_json_lines(file_path)

    @staticmethod
    def _read_delimited(
        file_path: Path,
        *,
        delimiter: str,
    ) -> tuple[list[str], list[Record]]:
        try:
            _configure_csv_field_limit()
            with file_path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle, delimiter=delimiter)
                if reader.fieldnames is None:
                    return [], []
                columns = [
                    _validate_column_name(name, source="CSV/TSV")
                    for name in reader.fieldnames
                ]
                if len(columns) != len(set(columns)):
                    raise DataOperationError(
                        "CSV/TSV benötigt eindeutige, nicht-leere Spaltennamen."
                    )
                rows: list[Record] = []
                for index, raw in enumerate(reader, start=1):
                    if index > MAX_DATA_ROWS:
                        raise DataOperationError(
                            "Datendatei überschreitet das Zeilenlimit von "
                            f"{MAX_DATA_ROWS}."
                        )
                    if None in raw:
                        raise DataOperationError(
                            "CSV/TSV enthält mehr Felder als die Kopfzeile definiert."
                        )
                    rows.append(
                        {
                            column: _infer_csv_scalar(raw.get(column, ""))
                            for column in columns
                        }
                    )
                return columns, rows
        except UnicodeDecodeError as exc:
            raise DataOperationError(
                f"Datendatei ist nicht als UTF-8 lesbar: {file_path.name!r}"
            ) from exc
        except csv.Error as exc:
            raise DataOperationError(
                f"CSV/TSV konnte nicht geparst werden: {file_path.name!r}: {exc}"
            ) from exc
        except OSError as exc:
            raise DataOperationError(
                f"Datendatei konnte nicht gelesen werden: {file_path.name!r}: {exc}"
            ) from exc

    @staticmethod
    def _read_delimited_text(
        data: str,
        *,
        delimiter: str,
    ) -> tuple[list[str], list[Record]]:
        try:
            _configure_csv_field_limit()
            reader = csv.DictReader(io.StringIO(data), delimiter=delimiter)
            if reader.fieldnames is None:
                return [], []
            columns = [
                _validate_column_name(name, source="CSV/TSV")
                for name in reader.fieldnames
            ]
            if len(columns) != len(set(columns)):
                raise DataOperationError(
                    "CSV/TSV benötigt eindeutige, nicht-leere Spaltennamen."
                )
            rows: list[Record] = []
            for index, raw in enumerate(reader, start=1):
                if index > MAX_DATA_ROWS:
                    raise DataOperationError(
                        "Datendaten überschreiten das Zeilenlimit von "
                        f"{MAX_DATA_ROWS}."
                    )
                if None in raw:
                    raise DataOperationError(
                        "CSV/TSV enthält mehr Felder als die Kopfzeile definiert."
                    )
                rows.append(
                    {
                        column: _infer_csv_scalar(raw.get(column, ""))
                        for column in columns
                    }
                )
            return columns, rows
        except csv.Error as exc:
            raise DataOperationError(
                f"CSV/TSV konnte nicht geparst werden: {exc}"
            ) from exc

    @staticmethod
    def _normalize_json_records(raw_rows: Any) -> tuple[list[str], list[Record]]:
        if not isinstance(raw_rows, list):
            raise DataOperationError(
                "JSON-Inline-Daten müssen ein Array von Objekten enthalten."
            )
        if len(raw_rows) > MAX_DATA_ROWS:
            raise DataOperationError(
                "Datendaten überschreiten das Zeilenlimit von "
                f"{MAX_DATA_ROWS}."
            )
        columns: list[str] = []
        known_columns: set[str] = set()
        rows: list[Record] = []
        for index, raw in enumerate(raw_rows, start=1):
            if not isinstance(raw, dict):
                raise DataOperationError(
                    f"JSON-Eintrag {index} muss ein Objekt enthalten."
                )
            record: Record = {}
            for raw_name, raw_value in raw.items():
                column = _validate_column_name(
                    raw_name,
                    source=f"JSON-Eintrag {index}",
                )
                if column not in known_columns:
                    known_columns.add(column)
                    columns.append(column)
                record[column] = _json_scalar(raw_value)
            rows.append(record)
        return columns, rows

    @classmethod
    def _read_json_array(cls, data: str) -> tuple[list[str], list[Record]]:
        try:
            raw = json.loads(data, parse_constant=_reject_json_constant)
        except json.JSONDecodeError as exc:
            raise DataOperationError(
                f"Ungültiges JSON: {exc.msg}"
            ) from exc
        return cls._normalize_json_records(raw)

    @classmethod
    def _read_json_lines_text(
        cls,
        data: str,
    ) -> tuple[list[str], list[Record]]:
        raw_rows: list[Any] = []
        for line_number, line in enumerate(data.splitlines(), start=1):
            if not line.strip():
                continue
            if len(raw_rows) >= MAX_DATA_ROWS:
                raise DataOperationError(
                    "Datendaten überschreiten das Zeilenlimit von "
                    f"{MAX_DATA_ROWS}."
                )
            try:
                raw_rows.append(json.loads(line, parse_constant=_reject_json_constant))
            except json.JSONDecodeError as exc:
                raise DataOperationError(
                    f"Ungültiges JSON in Zeile {line_number}: {exc.msg}"
                ) from exc
        return cls._normalize_json_records(raw_rows)

    @staticmethod
    def _read_json_lines(file_path: Path) -> tuple[list[str], list[Record]]:
        rows: list[Record] = []
        columns: list[str] = []
        known_columns: set[str] = set()
        try:
            with file_path.open("r", encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    if len(rows) >= MAX_DATA_ROWS:
                        raise DataOperationError(
                            "Datendatei überschreitet das Zeilenlimit von "
                            f"{MAX_DATA_ROWS}."
                        )
                    try:
                        raw = json.loads(line, parse_constant=_reject_json_constant)
                    except json.JSONDecodeError as exc:
                        raise DataOperationError(
                            f"Ungültiges JSON in Zeile {line_number}: {exc.msg}"
                        ) from exc
                    if not isinstance(raw, dict):
                        raise DataOperationError(
                            f"JSONL-Zeile {line_number} muss ein Objekt enthalten."
                        )
                    record: Record = {}
                    for raw_name, raw_value in raw.items():
                        column = _validate_column_name(
                            raw_name,
                            source=f"JSONL-Zeile {line_number}",
                        )
                        if column not in known_columns:
                            known_columns.add(column)
                            columns.append(column)
                        record[column] = _json_scalar(raw_value)
                    rows.append(record)
        except UnicodeDecodeError as exc:
            raise DataOperationError(
                f"Datendatei ist nicht als UTF-8 lesbar: {file_path.name!r}"
            ) from exc
        except OSError as exc:
            raise DataOperationError(
                f"Datendatei konnte nicht gelesen werden: {file_path.name!r}: {exc}"
            ) from exc

        return columns, rows

    def _write_records(
        self,
        path: str,
        *,
        columns: list[str],
        rows: list[Record],
    ) -> str:
        if self.workspace is None:
            raise DataOperationError(
                "Dateischreibzugriff ist für den Data-MCP nicht aktiviert."
            )
        file_path = self.workspace.resolve_writable_file(path)
        suffix = self._validate_suffix(file_path)
        try:
            if suffix in {".csv", ".tsv"}:
                delimiter = "," if suffix == ".csv" else "\t"
                with file_path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(
                        handle,
                        fieldnames=columns,
                        delimiter=delimiter,
                        extrasaction="raise",
                    )
                    writer.writeheader()
                    writer.writerows(rows)
            elif suffix in {".md", ".markdown"}:
                file_path.write_text(
                    render_markdown_table(
                        columns,
                        rows,
                        preserve_scalar_types=True,
                    )
                    + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
            else:
                with file_path.open(
                    "w",
                    encoding="utf-8",
                    newline="\n",
                ) as handle:
                    for row in rows:
                        handle.write(_json_dumps_precise(row))
                        handle.write("\n")
        except (OSError, csv.Error, TypeError, ValueError) as exc:
            raise DataOperationError(
                f"Datendatei konnte nicht geschrieben werden: {path!r}: {exc}"
            ) from exc
        relative = file_path.relative_to(self.workspace.directory).as_posix()
        return json.dumps(
            {
                "output_path": relative,
                "rows_written": len(rows),
                "columns": columns,
            },
            ensure_ascii=False,
            indent=2,
        )

    @staticmethod
    def _require_columns(
        columns: list[str],
        requested: list[str] | None,
    ) -> list[str]:
        if requested is None:
            return list(columns)
        if not isinstance(requested, list) or not requested:
            raise DataOperationError("columns muss eine nicht-leere Liste sein.")
        normalized: list[str] = []
        seen: set[str] = set()
        for value in requested:
            if not isinstance(value, str) or not value:
                raise DataOperationError(
                    "columns darf nur nicht-leere Strings enthalten."
                )
            if value not in columns:
                raise DataOperationError(f"Unbekannte Spalte: {value!r}")
            if value in seen:
                raise DataOperationError(
                    f"Spalte mehrfach angegeben: {value!r}"
                )
            seen.add(value)
            normalized.append(value)
        return normalized

    @staticmethod
    def _validate_filter(
        filter_spec: dict[str, Any],
        columns: list[str],
    ) -> tuple[str, str, Any]:
        if not isinstance(filter_spec, dict):
            raise DataOperationError("Jeder Filter muss ein Objekt sein.")
        extras = set(filter_spec) - {"column", "op", "value"}
        if extras:
            raise DataOperationError(
                "Unbekannte Filterfelder: "
                + ", ".join(sorted(str(value) for value in extras))
            )
        column = filter_spec.get("column")
        operator = filter_spec.get("op")
        value = filter_spec.get("value")
        if not isinstance(column, str) or column not in columns:
            raise DataOperationError(
                f"Unbekannte Filterspalte: {column!r}"
            )
        if not isinstance(operator, str) or operator not in FILTER_OPERATORS:
            raise DataOperationError(
                f"Nicht unterstützter Filteroperator: {operator!r}. "
                f"Erlaubt: {', '.join(sorted(FILTER_OPERATORS))}."
            )
        if operator in {"is_null", "not_null"}:
            return column, operator, None
        if operator in {"in", "not_in"}:
            if not isinstance(value, list) or not value:
                raise DataOperationError(
                    f"{operator} benötigt value als nicht-leere Liste."
                )
            return column, operator, [_json_scalar(item) for item in value]
        if operator == "contains":
            if not isinstance(value, str):
                raise DataOperationError(
                    "contains benötigt einen String als value."
                )
            return column, operator, value
        return column, operator, _json_scalar(value)

    @classmethod
    def _apply_filters(
        cls,
        rows: list[Record],
        columns: list[str],
        filters: list[dict[str, Any]] | None,
    ) -> list[Record]:
        if filters is None:
            return list(rows)
        if not isinstance(filters, list):
            raise DataOperationError("filters muss eine Liste sein.")
        validated = [
            cls._validate_filter(spec, columns)
            for spec in filters
        ]

        def matches(row: Record) -> bool:
            for column, operator, expected in validated:
                actual = row.get(column)
                try:
                    if operator == "eq" and not _typed_equal(actual, expected):
                        return False
                    if operator == "ne" and _typed_equal(actual, expected):
                        return False
                    if operator == "lt" and not (
                        actual is not None and actual < expected
                    ):
                        return False
                    if operator == "lte" and not (
                        actual is not None and actual <= expected
                    ):
                        return False
                    if operator == "gt" and not (
                        actual is not None and actual > expected
                    ):
                        return False
                    if operator == "gte" and not (
                        actual is not None and actual >= expected
                    ):
                        return False
                    if operator == "in" and not any(
                        _typed_equal(actual, candidate)
                        for candidate in expected
                    ):
                        return False
                    if operator == "not_in" and any(
                        _typed_equal(actual, candidate)
                        for candidate in expected
                    ):
                        return False
                    if operator == "is_null" and actual is not None:
                        return False
                    if operator == "not_null" and actual is None:
                        return False
                    if operator == "contains" and (
                        actual is None or str(expected) not in str(actual)
                    ):
                        return False
                except TypeError as exc:
                    raise DataOperationError(
                        "Filtervergleich für Spalte "
                        f"{column!r} ist für die vorhandenen Datentypen "
                        "nicht möglich."
                    ) from exc
            return True

        return [row for row in rows if matches(row)]

    @staticmethod
    def _sort_rows(
        rows: list[Record],
        columns: list[str],
        sort_by: list[str] | None,
        *,
        descending: bool,
    ) -> list[Record]:
        if sort_by is None:
            return list(rows)
        selected = DataOperations._require_columns(columns, sort_by)
        ordered = list(rows)
        try:
            for column in reversed(selected):
                present = [
                    row
                    for row in ordered
                    if row.get(column) is not None
                ]
                missing = [
                    row
                    for row in ordered
                    if row.get(column) is None
                ]
                present.sort(
                    key=lambda row: row.get(column),
                    reverse=bool(descending),
                )
                ordered = present + missing
            return ordered
        except TypeError as exc:
            raise DataOperationError(
                "Sortierung ist wegen gemischter Datentypen nicht möglich."
            ) from exc

    @staticmethod
    def _result(
        columns: list[str],
        rows: list[Record],
        *,
        total_rows: int,
        limit: int,
    ) -> str:
        returned = rows[:limit]
        prefix = (
            f"Rows: {len(returned)}/{total_rows}"
            + (" (truncated)" if total_rows > limit else "")
        )
        try:
            table = render_markdown_table(
                columns,
                returned,
                preserve_scalar_types=True,
            )
        except MarkdownTableError as exc:
            raise DataOperationError(str(exc)) from exc
        return prefix + "\n\n" + table

    @staticmethod
    def _column_metadata(
        columns: list[str],
        rows: list[Record],
        *,
        distinct_values_limit: int | None = None,
    ) -> list[dict[str, Any]]:
        column_stats = {
            column: {
                "present_count": 0,
                "null_count": 0,
                "values": [],
                "unique": {},
            }
            for column in columns
        }
        for row in rows:
            for column, value in row.items():
                stats = column_stats[column]
                stats["present_count"] += 1
                if value is None:
                    stats["null_count"] += 1
                else:
                    stats["values"].append(value)
                identity = _scalar_identity(value)
                stats["unique"].setdefault(identity, value)

        metadata: list[dict[str, Any]] = []
        distinct_chars_used = 0
        for column in columns:
            stats = column_stats[column]
            missing_count = len(rows) - stats["present_count"]
            unique = stats["unique"]
            if missing_count:
                unique.setdefault(_scalar_identity(None), None)

            entry: dict[str, Any] = {
                "name": column,
                "dtype": _dtype_name(stats["values"]),
                "null_count": stats["null_count"] + missing_count,
                "unique_count": len(unique),
            }
            if distinct_values_limit is not None:
                if distinct_values_limit == 0:
                    # Zero explicitly suppresses distinct values, including for
                    # empty/header-only datasets whose unique domain is empty.
                    entry["distinct_values_complete"] = False
                elif len(unique) <= distinct_values_limit:
                    distinct_values = list(unique.values())
                    serialized_sizes = [
                        _json_scalar_char_count(value)
                        for value in distinct_values
                    ]
                    domain_chars = (
                        2
                        + sum(serialized_sizes)
                        + max(0, len(serialized_sizes) - 1)
                    )
                    value_too_large = any(
                        size > MAX_SCHEMA_DISTINCT_VALUE_CHARS
                        for size in serialized_sizes
                    )
                    total_too_large = (
                        distinct_chars_used + domain_chars
                        > MAX_SCHEMA_DISTINCT_TOTAL_CHARS
                    )
                    if not value_too_large and not total_too_large:
                        entry["distinct_values"] = distinct_values
                        entry["distinct_values_complete"] = True
                        distinct_chars_used += domain_chars
                    else:
                        # Never return a partial domain. Size-bounded omission
                        # keeps schema inspection below normal MCP result limits
                        # even for low-cardinality columns with huge values.
                        entry["distinct_values_complete"] = False
                else:
                    # Never return a partial domain: it is too easy for a model
                    # to mistake a truncated list for the complete set.
                    entry["distinct_values_complete"] = False
            metadata.append(entry)
        return metadata

    @staticmethod
    def _bounded_schema_result(
        *,
        source: str,
        row_count: int,
        column_count: int,
        metadata: list[dict[str, Any]],
    ) -> str:
        """Serialize schema metadata within the dedicated result budget.

        Column metadata has priority over optional distinct-value domains. If
        the complete result would be too large, distinct domains are omitted
        first. Only if the metadata without domains still exceeds the budget is
        the returned column list truncated. Truncation is explicit through
        column_count and columns_complete.
        """

        def without_distinct_values(entry: dict[str, Any]) -> dict[str, Any]:
            stripped = dict(entry)
            if "distinct_values" in stripped:
                stripped.pop("distinct_values")
                stripped["distinct_values_complete"] = False
            return stripped

        minimal_metadata = [
            without_distinct_values(entry)
            for entry in metadata
        ]
        minimal_texts = [
            _json_dumps_precise(entry)
            for entry in minimal_metadata
        ]

        # Measure against the compact final wire representation. Use false for
        # columns_complete while budgeting because it is one character longer
        # than true and therefore a conservative fixed-overhead estimate.
        empty_result = _json_dumps_precise(
            {
                "source": source,
                "row_count": row_count,
                "column_count": column_count,
                "columns": [],
                "columns_complete": False,
            }
        )
        fixed_chars = len(empty_result) - 2  # exclude the empty [] payload

        def result_chars(texts: list[str]) -> int:
            return (
                fixed_chars
                + 2
                + sum(len(text) for text in texts)
                + max(0, len(texts) - 1)
            )

        if result_chars(minimal_texts) <= MAX_SCHEMA_RESULT_CHARS:
            selected = list(minimal_metadata)
            selected_texts = list(minimal_texts)
            used_chars = result_chars(selected_texts)

            # Add complete distinct domains only when they still fit after all
            # columns' basic metadata has been reserved.
            for index, entry in enumerate(metadata):
                if "distinct_values" not in entry:
                    continue
                full_text = _json_dumps_precise(entry)
                delta = len(full_text) - len(selected_texts[index])
                if used_chars + delta <= MAX_SCHEMA_RESULT_CHARS:
                    selected[index] = entry
                    selected_texts[index] = full_text
                    used_chars += delta

            result = {
                "source": source,
                "row_count": row_count,
                "column_count": column_count,
                "columns": selected,
                "columns_complete": True,
            }
            rendered = _json_dumps_precise(result)
            if len(rendered) > MAX_SCHEMA_RESULT_CHARS:
                raise AssertionError("Schema result budget calculation is inconsistent.")
            return rendered

        selected: list[dict[str, Any]] = []
        selected_texts: list[str] = []
        used_chars = fixed_chars + 2
        for entry, entry_text in zip(
            minimal_metadata,
            minimal_texts,
            strict=True,
        ):
            delta = len(entry_text) + (1 if selected_texts else 0)
            if used_chars + delta > MAX_SCHEMA_RESULT_CHARS:
                break
            selected.append(entry)
            selected_texts.append(entry_text)
            used_chars += delta

        result = {
            "source": source,
            "row_count": row_count,
            "column_count": column_count,
            "columns": selected,
            "columns_complete": len(selected) == column_count,
        }
        rendered = _json_dumps_precise(result)
        if len(rendered) > MAX_SCHEMA_RESULT_CHARS:
            raise AssertionError("Schema result budget calculation is inconsistent.")
        return rendered

    def inspect_schema(
        self,
        path: str | None = None,
        *,
        data: str | None = None,
        data_format: str | None = None,
        table_index: int = 0,
        distinct_values_limit: int = DEFAULT_DISTINCT_VALUES_LIMIT,
    ) -> str:
        """Return schema/profile metadata without exposing any dataset rows."""
        distinct_values_limit = self._validate_non_negative_limit(
            distinct_values_limit,
            maximum=MAX_DISTINCT_VALUES_LIMIT,
            name="distinct_values_limit",
        )
        columns, rows = self._read_records(
            path,
            data=data,
            data_format=data_format,
            table_index=table_index,
        )
        metadata = self._column_metadata(
            columns,
            rows,
            distinct_values_limit=distinct_values_limit,
        )
        source = (
            path
            if path is not None
            else f"inline:{data_format}"
        )
        return self._bounded_schema_result(
            source=source,
            row_count=len(rows),
            column_count=len(columns),
            metadata=metadata,
        )

    def inspect_data(
        self,
        path: str | None = None,
        *,
        data: str | None = None,
        data_format: str | None = None,
        table_index: int = 0,
        sample_rows: int = 5,
    ) -> str:
        """Return schema metadata plus a bounded preview of complete rows."""
        sample_rows = self._validate_limit(
            sample_rows,
            maximum=MAX_SAMPLE_ROWS,
            name="sample_rows",
        )
        columns, rows = self._read_records(
            path,
            data=data,
            data_format=data_format,
            table_index=table_index,
        )
        metadata = self._column_metadata(columns, rows)
        return _json_dumps_precise(
            {
                "source": (
                    path
                    if path is not None
                    else f"inline:{data_format}"
                ),
                "row_count": len(rows),
                "columns": metadata,
                "sample": rows[:sample_rows],
            },
            indent=2,
        )

    def select_data(
        self,
        path: str | None = None,
        *,
        data: str | None = None,
        data_format: str | None = None,
        table_index: int = 0,
        columns: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        sort_by: list[str] | None = None,
        descending: bool = False,
        limit: int = 100,
    ) -> str:
        limit = self._validate_limit(limit, maximum=MAX_RESULT_ROWS)
        source_columns, rows = self._read_records(
            path,
            data=data,
            data_format=data_format,
            table_index=table_index,
        )
        selected_columns = self._require_columns(source_columns, columns)
        filtered = self._apply_filters(rows, source_columns, filters)
        ordered = self._sort_rows(
            filtered,
            source_columns,
            sort_by,
            descending=descending,
        )
        return self._result(
            selected_columns,
            ordered,
            total_rows=len(ordered),
            limit=limit,
        )

    @staticmethod
    def _aggregation_value(
        values: list[Scalar],
        function: str,
        column: str,
    ) -> Scalar:
        present = [value for value in values if value is not None]
        if function == "count":
            return len(present)
        if function == "nunique":
            return len(
                {_scalar_identity(value) for value in present}
            )
        if function in {"sum", "mean", "median", "std"}:
            if any(
                isinstance(value, bool)
                or not isinstance(value, (int, float, Decimal))
                for value in present
            ):
                raise DataOperationError(
                    f"Aggregation {function!r} benötigt eine numerische "
                    f"Spalte: {column!r}"
                )

            if not present:
                return 0 if function == "sum" else None

            integer_only = all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in present
            )
            if function == "sum" and integer_only:
                return sum(present)

            use_decimal = integer_only or any(
                isinstance(value, Decimal)
                for value in present
            )
            if not use_decimal:
                numeric = [
                    float(value)
                    for value in present
                ]
                try:
                    if function == "sum":
                        result = sum(numeric)
                    elif function == "mean":
                        result = statistics.fmean(numeric)
                    elif function == "median":
                        result = statistics.median(numeric)
                    else:
                        result = (
                            statistics.stdev(numeric)
                            if len(numeric) >= 2
                            else None
                        )
                except (ArithmeticError, ValueError) as exc:
                    raise DataOperationError(
                        f"Aggregation {function!r} konnte für Spalte "
                        f"{column!r} nicht berechnet werden."
                    ) from exc

                if result is None:
                    return None
                return _require_finite_float_result(
                    float(result),
                    function=function,
                    column=column,
                )

            decimal_values = [
                _decimal_value(value)
                for value in present
            ]
            try:
                with localcontext() as context:
                    context.prec = _decimal_working_precision(decimal_values)
                    if function == "sum":
                        return sum(decimal_values, Decimal(0))
                    if function == "mean":
                        return statistics.mean(decimal_values)
                    if function == "median":
                        return statistics.median(decimal_values)
                    return (
                        statistics.stdev(decimal_values)
                        if len(decimal_values) >= 2
                        else None
                    )
            except (
                DecimalException,
                ArithmeticError,
                ValueError,
                OverflowError,
            ) as exc:
                raise DataOperationError(
                    f"Aggregation {function!r} konnte für Spalte "
                    f"{column!r} nicht präzise berechnet werden."
                ) from exc
        if not present:
            return None
        try:
            return min(present) if function == "min" else max(present)
        except TypeError as exc:
            raise DataOperationError(
                f"Aggregation {function!r} ist wegen gemischter Datentypen "
                f"für Spalte {column!r} nicht möglich."
            ) from exc

    @staticmethod
    def _validate_aggregations(
        aggregations: list[dict[str, str]],
        columns: list[str],
        group_by: list[str],
    ) -> list[tuple[str, str, str]]:
        if not isinstance(aggregations, list) or not aggregations:
            raise DataOperationError(
                "aggregations muss eine nicht-leere Liste sein."
            )
        result: list[tuple[str, str, str]] = []
        aliases = set(group_by)
        for spec in aggregations:
            if not isinstance(spec, dict):
                raise DataOperationError(
                    "Jede Aggregation muss ein Objekt sein."
                )
            extras = set(spec) - {"column", "function", "alias"}
            if extras:
                raise DataOperationError(
                    "Unbekannte Aggregationsfelder: "
                    + ", ".join(
                        sorted(str(value) for value in extras)
                    )
                )
            column = spec.get("column")
            function = spec.get("function")
            alias = spec.get("alias")
            if not isinstance(column, str) or column not in columns:
                raise DataOperationError(
                    f"Unbekannte Aggregationsspalte: {column!r}"
                )
            if (
                not isinstance(function, str)
                or function not in AGGREGATION_FUNCTIONS
            ):
                raise DataOperationError(
                    f"Nicht unterstützte Aggregation: {function!r}. "
                    f"Erlaubt: {', '.join(sorted(AGGREGATION_FUNCTIONS))}."
                )
            if alias is None:
                alias = f"{column}_{function}"
            alias = _validate_column_name(
                alias,
                source="Aggregation-Alias",
            )
            if alias in aliases:
                raise DataOperationError(
                    f"Doppelter Ergebnis-Spaltenname: {alias!r}"
                )
            aliases.add(alias)
            result.append((column, function, alias))
        return result

    def _aggregate_records(
        self,
        path: str | None,
        *,
        data: str | None,
        data_format: str | None,
        table_index: int,
        group_by: list[str] | None,
        aggregations: list[dict[str, str]],
        filters: list[dict[str, Any]] | None,
        sort_by: list[str] | None,
        descending: bool,
    ) -> tuple[list[str], list[Record]]:
        columns, rows = self._read_records(
            path,
            data=data,
            data_format=data_format,
            table_index=table_index,
        )
        groups = (
            []
            if group_by is None
            else self._require_columns(columns, group_by)
        )
        specs = self._validate_aggregations(
            aggregations,
            columns,
            groups,
        )
        filtered = self._apply_filters(rows, columns, filters)

        grouped: dict[
            tuple[tuple[str, Scalar], ...],
            tuple[tuple[Scalar, ...], list[Record]],
        ] = {}
        if groups:
            for row in filtered:
                values = tuple(row.get(column) for column in groups)
                identity = tuple(
                    _scalar_identity(value)
                    for value in values
                )
                if identity not in grouped:
                    grouped[identity] = (values, [])
                grouped[identity][1].append(row)
        else:
            grouped[()] = ((), filtered)

        result_rows: list[Record] = []
        for key, group_rows in grouped.values():
            result: Record = {
                column: key[index]
                for index, column in enumerate(groups)
            }
            for column, function, alias in specs:
                result[alias] = self._aggregation_value(
                    [row.get(column) for row in group_rows],
                    function,
                    column,
                )
            result_rows.append(result)

        result_columns = groups + [
            alias
            for _, _, alias in specs
        ]
        ordered = self._sort_rows(
            result_rows,
            result_columns,
            sort_by,
            descending=descending,
        )
        return result_columns, ordered

    def aggregate_data(
        self,
        aggregations: list[dict[str, str]],
        path: str | None = None,
        *,
        data: str | None = None,
        data_format: str | None = None,
        table_index: int = 0,
        group_by: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        sort_by: list[str] | None = None,
        descending: bool = False,
        limit: int = 200,
    ) -> str:
        limit = self._validate_limit(limit, maximum=MAX_RESULT_ROWS)
        result_columns, rows = self._aggregate_records(
            path,
            data=data,
            data_format=data_format,
            table_index=table_index,
            group_by=group_by,
            aggregations=aggregations,
            filters=filters,
            sort_by=sort_by,
            descending=descending,
        )
        return self._result(
            result_columns,
            rows,
            total_rows=len(rows),
            limit=limit,
        )

    def value_counts(
        self,
        column: str,
        path: str | None = None,
        *,
        data: str | None = None,
        data_format: str | None = None,
        table_index: int = 0,
        filters: list[dict[str, Any]] | None = None,
        limit: int = 50,
    ) -> str:
        limit = self._validate_limit(
            limit,
            maximum=MAX_VALUE_COUNT_ROWS,
        )
        columns, rows = self._read_records(
            path,
            data=data,
            data_format=data_format,
            table_index=table_index,
        )
        if column not in columns:
            raise DataOperationError(
                f"Unbekannte Spalte: {column!r}"
            )
        filtered = self._apply_filters(rows, columns, filters)
        counts: dict[
            tuple[str, Scalar],
            tuple[Scalar, int],
        ] = {}
        for row in filtered:
            value = row.get(column)
            identity = _scalar_identity(value)
            if identity in counts:
                original, count = counts[identity]
                counts[identity] = (original, count + 1)
            else:
                counts[identity] = (value, 1)

        result_rows: list[Record] = [
            {"value": value, "count": count}
            for value, count in sorted(
                counts.values(),
                key=lambda item: item[1],
                reverse=True,
            )
        ]
        return self._result(
            ["value", "count"],
            result_rows,
            total_rows=len(result_rows),
            limit=limit,
        )

    def select_data_to_file(
        self,
        output_path: str,
        input_path: str | None = None,
        *,
        data: str | None = None,
        data_format: str | None = None,
        table_index: int = 0,
        columns: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        sort_by: list[str] | None = None,
        descending: bool = False,
    ) -> str:
        source_columns, rows = self._read_records(
            input_path,
            data=data,
            data_format=data_format,
            table_index=table_index,
        )
        selected_columns = self._require_columns(
            source_columns,
            columns,
        )
        filtered = self._apply_filters(
            rows,
            source_columns,
            filters,
        )
        ordered = self._sort_rows(
            filtered,
            source_columns,
            sort_by,
            descending=descending,
        )
        selected_set = set(selected_columns)
        projected = [
            {
                column: value
                for column, value in row.items()
                if column in selected_set
            }
            for row in ordered
        ]
        return self._write_records(
            output_path,
            columns=selected_columns,
            rows=projected,
        )

    def aggregate_data_to_file(
        self,
        output_path: str,
        aggregations: list[dict[str, str]],
        input_path: str | None = None,
        *,
        data: str | None = None,
        data_format: str | None = None,
        table_index: int = 0,
        group_by: list[str] | None = None,
        filters: list[dict[str, Any]] | None = None,
        sort_by: list[str] | None = None,
        descending: bool = False,
    ) -> str:
        columns, rows = self._aggregate_records(
            input_path,
            data=data,
            data_format=data_format,
            table_index=table_index,
            group_by=group_by,
            aggregations=aggregations,
            filters=filters,
            sort_by=sort_by,
            descending=descending,
        )
        return self._write_records(
            output_path,
            columns=columns,
            rows=rows,
        )
