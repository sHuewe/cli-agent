"""Cross-format invariants for deterministic Data MCP filtering and aggregation."""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from cli_agent.data.data_operations import DataOperations


@pytest.mark.parametrize("count", [0, 1, 2, 7, 29])
def test_json_jsonl_and_csv_have_identical_grouped_numeric_results(count):
    rows = [
        {"group": "A" if i % 2 == 0 else "B", "value": i * 3 - 17, "active": i % 3 == 0}
        for i in range(count)
    ]
    formats = {
        "json": json.dumps(rows),
        "jsonl": "\n".join(json.dumps(row) for row in rows),
        "csv": "group,value,active\n" + "".join(
            f"{row['group']},{row['value']},{str(row['active']).lower()}\n"
            for row in rows
        ),
    }
    ops = DataOperations(None)
    results = []
    for fmt, payload in formats.items():
        columns, grouped = ops._aggregate_records(
            None, data=payload, data_format=fmt, table_index=0,
            group_by=["group"],
            aggregations=[
                {"column": "value", "function": "sum", "alias": "total"},
                {"column": "value", "function": "count", "alias": "observations"},
            ],
            filters=None, sort_by=["group"], descending=False,
        )
        assert columns == ["group", "total", "observations"]
        results.append(grouped)
    assert results[0] == results[1] == results[2]
    assert sum(row["total"] for row in results[0]) == sum(row["value"] for row in rows)
    assert sum(row["observations"] for row in results[0]) == count


@pytest.mark.parametrize("count", [1, 3, 8, 37])
def test_filtered_aggregation_equals_aggregate_over_selected_subset(count):
    data = json.dumps([
        {"group": "A" if i % 3 else "B", "value": i - 10, "keep": i % 2 == 0}
        for i in range(count)
    ])
    ops = DataOperations(None)
    common = dict(data=data, data_format="json", table_index=0)
    condition = [{"column": "keep", "op": "eq", "value": True}]
    _, direct = ops._aggregate_records(
        None, **common, group_by=["group"],
        aggregations=[{"column": "value", "function": "sum", "alias": "total"}],
        filters=condition, sort_by=["group"], descending=False,
    )
    cols, selected = ops._select_records(None, **common, filters=condition)
    assert cols == ["group", "value", "keep"]
    _, chained = ops._aggregate_records(
        None, data=json.dumps(selected), data_format="json", table_index=0,
        group_by=["group"],
        aggregations=[{"column": "value", "function": "sum", "alias": "total"}],
        filters=None, sort_by=["group"], descending=False,
    )
    assert direct == chained


@pytest.mark.parametrize("count", [2, 5, 31])
def test_numeric_nulls_are_ignored_and_group_count_is_exact(count):
    rows = [
        {"bucket": "A", "value": None if i % 3 == 0 else i}
        for i in range(count)
    ]
    ops = DataOperations(None)
    _, aggregate = ops._aggregate_records(
        None, data=json.dumps(rows), data_format="json", table_index=0,
        group_by=["bucket"], filters=None, sort_by=None, descending=False,
        aggregations=[
            {"column": "value", "function": "sum", "alias": "sum"},
            {"column": "value", "function": "count", "alias": "count"},
        ],
    )
    assert aggregate == [{
        "bucket": "A",
        "sum": sum(i for i in range(count) if i % 3),
        "count": sum(1 for i in range(count) if i % 3),
    }]
