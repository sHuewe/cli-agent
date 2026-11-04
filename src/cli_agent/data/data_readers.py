"""Bounded readers and data-format normalization for DataOperations."""
from __future__ import annotations
from cli_agent.data.data_core import *

class DataReadMixin:
    @staticmethod
    def extract_markdown_tables(markdown: str) -> str:
        if not isinstance(markdown, str):
            raise DataOperationError("markdown must be a string.")
        if len(markdown) > MAX_DATA_PAYLOAD_CHARS:
            raise DataOperationError(
                "Markdown exceeds the limit of "
                f"{MAX_DATA_PAYLOAD_CHARS} characters."
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
            raise DataOperationError("table must be an object.")
        columns = table.get("columns")
        raw_rows = table.get("rows")
        if (
            not isinstance(columns, list)
            or not columns
            or not all(isinstance(column, str) and column for column in columns)
            or len(columns) != len(set(columns))
        ):
            raise DataOperationError(
                "table.columns must be a nonempty list of unique "
                "column names."
            )
        if not isinstance(raw_rows, list):
            raise DataOperationError("table.rows must be a list.")
        if len(raw_rows) > MAX_DATA_ROWS:
            raise DataOperationError(
                "Tabular data exceed the row limit of "
                f"{MAX_DATA_ROWS}."
            )
        rows: list[Record] = []
        for index, raw_row in enumerate(raw_rows, start=1):
            if not isinstance(raw_row, dict):
                raise DataOperationError(
                    f"table.rows entry {index} must be an object."
                )
            extras = set(raw_row) - set(columns)
            if extras:
                raise DataOperationError(
                    f"table.rows entry {index} contains unknown columns: "
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
                "table_index must be a nonnegative integer."
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
                f"Markdown contains no table with index {table_index}; "
                f"found: {len(tables)}."
            )
        return cls._read_markdown_table(tables[table_index])

    @classmethod
    def _read_markdown_table(cls, table: dict[str, Any]) -> tuple[list[str], list[Record]]:
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

    @classmethod
    def wrap_data(cls, content: str) -> list[tuple[list[str], list[Record]]]:
        """Identify and normalize all unambiguous text datasets once.

        This is the only raw-text ingress of the MCP API. Existing workspace
        files may be passed directly to downstream path-based tools instead.
        """
        if not isinstance(content, str):
            raise DataOperationError("content must be a string.")
        if len(content) > MAX_DATA_PAYLOAD_CHARS or len(content.encode("utf-8")) > MAX_DATA_PAYLOAD_CHARS:
            raise DataOperationError("Inline data exceed the size limit.")
        normalized = content.lstrip("\ufeff")
        stripped = normalized.lstrip(" \t\r\n")
        if not stripped:
            raise DataOperationError("No dataset was recognized in the text.")

        if stripped[0] in "{[":
            try:
                parsed = json.loads(stripped, parse_float=Decimal, parse_constant=_reject_json_constant)
            except json.JSONDecodeError as exc:
                # JSONL and NDJSON have the same syntax: one JSON object per
                # non-empty line. A malformed JSON-looking document must not
                # silently fall back to Markdown or CSV.
                lines = [line for line in stripped.splitlines() if line.strip()]
                if len(lines) > 1:
                    try:
                        objects = [json.loads(line, parse_float=Decimal, parse_constant=_reject_json_constant) for line in lines]
                    except json.JSONDecodeError:
                        pass
                    else:
                        if all(isinstance(value, dict) for value in objects):
                            return [cls._normalize_json_records(objects)]
                raise DataOperationError(f"Invalid JSON/JSONL: {exc.msg}") from exc
            return cls._discover_inline_json(parsed)

        try:
            tables = extract_markdown_tables(content, max_rows=MAX_DATA_ROWS)
        except MarkdownTableError as exc:
            raise DataOperationError(str(exc)) from exc
        if tables:
            return [cls._read_markdown_table(table) for table in tables]

        # Delimited text needs a header and at least a line break. Refuse
        # arbitrary comma-containing prose rather than guessing a table.
        first_line = stripped.splitlines()[0]
        if "\n" in stripped or "\r" in stripped:
            if "\t" in first_line:
                return [cls._read_delimited_text(stripped, delimiter="\t")]
            if "," in first_line:
                return [cls._read_delimited_text(stripped, delimiter=",")]
        raise DataOperationError("No supported Markdown, JSON, CSV or TSV data format was recognized.")

    @classmethod
    def _discover_inline_json(cls, raw: Any) -> list[tuple[list[str], list[Record]]]:
        """Collect all eligible JSON record arrays, never just the first."""
        found: list[tuple[list[str], list[Record]]] = []
        visited = 0

        def visit(value: Any, depth: int, declared_columns: Any = None) -> None:
            nonlocal visited
            visited += 1
            if visited > 10_000 or depth > 16:
                raise DataOperationError("JSON structure is too complex for table discovery.")
            if isinstance(value, list):
                if value and all(isinstance(row, dict) for row in value):
                    try:
                        result = cls._normalize_json_records(value)
                    except DataOperationError:
                        pass
                    else:
                        found.append(result)
                        return
                if not value and isinstance(declared_columns, list) and declared_columns:
                    columns, rows = cls._read_table({"columns": declared_columns, "rows": []})
                    found.append((columns, rows))
                    return
                for item in value:
                    visit(item, depth + 1)
            elif isinstance(value, dict):
                for key, child in value.items():
                    if isinstance(child, (list, dict)):
                        visit(child, depth + 1, value.get("columns") if key == "rows" else None)

        visit(raw, 0)
        # A single flat object is also an unambiguous one-record JSONL/NDJSON
        # document. This matters for short captures with just one line.
        if not found and isinstance(raw, dict):
            try:
                columns, rows = cls._normalize_json_records([raw])
            except DataOperationError:
                pass
            else:
                found.append((columns, rows))
        if not found:
            raise DataOperationError("JSON contains no unambiguous arrays of tabular objects.")
        if len(found) > 20:
            raise DataOperationError("JSON contains too many tables (maximum 20).")
        return found

    def _read_records(
        self,
        path: str | None = None,
        *,
        data: str | None = None,
        data_format: str | None = None,
        table_index: int = 0,
        records_path: str | None = None,
    ) -> tuple[list[str], list[Record]]:
        source_count = sum(source is not None for source in (path, data))
        if source_count != 1:
            raise DataOperationError(
                "Exactly one data source must be provided: path or data."
            )

        if data is not None:
            if not isinstance(data, str):
                raise DataOperationError("data must be a string.")
            if len(data) > MAX_DATA_PAYLOAD_CHARS:
                raise DataOperationError(
                    "Inline data exceed the limit of "
                    f"{MAX_DATA_PAYLOAD_CHARS} characters."
                )
            if not isinstance(data_format, str):
                raise DataOperationError(
                    "data_format is required for inline data."
                )
            normalized_format = data_format.casefold()
            if normalized_format not in DATA_FORMATS:
                raise DataOperationError(
                    "Unsupported inline data format. Allowed formats: "
                    "csv, tsv, json, jsonl, ndjson and markdown."
                )
            if normalized_format == "markdown":
                if records_path is not None:
                    raise DataOperationError("records_path is allowed only for JSON.")
                return self._read_markdown_text(
                    data,
                    table_index=table_index,
                )
            if table_index != 0:
                raise DataOperationError(
                    "table_index is allowed only for Markdown data."
                )
            if normalized_format in {"csv", "tsv"}:
                if records_path is not None:
                    raise DataOperationError("records_path is allowed only for JSON.")
                return self._read_delimited_text(
                    data,
                    delimiter="," if normalized_format == "csv" else "\t",
                )
            if normalized_format == "json":
                return self._read_json_array(data, records_path=records_path)
            if records_path is not None:
                raise DataOperationError("records_path is allowed only for JSON.")
            return self._read_json_lines_text(data)

        if data_format is not None:
            raise DataOperationError(
                "data_format may only be used together with data."
            )
        if self.workspace is None:
            raise DataOperationError(
                "File access is not enabled for Data MCP. "
                "Use inline data or start cli-agent with "
                "--with-os-read or --with-os-write."
            )

        file_path = self.workspace.resolve_readable_file(path, direct=True)
        suffix = self._validate_suffix(file_path)
        if records_path is not None and suffix != ".json":
            raise DataOperationError("records_path is allowed only for JSON.")
        try:
            size = file_path.stat().st_size
        except OSError as exc:
            raise DataOperationError(
                f"Could not read file size: {path!r}: {exc}"
            ) from exc
        if size > MAX_DATA_FILE_BYTES:
            raise DataOperationError(
                "Data file exceeds the limit of "
                f"{MAX_DATA_FILE_BYTES} Bytes: {path!r}"
            )

        if suffix in {".md", ".markdown"}:
            try:
                text = file_path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise DataOperationError(
                    f"Data file is not readable as UTF-8: {file_path.name!r}"
                ) from exc
            except OSError as exc:
                raise DataOperationError(
                    f"Could not read data file: "
                    f"{file_path.name!r}: {exc}"
                ) from exc
            return self._read_markdown_text(
                text,
                table_index=table_index,
            )

        if table_index != 0:
            raise DataOperationError(
                "table_index is allowed only for Markdown data."
            )
        if suffix == ".json":
            try:
                return self._read_json_array(
                    file_path.read_text(encoding="utf-8"),
                    records_path=records_path,
                )
            except (UnicodeDecodeError, OSError) as exc:
                raise DataOperationError("Could not read JSON file as UTF-8.") from exc
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
                        "CSV/TSV requires unique, nonempty column names."
                    )
                rows: list[Record] = []
                for index, raw in enumerate(reader, start=1):
                    if index > MAX_DATA_ROWS:
                        raise DataOperationError(
                            "Data file exceeds the row limit of "
                            f"{MAX_DATA_ROWS}."
                        )
                    if None in raw:
                        raise DataOperationError(
                            "CSV/TSV contains more fields than the header defines."
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
                f"Data file is not readable as UTF-8: {file_path.name!r}"
            ) from exc
        except csv.Error as exc:
            raise DataOperationError(
                f"Could not parse CSV/TSV: {file_path.name!r}: {exc}"
            ) from exc
        except OSError as exc:
            raise DataOperationError(
                f"Could not read data file: {file_path.name!r}: {exc}"
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
                    "CSV/TSV requires unique, nonempty column names."
                )
            rows: list[Record] = []
            for index, raw in enumerate(reader, start=1):
                if index > MAX_DATA_ROWS:
                    raise DataOperationError(
                        "Dataset exceeds the row limit of "
                        f"{MAX_DATA_ROWS}."
                    )
                if None in raw:
                    raise DataOperationError(
                        "CSV/TSV contains more fields than the header defines."
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
                f"Could not parse CSV/TSV: {exc}"
            ) from exc

    @staticmethod
    def _normalize_json_records(raw_rows: Any) -> tuple[list[str], list[Record]]:
        if not isinstance(raw_rows, list):
            raise DataOperationError(
                "JSON data at the selected records_path must be an array of objects."
            )
        if len(raw_rows) > MAX_DATA_ROWS:
            raise DataOperationError(
                "Dataset exceeds the row limit of "
                f"{MAX_DATA_ROWS}."
            )
        columns: list[str] = []
        known_columns: set[str] = set()
        rows: list[Record] = []
        for index, raw in enumerate(raw_rows, start=1):
            if not isinstance(raw, dict):
                raise DataOperationError(
                    f"JSON entry {index} must contain an object."
                )
            record: Record = {}
            for raw_name, raw_value in raw.items():
                column = _validate_column_name(
                    raw_name,
                    source=f"JSON entry {index}",
                )
                if column not in known_columns:
                    known_columns.add(column)
                    columns.append(column)
                record[column] = _json_scalar(raw_value)
            rows.append(record)
        return columns, rows

    @classmethod
    def _read_json_array(
        cls, data: str, *, records_path: str | None = None
    ) -> tuple[list[str], list[Record]]:
        try:
            raw = json.loads(data, parse_float=Decimal, parse_constant=_reject_json_constant)
        except json.JSONDecodeError as exc:
            raise DataOperationError(
                f"Invalid JSON: {exc.msg}"
            ) from exc
        if isinstance(raw, dict) and raw.get("scalar_encoding") == SCALAR_ENCODING:
            # Generated artifacts encode genuine Decimal values explicitly;
            # their untagged JSON numbers originate from binary float64 and
            # must round-trip as floats, including subnormal values.
            raw = json.loads(data, parse_constant=_reject_json_constant)
        selected = cls._resolve_json_pointer(raw, records_path)
        if isinstance(raw, dict) and raw.get("scalar_encoding") == SCALAR_ENCODING and isinstance(selected, list):
            restored = []
            for row in selected:
                if not isinstance(row, dict):
                    restored.append(row)
                    continue
                restored.append({key: cls._restore_artifact_value(value) for key, value in row.items()})
            selected = restored
        columns, rows = cls._normalize_json_records(selected)
        # Generated single and multi-table artifacts declare their schema
        # beside /rows. Empty arrays cannot carry column names themselves;
        # recover them from the parent of /rows or /tables/N/rows.
        if not rows and isinstance(records_path, str) and records_path.endswith("/rows"):
            parent_pointer = records_path.rsplit("/", 1)[0]
            parent = cls._resolve_json_pointer(raw, parent_pointer)
            if isinstance(parent, dict) and "columns" in parent:
                declared = parent["columns"]
                if not isinstance(declared, list) or len(declared) > 100:
                    raise DataOperationError("JSON DataFrame requires a list of at most 100 column names.")
                columns = [
                    _validate_column_name(name, source="JSON-DataFrame-Schema")
                    for name in declared
                ]
                if len(columns) != len(set(columns)):
                    raise DataOperationError("JSON DataFrame schema requires unique column names.")
        return columns, rows

    @staticmethod
    def _restore_artifact_value(value: Any) -> Any:
        if isinstance(value, dict) and set(value) == {DECIMAL_SCALAR_KEY}:
            encoded = value[DECIMAL_SCALAR_KEY]
            if not isinstance(encoded, str):
                raise DataOperationError("Invalid decimal-value encoding.")
            try:
                return _json_scalar(Decimal(encoded))
            except (DecimalException, ValueError) as exc:
                raise DataOperationError("Invalid decimal-value encoding.") from exc
        return value

    @staticmethod
    def _resolve_json_pointer(raw: Any, pointer: str | None) -> Any:
        """Select a record array via an RFC 6901 JSON pointer (not JSONPath)."""
        if pointer is None or pointer == "":
            return raw
        if not isinstance(pointer, str) or not pointer.startswith("/"):
            raise DataOperationError("records_path must be a JSON Pointer starting with '/'.")
        parts = pointer[1:].split("/")
        if len(parts) > 32:
            raise DataOperationError("records_path is nested too deeply.")
        current = raw
        for part in parts:
            if "~" in part:
                if any(part[i:i+2] not in {"~0", "~1"} for i, c in enumerate(part) if c == "~"):
                    raise DataOperationError("Invalid JSON Pointer escape sequence.")
                part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(current, dict) and part in current:
                current = current[part]
            elif isinstance(current, list) and part.isascii() and part.isdecimal() and (part == "0" or not part.startswith("0")) and int(part) < len(current):
                current = current[int(part)]
            else:
                raise DataOperationError(f"records_path does not exist: {pointer!r}.")
        return current

    def inspect_json(self, path: str) -> str:
        """Discover JSON record arrays without returning their values."""
        if self.workspace is None:
            raise DataOperationError("File access is not enabled.")
        file_path = self.workspace.resolve_readable_file(path, direct=True)
        if file_path.suffix.casefold() != ".json":
            raise DataOperationError("inspect_json requires a .json file.")
        if file_path.stat().st_size > MAX_DATA_FILE_BYTES:
            raise DataOperationError("JSON file exceeds the size limit.")
        try:
            raw = json.loads(file_path.read_text(encoding="utf-8"), parse_constant=_reject_json_constant)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DataOperationError("Could not parse JSON file.") from exc
        discovered: list[dict[str, Any]] = []
        inspected = 0
        complete = True

        def visit(value: Any, pointer: str, depth: int) -> None:
            nonlocal inspected, complete
            if depth > 16 or inspected >= 10_000 or len(discovered) >= 50:
                complete = False
                return
            inspected += 1
            if isinstance(value, list):
                if value and all(isinstance(row, dict) for row in value):
                    # Only describe arrays that the regular tabular reader accepts.
                    try:
                        columns, _ = self._normalize_json_records(value)
                    except DataOperationError:
                        pass
                    else:
                        discovered.append({"records_path": pointer, "row_count": len(value), "columns": columns[:100], "columns_complete": len(columns) <= 100})
                        return
                if len(value) > 20:
                    complete = False
                for index, item in enumerate(value[:20]):
                    visit(item, f"{pointer}/{index}", depth + 1)
            elif isinstance(value, dict):
                if len(value) > 100:
                    complete = False
                for key, item in list(value.items())[:100]:
                    escaped = key.replace("~", "~0").replace("/", "~1")
                    visit(item, f"{pointer}/{escaped}", depth + 1)

        visit(raw, "", 0)
        return json.dumps({"source": path, "record_sets": discovered, "complete": complete}, ensure_ascii=False)

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
                    "Dataset exceeds the row limit of "
                    f"{MAX_DATA_ROWS}."
                )
            try:
                raw_rows.append(json.loads(line, parse_float=Decimal, parse_constant=_reject_json_constant))
            except json.JSONDecodeError as exc:
                raise DataOperationError(
                    f"Invalid JSON on line {line_number}: {exc.msg}"
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
                            "Data file exceeds the row limit of "
                            f"{MAX_DATA_ROWS}."
                        )
                    try:
                        raw = json.loads(line, parse_float=Decimal, parse_constant=_reject_json_constant)
                    except json.JSONDecodeError as exc:
                        raise DataOperationError(
                            f"Invalid JSON on line {line_number}: {exc.msg}"
                        ) from exc
                    if not isinstance(raw, dict):
                        raise DataOperationError(
                            f"JSONL line {line_number} must contain an object."
                        )
                    record: Record = {}
                    for raw_name, raw_value in raw.items():
                        column = _validate_column_name(
                            raw_name,
                            source=f"JSONL line {line_number}",
                        )
                        if column not in known_columns:
                            known_columns.add(column)
                            columns.append(column)
                        record[column] = _json_scalar(raw_value)
                    rows.append(record)
        except UnicodeDecodeError as exc:
            raise DataOperationError(
                f"Data file is not readable as UTF-8: {file_path.name!r}"
            ) from exc
        except OSError as exc:
            raise DataOperationError(
                f"Could not read data file: {file_path.name!r}: {exc}"
            ) from exc

        return columns, rows

