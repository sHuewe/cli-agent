from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from cli_agent.config import McpServerConfig
from cli_agent.data_markdown import (
    MarkdownTableError,
    extract_markdown_tables as parse_markdown_tables,
)
from cli_agent.data_operations import (
    DataOperationError,
    DataOperations,
)
from cli_agent.os_operations import Workspace, WorkspaceError


def _operations(tmp_path: Path, *, protected_paths=()) -> DataOperations:
    workspace = Workspace.from_directory(
        tmp_path,
        McpServerConfig(
            name="data",
            config={"allow_write_files": True},
        ),
        protected_paths=tuple(protected_paths),
    )
    return DataOperations(workspace)


def _markdown_rows(markdown: str):
    operations = DataOperations(None)
    _columns, rows = operations._read_records(
        data=markdown,
        data_format="markdown",
    )
    return rows


def _markdown_table(markdown: str):
    tables = parse_markdown_tables(markdown)
    assert len(tables) == 1
    return tables[0]


def _write_csv(tmp_path: Path) -> Path:
    path = tmp_path / "sales.csv"
    path.write_text(
        "country,category,revenue,active\n"
        "DE,A,10.5,true\n"
        "DE,B,20,false\n"
        "FR,A,7.5,true\n"
        "DE,A,12,true\n",
        encoding="utf-8",
    )
    return path


def test_inspect_data_returns_schema_counts_and_bounded_sample(tmp_path: Path) -> None:
    _write_csv(tmp_path)

    result = json.loads(_operations(tmp_path).inspect_data("sales.csv", sample_rows=2))

    assert result["row_count"] == 4
    assert result["sample"] == [
        {"country": "DE", "category": "A", "revenue": 10.5, "active": True},
        {"country": "DE", "category": "B", "revenue": 20, "active": False},
    ]
    metadata = {entry["name"]: entry for entry in result["columns"]}
    assert metadata["country"]["dtype"] == "string"
    assert metadata["revenue"]["dtype"] == "float"
    assert metadata["active"]["dtype"] == "bool"
    assert metadata["category"]["unique_count"] == 2


def test_select_data_filters_projects_sorts_and_reports_truncation(
    tmp_path: Path,
) -> None:
    _write_csv(tmp_path)

    result = _operations(tmp_path).select_data(
        "sales.csv",
        columns=["country", "revenue"],
        filters=[
            {"column": "country", "op": "eq", "value": "DE"},
            {"column": "revenue", "op": "gt", "value": 10},
        ],
        sort_by=["revenue"],
        descending=True,
        limit=2,
    )

    assert result.startswith("Rows: 2/3 (truncated)")
    assert _markdown_rows(result) == [
        {"country": "DE", "revenue": 20},
        {"country": "DE", "revenue": 12},
    ]


def test_value_counts_supports_filters(tmp_path: Path) -> None:
    _write_csv(tmp_path)

    result = _operations(tmp_path).value_counts(
        "category",
        "sales.csv",
        filters=[{"column": "country", "op": "eq", "value": "DE"}],
    )

    assert _markdown_rows(result) == [
        {"value": "A", "count": 2},
        {"value": "B", "count": 1},
    ]


def test_aggregate_data_groups_and_computes_numeric_statistics(
    tmp_path: Path,
) -> None:
    _write_csv(tmp_path)

    result = _operations(tmp_path).aggregate_data(
        [
            {
                "column": "revenue",
                "function": "sum",
                "alias": "revenue_total",
            },
            {
                "column": "revenue",
                "function": "mean",
                "alias": "revenue_mean",
            },
            {
                "column": "category",
                "function": "count",
                "alias": "rows",
            },
        ],
        "sales.csv",
        group_by=["country"],
        sort_by=["country"],
    )

    rows = _markdown_rows(result)
    assert rows[0]["country"] == "DE"
    assert rows[0]["revenue_total"] == 42.5
    assert rows[0]["revenue_mean"] == pytest.approx(42.5 / 3)
    assert rows[0]["rows"] == 3
    assert rows[1] == {
        "country": "FR",
        "revenue_total": 7.5,
        "revenue_mean": 7.5,
        "rows": 1,
    }


def test_jsonl_is_normalized_to_union_of_columns(tmp_path: Path) -> None:
    (tmp_path / "events.jsonl").write_text(
        '{"kind":"a","value":1}\n'
        '{"kind":"b","extra":true}\n',
        encoding="utf-8",
    )

    result = _operations(tmp_path).select_data("events.jsonl")

    assert _markdown_rows(result) == [
        {"kind": "a", "value": 1, "extra": None},
        {"kind": "b", "value": None, "extra": True},
    ]


def test_select_data_to_file_uses_same_workspace_security_and_writes_csv(
    tmp_path: Path,
) -> None:
    _write_csv(tmp_path)
    operations = _operations(tmp_path)

    status = json.loads(
        operations.select_data_to_file(
            "derived.csv",
            "sales.csv",
            columns=["country", "revenue"],
            filters=[{"column": "country", "op": "eq", "value": "FR"}],
        )
    )

    assert status["output_path"] == "derived.csv"
    assert status["rows_written"] == 1
    assert (tmp_path / "derived.csv").read_text(encoding="utf-8") == (
        "country,revenue\nFR,7.5\n"
    )


def test_aggregate_data_to_file_supports_jsonl_output(tmp_path: Path) -> None:
    _write_csv(tmp_path)
    operations = _operations(tmp_path)

    status = json.loads(
        operations.aggregate_data_to_file(
            "summary.jsonl",
            [{"column": "revenue", "function": "sum", "alias": "total"}],
            "sales.csv",
            group_by=["country"],
            sort_by=["country"],
        )
    )

    assert status["rows_written"] == 2
    lines = [
        json.loads(line)
        for line in (tmp_path / "summary.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert lines == [
        {"country": "DE", "total": 42.5},
        {"country": "FR", "total": 7.5},
    ]


@pytest.mark.parametrize("path", ["../outside.csv", "/outside.csv"])
def test_data_reads_cannot_escape_workspace(tmp_path: Path, path: str) -> None:
    with pytest.raises(WorkspaceError):
        _operations(tmp_path).inspect_data(path)


def test_data_read_rejects_protected_path(tmp_path: Path) -> None:
    source = _write_csv(tmp_path)

    with pytest.raises(WorkspaceError, match="geschützten"):
        _operations(
            tmp_path,
            protected_paths=(source,),
        ).inspect_data("sales.csv")


def test_data_write_rejects_protected_path(tmp_path: Path) -> None:
    _write_csv(tmp_path)
    protected = tmp_path / "derived.csv"
    operations = _operations(
        tmp_path,
        protected_paths=(protected,),
    )

    with pytest.raises(WorkspaceError, match="geschützten"):
        operations.select_data_to_file(
            "derived.csv",
            "sales.csv",
        )
    assert not protected.exists()


def test_data_operations_reject_unsupported_formats(tmp_path: Path) -> None:
    (tmp_path / "data.txt").write_text("a,b\n1,2\n", encoding="utf-8")

    with pytest.raises(DataOperationError, match="Datenformat"):
        _operations(tmp_path).inspect_data("data.txt")


def test_filters_reject_unknown_operator_without_evaluation(tmp_path: Path) -> None:
    _write_csv(tmp_path)

    with pytest.raises(DataOperationError, match="Filteroperator"):
        _operations(tmp_path).select_data(
            "sales.csv",
            filters=[
                {
                    "column": "revenue",
                    "op": "eval",
                    "value": "__import__('os')",
                }
            ],
        )


def test_aggregation_rejects_non_numeric_sum(tmp_path: Path) -> None:
    _write_csv(tmp_path)

    with pytest.raises(DataOperationError, match="numerische"):
        _operations(tmp_path).aggregate_data(
            [{"column": "country", "function": "sum"}],
            "sales.csv",
        )


def test_limits_are_validated(tmp_path: Path) -> None:
    _write_csv(tmp_path)
    operations = _operations(tmp_path)

    with pytest.raises(DataOperationError, match="sample_rows"):
        operations.inspect_data("sales.csv", sample_rows=0)
    with pytest.raises(DataOperationError, match="limit"):
        operations.select_data("sales.csv", limit=1001)



def test_csv_missing_trailing_field_is_null(tmp_path: Path) -> None:
    (tmp_path / "incomplete.csv").write_text(
        "a,b\n1\n",
        encoding="utf-8",
    )

    result = _operations(tmp_path).select_data("incomplete.csv")

    assert _markdown_rows(result) == [{"a": 1, "b": None}]



def test_inline_csv_works_without_workspace() -> None:
    operations = DataOperations(None)

    result = operations.aggregate_data(
        [{"column": "revenue", "function": "sum", "alias": "total"}],
        data="country,revenue\nDE,10\nFR,7.5\n",
        data_format="csv",
    )

    assert _markdown_rows(result) == [{"total": 17.5}]


def test_inline_json_array_works_without_workspace() -> None:
    operations = DataOperations(None)

    result = operations.select_data(
        data='[{"name":"A","value":2},{"name":"B","value":5}]',
        data_format="json",
        filters=[{"column": "value", "op": "gt", "value": 2}],
    )

    assert _markdown_rows(result) == [{"name": "B", "value": 5}]


def test_path_is_rejected_without_workspace_access() -> None:
    operations = DataOperations(None)

    with pytest.raises(DataOperationError, match="Dateizugriff"):
        operations.inspect_data("sales.csv")


def test_exactly_one_source_is_required() -> None:
    operations = DataOperations(None)

    with pytest.raises(DataOperationError, match="Genau eine Datenquelle"):
        operations.inspect_data()
    with pytest.raises(DataOperationError, match="Genau eine Datenquelle"):
        operations.inspect_data(
            "sales.csv",
            data="a,b\n1,2\n",
            data_format="csv",
        )


def test_inline_data_requires_explicit_format() -> None:
    operations = DataOperations(None)

    with pytest.raises(DataOperationError, match="data_format"):
        operations.inspect_data(data="a,b\n1,2\n")


def test_data_format_is_rejected_for_path_source(tmp_path: Path) -> None:
    _write_csv(tmp_path)

    with pytest.raises(DataOperationError, match="data_format"):
        _operations(tmp_path).inspect_data(
            "sales.csv",
            data_format="csv",
        )


def test_inline_json_rejects_nested_values() -> None:
    operations = DataOperations(None)

    with pytest.raises(DataOperationError, match="skalare"):
        operations.inspect_data(
            data='[{"name":"A","nested":{"x":1}}]',
            data_format="json",
        )



@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("0.1 + 0.2", "0.3"),
        ("(417.3 - 382.1) / 382.1 * 100", "9.2122481025909447788537032190526040303585448835383"),
        ("2 ** 10", "1024"),
        ("sqrt(2)", "1.4142135623730950488016887242096980785696718753769"),
        ("round(10 / 3, 4)", "3.3333"),
        ("min(7, 2, 9) + max(1, 4)", "6"),
        ("abs(-12.5)", "12.5"),
        ("17 % 5", "2"),
    ],
)
def test_calculate_uses_decimal_arithmetic(
    expression: str,
    expected: str,
) -> None:
    result = json.loads(DataOperations(None).calculate(expression))

    assert result["expression"] == expression
    assert result["result"] == expected


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo unsafe')",
        "open('x')",
        "(1).__class__",
        "[1, 2, 3]",
        "lambda: 1",
        "sum([1, 2])",
        "True + 1",
    ],
)
def test_calculate_rejects_code_and_unsupported_syntax(expression: str) -> None:
    with pytest.raises(DataOperationError):
        DataOperations(None).calculate(expression)


def test_calculate_rejects_division_by_zero() -> None:
    with pytest.raises(DataOperationError, match="nicht definiert"):
        DataOperations(None).calculate("1 / 0")


def test_calculate_rejects_fractional_power_exponent() -> None:
    with pytest.raises(DataOperationError, match="ganze Zahl"):
        DataOperations(None).calculate("2 ** 0.5")


def test_calculate_rejects_excessive_power() -> None:
    with pytest.raises(DataOperationError, match="Exponent"):
        DataOperations(None).calculate("2 ** 1001")


def test_calculate_rejects_oversized_expression() -> None:
    with pytest.raises(DataOperationError, match="512"):
        DataOperations(None).calculate("1+" * 300 + "1")



def test_extract_markdown_tables_returns_normalized_markdown() -> None:
    markdown = """Intro

Spielbericht
| | Heim | Gast | Sätze | Spiele |
|---|---|---|---|---|
| D1-D1 | A / B | C / D | 3:1 | 1:0 |
| 1-2 | A | D | 2:3 | 0:1 |

Danach
"""
    result = DataOperations(None).extract_markdown_tables(markdown)

    assert result.startswith("## Table 0\n\nContext: Spielbericht")
    table = _markdown_table(result)
    assert table["columns"] == ["column_1", "Heim", "Gast", "Sätze", "Spiele"]
    assert table["row_count"] == 2
    assert table["rows"][0] == {
        "column_1": "D1-D1",
        "Heim": "A / B",
        "Gast": "C / D",
        "Sätze": "3:1",
        "Spiele": "1:0",
    }


def test_extract_markdown_tables_handles_multiple_and_duplicate_headers() -> None:
    markdown = """Erste
| Name | Name |
|---|---|
| A | B |

Zweite
| x | y |
|---|---|
| 1 | 2 |
"""
    result = DataOperations(None).extract_markdown_tables(markdown)
    tables = parse_markdown_tables(result)

    assert len(tables) == 2
    assert tables[0]["columns"] == ["Name", "Name_2"]
    assert tables[1]["columns"] == ["x", "y"]


def test_normalized_markdown_can_be_passed_to_existing_data_tools() -> None:
    markdown = """Spielbericht
| | Heim | Gast | Sätze | Spiele |
|---|---|---|---|---|
| D1-D1 | A / B | C / D | 3:1 | 1:0 |
| 1-2 | A | D | 2:3 | 0:1 |
"""
    operations = DataOperations(None)
    normalized = operations.extract_markdown_tables(markdown)

    inspected = json.loads(
        operations.inspect_data(
            data=normalized,
            data_format="markdown",
        )
    )
    selected = operations.select_data(
        data=normalized,
        data_format="markdown",
        filters=[{"column": "Spiele", "op": "eq", "value": "1:0"}],
    )

    assert inspected["source"] == "inline:markdown"
    assert inspected["row_count"] == 2
    assert _markdown_rows(selected)[0]["column_1"] == "D1-D1"


def test_markdown_table_index_selects_one_of_multiple_tables() -> None:
    markdown = """One
| a |
|---|
| 1 |

Two
| b |
|---|
| 2 |
"""
    result = json.loads(
        DataOperations(None).inspect_data(
            data=markdown,
            data_format="markdown",
            table_index=1,
        )
    )

    assert result["row_count"] == 1
    assert result["columns"][0]["name"] == "b"


def test_extract_markdown_tables_ignores_non_tables() -> None:
    result = DataOperations(None).extract_markdown_tables(
        "Nur Text\n\n| keine | Tabelle |\n"
    )

    assert result == "No Markdown tables found."


def test_extract_markdown_tables_normalizes_literal_json_newlines() -> None:
    markdown = (
        "| A | B |\\n"
        "|---|---|\\n"
        "| 1 | 2 |"
    )

    result = DataOperations(None).extract_markdown_tables(markdown)

    assert _markdown_rows(result) == [{"A": 1, "B": 2}]


def test_extract_markdown_tables_normalizes_mixed_real_and_literal_newlines() -> None:
    markdown = (
        "| A | B |\n"
        "|---|---|\\n"
        "| 1 | 2 |\\n"
        "| 3 | 4 |"
    )

    result = DataOperations(None).extract_markdown_tables(markdown)

    assert _markdown_rows(result) == [
        {"A": 1, "B": 2},
        {"A": 3, "B": 4},
    ]


def test_extract_markdown_tables_normalizes_mixed_literal_crlf() -> None:
    markdown = (
        "| A | B |\n"
        "|---|---|\\r\\n"
        "| 1 | 2 |"
    )

    result = DataOperations(None).extract_markdown_tables(markdown)

    assert _markdown_rows(result) == [{"A": 1, "B": 2}]


def test_extract_markdown_tables_keeps_literal_newline_inside_cell() -> None:
    markdown = (
        "| A | B |\n"
        "|---|---|\n"
        r"| 1 | literal\nvalue |"
    )

    result = DataOperations(None).extract_markdown_tables(markdown)
    rows = _markdown_rows(result)

    assert rows[0]["B"] == r"literal\nvalue"


def test_extract_markdown_tables_repairs_separator_width_only() -> None:
    markdown = (
        "| A | B | C |\n"
        "|---|---|\n"
        "| 1 | 2 | 3 |"
    )

    result = DataOperations(None).extract_markdown_tables(markdown)
    table = _markdown_table(result)

    assert table["columns"] == ["A", "B", "C"]
    assert table["row_count"] == 1
    assert _markdown_rows(result) == [{"A": 1, "B": 2, "C": 3}]


def test_extract_markdown_tables_preserves_other_backslash_escapes() -> None:
    markdown = (
        "| Path | Value |\\n"
        "|---|---|\\n"
        r"| C:\\temp | literal\\tvalue |"
    )

    result = DataOperations(None).extract_markdown_tables(markdown)
    rows = _markdown_rows(result)

    assert rows[0]["Path"] == r"C:\temp"
    assert rows[0]["Value"] == r"literal\tvalue"


def test_select_data_result_is_normalized_markdown() -> None:
    result = DataOperations(None).select_data(
        data="| a | b |\n|---|---|\n| 1 | x |\n| 2 | y |",
        data_format="markdown",
        columns=["b", "a"],
    )

    assert result.startswith("Rows: 2/2")
    table = _markdown_table(result)
    assert table["columns"] == ["b", "a"]
    assert len(table["source_headers"]) == len(table["columns"])


def test_markdown_file_can_be_written_and_reused(tmp_path: Path) -> None:
    operations = _operations(tmp_path)
    status = json.loads(
        operations.select_data_to_file(
            "derived.md",
            data="| a | b |\n|---|---|\n| 1 | 2 |",
            data_format="markdown",
        )
    )

    assert status["output_path"] == "derived.md"
    result = json.loads(operations.inspect_data("derived.md"))
    assert result["row_count"] == 1
    assert result["sample"] == [{"a": 1, "b": 2}]



def test_json_string_scalars_survive_markdown_chaining() -> None:
    operations = DataOperations(None)
    selected = operations.select_data(
        data=json.dumps(
            [
                {"value": "001"},
                {"value": "true"},
                {"value": "1e3"},
                {"value": ""},
                {"value": "  spaced  "},
                {"value": None},
            ]
        ),
        data_format="json",
    )

    chained = json.loads(
        operations.inspect_data(
            data=selected,
            data_format="markdown",
            sample_rows=6,
        )
    )

    assert [row["value"] for row in chained["sample"]] == [
        "001",
        "true",
        "1e3",
        "",
        "  spaced  ",
        None,
    ]
    assert chained["columns"][0]["dtype"] == "string"


def test_normalizing_generated_markdown_keeps_scalar_overrides() -> None:
    operations = DataOperations(None)
    selected = operations.select_data(
        data='[{"value":"001"},{"value":""},{"value":null}]',
        data_format="json",
    )

    normalized = operations.extract_markdown_tables(selected)
    chained = json.loads(
        operations.inspect_data(
            data=normalized,
            data_format="markdown",
            sample_rows=3,
        )
    )

    assert [row["value"] for row in chained["sample"]] == [
        "001",
        "",
        None,
    ]


def test_large_integer_sum_stays_exact() -> None:
    operations = DataOperations(None)
    result = operations.aggregate_data(
        [{"column": "value", "function": "sum", "alias": "total"}],
        data=(
            '[{"value":9007199254740992},'
            '{"value":1}]'
        ),
        data_format="json",
    )

    assert _markdown_rows(result) == [
        {"total": 9007199254740993}
    ]


def test_large_integer_statistics_use_decimal_precision() -> None:
    operations = DataOperations(None)
    result = operations.aggregate_data(
        [
            {"column": "value", "function": "mean", "alias": "mean"},
            {"column": "value", "function": "median", "alias": "median"},
            {"column": "value", "function": "std", "alias": "std"},
        ],
        data=(
            '[{"value":9007199254740992},'
            '{"value":9007199254740993}]'
        ),
        data_format="json",
    )

    row = _markdown_rows(result)[0]
    assert row["mean"] == Decimal("9007199254740992.5")
    assert row["median"] == Decimal("9007199254740992.5")
    assert isinstance(row["std"], Decimal)
    assert str(row["std"]).startswith("0.7071067811865475244")


def test_decimal_aggregate_jsonl_write_preserves_numeric_text(
    tmp_path: Path,
) -> None:
    operations = _operations(tmp_path)
    operations.aggregate_data_to_file(
        "summary.jsonl",
        [{"column": "value", "function": "mean", "alias": "mean"}],
        data=(
            '[{"value":9007199254740992},'
            '{"value":9007199254740993}]'
        ),
        data_format="json",
    )

    assert (
        tmp_path / "summary.jsonl"
    ).read_text(encoding="utf-8").strip() == (
        '{"mean":9007199254740992.5}'
    )



def test_untrusted_legacy_scalar_override_comment_cannot_change_visible_value() -> None:
    operations = DataOperations(None)
    markdown = """<!-- cli-agent:data-overrides:v1:W1swLDAsInMiLCJhdHRhY2tlciJdXQ -->
| value |
|---|
| 5 |
"""

    result = json.loads(
        operations.inspect_data(
            data=markdown,
            data_format="markdown",
        )
    )

    assert result["sample"] == [{"value": 5}]
    assert result["columns"][0]["dtype"] == "int"


def test_lossless_string_chaining_uses_visible_typed_cells() -> None:
    operations = DataOperations(None)
    result = operations.select_data(
        data='[{"value":"001"},{"value":"true"},{"value":""}]',
        data_format="json",
    )

    assert '`string:"001"`' in result
    assert '`string:"true"`' in result
    assert '`string:""`' in result

    chained = json.loads(
        operations.inspect_data(
            data=result,
            data_format="markdown",
            sample_rows=3,
        )
    )
    assert [row["value"] for row in chained["sample"]] == [
        "001",
        "true",
        "",
    ]


def test_descending_sort_keeps_nulls_last() -> None:
    operations = DataOperations(None)
    result = operations.select_data(
        data='[{"value":null},{"value":2},{"value":5},{"value":null}]',
        data_format="json",
        sort_by=["value"],
        descending=True,
    )

    assert _markdown_rows(result) == [
        {"value": 5},
        {"value": 2},
        {"value": None},
        {"value": None},
    ]


def test_bool_and_numbers_have_distinct_scalar_identity() -> None:
    operations = DataOperations(None)
    data = (
        '[{"id":"t","value":true},'
        '{"id":"one","value":1},'
        '{"id":"f","value":false},'
        '{"id":"zero","value":0}]'
    )

    equal = operations.select_data(
        data=data,
        data_format="json",
        columns=["id"],
        filters=[{"column": "value", "op": "eq", "value": 1}],
    )
    member = operations.select_data(
        data=data,
        data_format="json",
        columns=["id"],
        filters=[{"column": "value", "op": "in", "value": [1]}],
    )
    inspected = json.loads(
        operations.inspect_data(
            data=data,
            data_format="json",
            sample_rows=4,
        )
    )
    counts = operations.value_counts(
        "value",
        data=data,
        data_format="json",
    )
    grouped = operations.aggregate_data(
        [{"column": "id", "function": "count", "alias": "count"}],
        data=data,
        data_format="json",
        group_by=["value"],
    )
    unique = operations.aggregate_data(
        [{"column": "value", "function": "nunique", "alias": "unique"}],
        data=data,
        data_format="json",
    )

    assert _markdown_rows(equal) == [{"id": "one"}]
    assert _markdown_rows(member) == [{"id": "one"}]
    value_metadata = next(
        column
        for column in inspected["columns"]
        if column["name"] == "value"
    )
    assert value_metadata["unique_count"] == 4
    assert _markdown_rows(counts) == [
        {"value": True, "count": 1},
        {"value": 1, "count": 1},
        {"value": False, "count": 1},
        {"value": 0, "count": 1},
    ]
    assert len(_markdown_rows(grouped)) == 4
    assert _markdown_rows(unique) == [{"unique": 4}]


def test_float_aggregation_rejects_non_finite_result() -> None:
    operations = DataOperations(None)

    with pytest.raises(DataOperationError, match="endliches Ergebnis"):
        operations.aggregate_data(
            [{"column": "value", "function": "sum", "alias": "total"}],
            data="value\n1e308\n1e308\n",
            data_format="csv",
        )


def test_failed_non_finite_aggregation_does_not_truncate_output(
    tmp_path: Path,
) -> None:
    operations = _operations(tmp_path)
    output = tmp_path / "summary.jsonl"
    output.write_text("keep\n", encoding="utf-8")

    with pytest.raises(DataOperationError, match="endliches Ergebnis"):
        operations.aggregate_data_to_file(
            "summary.jsonl",
            [{"column": "value", "function": "sum", "alias": "total"}],
            data="value\n1e308\n1e308\n",
            data_format="csv",
        )

    assert output.read_text(encoding="utf-8") == "keep\n"



def test_inline_json_records_remain_sparse() -> None:
    operations = DataOperations(None)
    columns, rows = operations._read_records(
        data='[{"a":1},{"b":2},{"c":3}]',
        data_format="json",
    )

    assert columns == ["a", "b", "c"]
    assert rows == [{"a": 1}, {"b": 2}, {"c": 3}]

    selected = operations.select_data(
        data='[{"a":1},{"b":2},{"c":3}]',
        data_format="json",
    )
    assert _markdown_rows(selected) == [
        {"a": 1, "b": None, "c": None},
        {"a": None, "b": 2, "c": None},
        {"a": None, "b": None, "c": 3},
    ]


def test_jsonl_file_records_remain_sparse(tmp_path: Path) -> None:
    path = tmp_path / "sparse.jsonl"
    path.write_text(
        '{"a":1}\n{"b":2}\n{"c":3}\n',
        encoding="utf-8",
    )
    operations = _operations(tmp_path)

    columns, rows = operations._read_records("sparse.jsonl")

    assert columns == ["a", "b", "c"]
    assert rows == [{"a": 1}, {"b": 2}, {"c": 3}]

    inspected = json.loads(operations.inspect_data("sparse.jsonl"))
    metadata = {
        column["name"]: column
        for column in inspected["columns"]
    }
    assert metadata["a"]["null_count"] == 2
    assert metadata["a"]["unique_count"] == 2


def test_sparse_jsonl_to_jsonl_keeps_missing_fields_absent(
    tmp_path: Path,
) -> None:
    operations = _operations(tmp_path)
    operations.select_data_to_file(
        "sparse.jsonl",
        data='[{"a":1},{"b":2}]',
        data_format="json",
    )

    assert (
        tmp_path / "sparse.jsonl"
    ).read_text(encoding="utf-8").splitlines() == [
        '{"a":1}',
        '{"b":2}',
    ]


def test_decimal_aggregation_rejects_excessive_working_precision() -> None:
    operations = DataOperations(None)
    data = (
        "| value |\n"
        "|---|\n"
        "| `decimal:1e1000000000` |\n"
        "| `decimal:1` |"
    )

    with pytest.raises(DataOperationError, match="Arbeitspräzision"):
        operations.aggregate_data(
            [
                {
                    "column": "value",
                    "function": "mean",
                    "alias": "mean",
                }
            ],
            data=data,
            data_format="markdown",
        )



@pytest.mark.parametrize("data_format,delimiter", [("csv", ","), ("tsv", "\t")])
def test_inline_delimited_accepts_field_above_python_csv_default(
    data_format: str,
    delimiter: str,
) -> None:
    long_value = "x" * 150_000
    columns, rows = DataOperations(None)._read_records(
        data=f"value{delimiter}other\n{long_value}{delimiter}ok\n",
        data_format=data_format,
    )

    assert columns == ["value", "other"]
    assert rows == [{"value": long_value, "other": "ok"}]


@pytest.mark.parametrize(
    "suffix,delimiter",
    [(".csv", ","), (".tsv", "\t")],
)
def test_delimited_file_accepts_field_above_python_csv_default(
    tmp_path: Path,
    suffix: str,
    delimiter: str,
) -> None:
    long_value = "x" * 150_000
    path = tmp_path / f"large-field{suffix}"
    path.write_text(
        f"value{delimiter}other\n{long_value}{delimiter}ok\n",
        encoding="utf-8",
    )

    columns, rows = _operations(tmp_path)._read_records(path.name)

    assert columns == ["value", "other"]
    assert rows == [{"value": long_value, "other": "ok"}]


def test_markdown_row_limit_is_checked_before_table_materialization() -> None:
    markdown = (
        "| value |\n"
        "|---|\n"
        "| 1 |\n"
        "| 2 |\n"
        "| 3 |\n"
    )

    with pytest.raises(MarkdownTableError, match="Zeilenlimit"):
        parse_markdown_tables(markdown, max_rows=2)


def test_markdown_row_budget_applies_across_all_extracted_tables() -> None:
    markdown = (
        "| first |\n"
        "|---|\n"
        "| 1 |\n"
        "| 2 |\n"
        "\n"
        "| second |\n"
        "|---|\n"
        "| 3 |\n"
    )

    with pytest.raises(MarkdownTableError, match="Zeilenlimit"):
        parse_markdown_tables(markdown, max_rows=2)



def test_adjacent_tables_do_not_reemit_previous_table_as_context() -> None:
    operations = DataOperations(None)
    source = (
        "| first |\n"
        "|---|\n"
        "| 1 |\n"
        "\n"
        "| second |\n"
        "|---|\n"
        "| 2 |\n"
    )

    normalized = operations.extract_markdown_tables(source)
    tables = parse_markdown_tables(normalized)

    assert len(tables) == 2
    assert tables[0]["columns"] == ["first"]
    assert tables[1]["columns"] == ["second"]

    second = json.loads(
        operations.inspect_data(
            data=normalized,
            data_format="markdown",
            table_index=1,
        )
    )
    assert second["sample"] == [{"second": 2}]


@pytest.mark.parametrize(
    ("data_format", "data"),
    [
        ("csv", " id ,value\n1,x\n"),
        ("json", '[{" id ":1}]'),
        ("jsonl", '{" id ":1}\n'),
    ],
)
def test_inline_sources_reject_column_names_with_surrounding_whitespace(
    data_format: str,
    data: str,
) -> None:
    with pytest.raises(DataOperationError, match="Spaltennamen.*Leerzeichen"):
        DataOperations(None).inspect_data(
            data=data,
            data_format=data_format,
        )


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("bad.csv", " id ,value\n1,x\n"),
        ("bad.jsonl", '{" id ":1}\n'),
    ],
)
def test_file_sources_reject_column_names_with_surrounding_whitespace(
    tmp_path: Path,
    filename: str,
    content: str,
) -> None:
    (tmp_path / filename).write_text(content, encoding="utf-8")

    with pytest.raises(DataOperationError, match="Spaltennamen.*Leerzeichen"):
        _operations(tmp_path).inspect_data(filename)


def test_aggregation_alias_rejects_surrounding_whitespace() -> None:
    with pytest.raises(DataOperationError, match="Spaltennamen.*Leerzeichen"):
        DataOperations(None).aggregate_data(
            [
                {
                    "column": "value",
                    "function": "sum",
                    "alias": " total ",
                }
            ],
            data='[{"value":1}]',
            data_format="json",
        )
