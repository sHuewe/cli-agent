"""Grouping, filtering, aggregation and bounded schema analysis."""
from __future__ import annotations
from cli_agent.data.data_core import *

class DataQueryMixin:
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
                    comparable_actual, comparable_expected = (
                        _filter_comparison_values(actual, expected)
                        if operator not in {"in", "not_in"} else (actual, expected)
                    )
                    if operator == "eq" and not _typed_equal(comparable_actual, comparable_expected):
                        return False
                    if operator == "ne" and _typed_equal(comparable_actual, comparable_expected):
                        return False
                    if operator == "lt" and not (
                        comparable_actual is not None and comparable_actual < comparable_expected
                    ):
                        return False
                    if operator == "lte" and not (
                        comparable_actual is not None and comparable_actual <= comparable_expected
                    ):
                        return False
                    if operator == "gt" and not (
                        comparable_actual is not None and comparable_actual > comparable_expected
                    ):
                        return False
                    if operator == "gte" and not (
                        comparable_actual is not None and comparable_actual >= comparable_expected
                    ):
                        return False
                    if operator == "in" and not any(
                        _typed_equal(*_filter_comparison_values(actual, candidate))
                        for candidate in expected
                    ):
                        return False
                    if operator == "not_in" and any(
                        _typed_equal(*_filter_comparison_values(actual, candidate))
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
        selected = DataQueryMixin._require_columns(columns, sort_by)
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
        result = prefix + "\n\n" + table
        if len(result.encode("utf-8")) > MAX_FULL_RESULT_BYTES:
            raise DataOperationError(
                "Vollständiges Datenergebnis ist zu groß für einen MCP-Aufruf. "
                "Bitte Zeitraum oder Spalten eingrenzen."
            )
        return result

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
        records_path: str | None = None,
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
            records_path=records_path,
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
        offset: int = 0,
        records_path: str | None = None,
    ) -> str:
        """Return schema metadata plus a bounded preview of complete rows."""
        sample_rows = self._validate_limit(
            sample_rows,
            maximum=MAX_SAMPLE_ROWS,
            name="sample_rows",
        )
        offset = self._validate_non_negative_limit(
            offset, maximum=MAX_DATA_ROWS, name="offset",
        )
        columns, rows = self._read_records(
            path,
            data=data,
            data_format=data_format,
            table_index=table_index,
            records_path=records_path,
        )
        metadata = self._column_metadata(columns, rows)
        sample = rows[offset:offset + sample_rows]
        next_offset = offset + len(sample)
        if next_offset >= len(rows):
            next_offset = None
        return _json_dumps_precise(
            {
                "source": (
                    path
                    if path is not None
                    else f"inline:{data_format}"
                ),
                "row_count": len(rows),
                "columns": metadata,
                "offset": offset,
                "sample": sample,
                "next_offset": next_offset,
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
        records_path: str | None = None,
    ) -> str:
        selected_columns, ordered = self._select_records(
            path, data=data, data_format=data_format,
            table_index=table_index, columns=columns, filters=filters,
            sort_by=sort_by, descending=descending, records_path=records_path,
        )
        return self._result(
            selected_columns, ordered, total_rows=len(ordered), limit=len(ordered),
        )

    def _select_records(
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
        records_path: str | None = None,
    ) -> tuple[list[str], list[Record]]:
        source_columns, rows = self._read_records(
            path,
            data=data,
            data_format=data_format,
            table_index=table_index,
            records_path=records_path,
        )
        selected_columns = self._require_columns(source_columns, columns)
        filtered = self._apply_filters(rows, source_columns, filters)
        ordered = self._sort_rows(
            filtered,
            source_columns,
            sort_by,
            descending=descending,
        )
        return selected_columns, ordered

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
        records_path: str | None = None,
    ) -> tuple[list[str], list[Record]]:
        columns, rows = self._read_records(
            path,
            data=data,
            data_format=data_format,
            table_index=table_index,
            records_path=records_path,
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
        records_path: str | None = None,
    ) -> str:
        result_columns, rows = self._aggregate_records(
            path,
            data=data,
            data_format=data_format,
            table_index=table_index,
            records_path=records_path,
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
            limit=len(rows),
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
        records_path: str | None = None,
    ) -> str:
        result_rows, total = self._value_counts_records(
            column, path, data=data, data_format=data_format,
            table_index=table_index, filters=filters, limit=limit,
            records_path=records_path,
        )
        return self._result(
            ["value", "count"], result_rows,
            total_rows=total, limit=len(result_rows),
        )

    def _value_counts_records(
        self,
        column: str,
        path: str | None = None,
        *,
        data: str | None = None,
        data_format: str | None = None,
        table_index: int = 0,
        filters: list[dict[str, Any]] | None = None,
        limit: int = 50,
        records_path: str | None = None,
    ) -> tuple[list[Record], int]:
        limit = self._validate_limit(
            limit,
            maximum=MAX_VALUE_COUNT_ROWS,
        )
        columns, rows = self._read_records(
            path,
            data=data,
            data_format=data_format,
            table_index=table_index,
            records_path=records_path,
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
        return result_rows[:limit], len(result_rows)
