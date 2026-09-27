from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_agent.config import McpServerConfig
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

    result = json.loads(
        _operations(tmp_path).select_data(
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
    )

    assert result["total_rows"] == 3
    assert result["returned_rows"] == 2
    assert result["truncated"] is True
    assert result["rows"] == [
        {"country": "DE", "revenue": 20},
        {"country": "DE", "revenue": 12},
    ]


def test_value_counts_supports_filters(tmp_path: Path) -> None:
    _write_csv(tmp_path)

    result = json.loads(
        _operations(tmp_path).value_counts(
            "category",
            "sales.csv",
            filters=[{"column": "country", "op": "eq", "value": "DE"}],
        )
    )

    assert result["rows"] == [
        {"value": "A", "count": 2},
        {"value": "B", "count": 1},
    ]


def test_aggregate_data_groups_and_computes_numeric_statistics(
    tmp_path: Path,
) -> None:
    _write_csv(tmp_path)

    result = json.loads(
        _operations(tmp_path).aggregate_data(
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
    )

    assert result["rows"] == [
        {
            "country": "DE",
            "revenue_total": 42.5,
            "revenue_mean": pytest.approx(42.5 / 3),
            "rows": 3,
        },
        {
            "country": "FR",
            "revenue_total": 7.5,
            "revenue_mean": 7.5,
            "rows": 1,
        },
    ]


def test_jsonl_is_normalized_to_union_of_columns(tmp_path: Path) -> None:
    (tmp_path / "events.jsonl").write_text(
        '{"kind":"a","value":1}\n'
        '{"kind":"b","extra":true}\n',
        encoding="utf-8",
    )

    result = json.loads(_operations(tmp_path).select_data("events.jsonl"))

    assert result["rows"] == [
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

    result = json.loads(
        _operations(tmp_path).select_data("incomplete.csv")
    )

    assert result["rows"] == [{"a": 1, "b": None}]



def test_inline_csv_works_without_workspace() -> None:
    operations = DataOperations(None)

    result = json.loads(
        operations.aggregate_data(
            [{"column": "revenue", "function": "sum", "alias": "total"}],
            data="country,revenue\nDE,10\nFR,7.5\n",
            data_format="csv",
        )
    )

    assert result["rows"] == [{"total": 17.5}]


def test_inline_json_array_works_without_workspace() -> None:
    operations = DataOperations(None)

    result = json.loads(
        operations.select_data(
            data='[{"name":"A","value":2},{"name":"B","value":5}]',
            data_format="json",
            filters=[{"column": "value", "op": "gt", "value": 2}],
        )
    )

    assert result["rows"] == [{"name": "B", "value": 5}]


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



def test_extract_markdown_tables_returns_canonical_table_objects() -> None:
    markdown = """Intro

Spielbericht
| | Heim | Gast | Sätze | Spiele |
|---|---|---|---|---|
| D1-D1 | A / B | C / D | 3:1 | 1:0 |
| 1-2 | A | D | 2:3 | 0:1 |

Danach
"""
    operations = DataOperations(None)

    result = json.loads(operations.extract_markdown_tables(markdown))

    assert result["table_count"] == 1
    table = result["tables"][0]
    assert table["index"] == 0
    assert table["context"] == "Spielbericht"
    assert table["source_headers"] == ["", "Heim", "Gast", "Sätze", "Spiele"]
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
    result = json.loads(DataOperations(None).extract_markdown_tables(markdown))

    assert result["table_count"] == 2
    assert result["tables"][0]["columns"] == ["Name", "Name_2"]
    assert result["tables"][1]["context"] == "Zweite"


def test_extracted_table_can_be_passed_to_existing_data_tools() -> None:
    markdown = """Spielbericht
| | Heim | Gast | Sätze | Spiele |
|---|---|---|---|---|
| D1-D1 | A / B | C / D | 3:1 | 1:0 |
| 1-2 | A | D | 2:3 | 0:1 |
"""
    operations = DataOperations(None)
    extracted = json.loads(operations.extract_markdown_tables(markdown))
    table = extracted["tables"][0]

    inspected = json.loads(operations.inspect_data(table=table))
    selected = json.loads(
        operations.select_data(
            table=table,
            filters=[{"column": "Spiele", "op": "eq", "value": "1:0"}],
        )
    )

    assert inspected["source"] == "table"
    assert inspected["row_count"] == 2
    assert selected["total_rows"] == 1
    assert selected["rows"][0]["column_1"] == "D1-D1"


def test_table_source_is_mutually_exclusive_with_other_sources() -> None:
    table = {"columns": ["a"], "rows": [{"a": 1}]}
    operations = DataOperations(None)

    with pytest.raises(DataOperationError, match="Genau eine Datenquelle"):
        operations.inspect_data(data="a\n1\n", data_format="csv", table=table)
    with pytest.raises(DataOperationError, match="Genau eine Datenquelle"):
        operations.inspect_data()


def test_table_source_rejects_unknown_row_columns() -> None:
    operations = DataOperations(None)

    with pytest.raises(DataOperationError, match="unbekannte Spalten"):
        operations.inspect_data(
            table={
                "columns": ["a"],
                "rows": [{"a": 1, "b": 2}],
            }
        )


def test_extract_markdown_tables_ignores_non_tables() -> None:
    result = json.loads(
        DataOperations(None).extract_markdown_tables(
            "Nur Text\n\n| keine | Tabelle |\n"
        )
    )

    assert result == {"table_count": 0, "tables": []}
