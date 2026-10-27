from __future__ import annotations

import json
from decimal import Decimal

from cli_agent.data_result_metadata import (
    MAX_SUMMARY_METADATA_BYTES,
    add_large_result_summary,
    markdown_result,
)
from cli_agent.mcp_result_artifacts import METADATA_FOR_LLM_KEY


def _large_metadata(columns, rows, *, operation="select_data"):
    metadata = {"operation": operation, "row_count": len(rows), "columns": columns, "format": "markdown"}
    add_large_result_summary(metadata, columns, rows)
    return metadata


def _size(metadata):
    return len(json.dumps(metadata, ensure_ascii=False, allow_nan=False).encode("utf-8"))


def test_large_result_statistics_cover_all_rows_not_just_the_preview():
    rows = [
        {"timestamp": f"2026-10-27T{hour:02d}:00:00", "power": hour * 10, "zone": "A" if hour < 5 else "B"}
        for hour in range(12)
    ]
    rows[3]["power"] = None
    rows[-1]["power"] = 2_000
    metadata = _large_metadata(["timestamp", "power", "zone"], rows)
    stats = {s["column"]: s for s in metadata["column_stats"]}
    assert stats["timestamp"]["min"] == "2026-10-27T00:00:00"
    assert stats["timestamp"]["max"] == "2026-10-27T11:00:00"
    assert stats["power"]["null_count"] == 1
    assert stats["power"]["min"] == 0
    assert stats["power"]["max"] == 2_000
    assert stats["power"]["mean"] == sum(row["power"] for row in rows if row["power"] is not None) / 11
    assert stats["zone"]["value_counts"] == [
        {"value": "A", "count": 5}, {"value": "B", "count": 7},
    ]
    assert len(metadata["sample_rows"]) == 5
    assert metadata["sample_rows_complete"] is False
    assert metadata["stats_columns_complete"] is True
    assert _size(metadata) + 100 < MAX_SUMMARY_METADATA_BYTES


def test_large_result_budget_is_utf8_bounded_with_many_columns():
    columns = [f"column_{i}_" + "ä" * 80 for i in range(100)]
    rows = [{column: i for i, column in enumerate(columns)} for _ in range(3)]
    metadata = _large_metadata(columns, rows)
    assert _size(metadata) + 100 < MAX_SUMMARY_METADATA_BYTES
    assert metadata["column_count"] == 100
    assert metadata["columns_complete"] is False
    assert metadata["stats_columns_complete"] is False
    assert len(metadata["columns"]) < len(columns)
    assert len(metadata["sample_rows"]) == 0
    assert metadata["sample_rows_complete"] is False


def test_wide_strings_are_never_partially_exposed_in_samples():
    metadata = _large_metadata(["id", "secret"], [
        {"id": i, "secret": "ä" * 9_000} for i in range(2)
    ])
    assert metadata["sample_rows"] == []
    assert metadata["sample_rows_complete"] is False
    assert metadata["stats_columns_complete"] is True
    assert _size(metadata) < MAX_SUMMARY_METADATA_BYTES


def test_low_cardinality_domains_are_exact_and_high_cardinality_is_omitted():
    metadata = _large_metadata(["group", "mixed"], [
        {"group": f"group-{i}", "mixed": 1 if i == 0 else "text"} for i in range(8)
    ])
    stats = {s["column"]: s for s in metadata["column_stats"]}
    assert "value_counts" not in stats["group"]
    assert "min" not in stats["mixed"]
    assert "mean" not in stats["mixed"]


def test_time_range_accounts_for_offsets_and_nanosecond_ties():
    metadata = _large_metadata(["timestamp"], [
        {"timestamp": "2026-10-27T12:00:00+02:00"},
        {"timestamp": "2026-10-27T11:00:00+00:00"},
    ])
    stat = metadata["column_stats"][0]
    assert stat["min"] == "2026-10-27T12:00:00+02:00"
    assert stat["max"] == "2026-10-27T11:00:00+00:00"

    nanos = _large_metadata(["timestamp"], [
        {"timestamp": "2026-10-27T12:00:00.123456789"},
        {"timestamp": "2026-10-27T12:00:00.123456100"},
    ])
    assert nanos["column_stats"][0]["min"].endswith("100")
    assert nanos["column_stats"][0]["max"].endswith("789")

    # The extra fractional digits must be compared numerically, even when
    # another timestamp ends after the sixth digit, before a Z or offset.
    different_precision = _large_metadata(["timestamp"], [
        {"timestamp": "2026-10-27T12:00:00.123456100Z"},
        {"timestamp": "2026-10-27T12:00:00.123456Z"},
    ])
    assert different_precision["column_stats"][0]["min"] == "2026-10-27T12:00:00.123456Z"
    assert different_precision["column_stats"][0]["max"] == "2026-10-27T12:00:00.123456100Z"

    different_offsets = _large_metadata(["timestamp"], [
        {"timestamp": "2026-10-27T14:00:00.123456100+02:00"},
        {"timestamp": "2026-10-27T12:00:00.123456Z"},
    ])
    assert different_offsets["column_stats"][0]["min"] == "2026-10-27T12:00:00.123456Z"
    assert different_offsets["column_stats"][0]["max"] == "2026-10-27T14:00:00.123456100+02:00"

    equivalent_precision = _large_metadata(["timestamp"], [
        {"timestamp": "2026-10-27T12:00:00.1234561Z"},
        {"timestamp": "2026-10-27T12:00:00.123456100Z"},
    ])
    assert equivalent_precision["column_stats"][0]["min"] in [
        "2026-10-27T12:00:00.1234561Z", "2026-10-27T12:00:00.123456100Z",
    ]
    assert equivalent_precision["column_stats"][0]["max"] in [
        "2026-10-27T12:00:00.1234561Z", "2026-10-27T12:00:00.123456100Z",
    ]


def test_mixed_timezone_styles_do_not_claim_a_time_range():
    metadata = _large_metadata(["timestamp"], [
        {"timestamp": "2026-10-27T12:00:00"},
        {"timestamp": "2026-10-27T13:00:00Z"},
    ])
    assert "min" not in metadata["column_stats"][0]


def test_decimal_values_stay_explicitly_typed_in_metadata():
    metadata = _large_metadata(["total"], [
        {"total": Decimal("0.12345678901234567890")},
        {"total": Decimal("1.12345678901234567890")},
    ])
    stats = metadata["column_stats"][0]
    assert stats["min"] == {"decimal": "0.12345678901234567890"}
    assert stats["max"] == {"decimal": "1.12345678901234567890"}
    assert metadata["sample_rows"][0]["total"] == {"decimal": "0.12345678901234567890"}
    assert _size(metadata) < MAX_SUMMARY_METADATA_BYTES


def test_markdown_result_includes_summary_only_when_over_inline_limit():
    rows = [{"value": 1}, {"value": 3}]
    small = markdown_result("short", ["value"], 2, operation="aggregate_data", rows=rows)
    assert "column_stats" not in small.meta[METADATA_FOR_LLM_KEY]
    assert small.meta[METADATA_FOR_LLM_KEY]["result"] == "short"

    large = markdown_result("x" * 5_001, ["value"], 2, operation="aggregate_data", rows=rows)
    metadata = large.meta[METADATA_FOR_LLM_KEY]
    assert "result" not in metadata
    assert metadata["column_stats"][0]["mean"] == 2
    assert metadata["sample_rows"] == rows
    assert metadata["sample_rows_complete"] is True
