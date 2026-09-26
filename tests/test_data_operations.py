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

    result = json.loads(_operations(tmp_path).inspect_data("sales.csv", 2))

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
            "sales.csv",
            "category",
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
            "sales.csv",
            group_by=["country"],
            aggregations=[
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
            "sales.csv",
            "derived.csv",
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
            "sales.csv",
            "summary.jsonl",
            group_by=["country"],
            aggregations=[
                {"column": "revenue", "function": "sum", "alias": "total"}
            ],
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
            "sales.csv",
            "derived.csv",
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
            "sales.csv",
            group_by=None,
            aggregations=[
                {"column": "country", "function": "sum"}
            ],
        )


def test_limits_are_validated(tmp_path: Path) -> None:
    _write_csv(tmp_path)
    operations = _operations(tmp_path)

    with pytest.raises(DataOperationError, match="sample_rows"):
        operations.inspect_data("sales.csv", 0)
    with pytest.raises(DataOperationError, match="limit"):
        operations.select_data("sales.csv", limit=1001)
