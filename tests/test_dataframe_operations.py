from __future__ import annotations

import json

import pandas as pd
import pytest

from cli_agent.config import McpServerConfig
from cli_agent.data_operations import DataOperationError, DataOperations
from cli_agent.dataframe_operations import DataFrameOperations
from cli_agent.dataframe_types import DataFrameSource, DerivedColumn, ResampleAggregation
from cli_agent.mcp_result_artifacts import (
    METADATA_FOR_LLM_KEY,
    ONLY_METADATA_TO_LLM_KEY,
    McpResultArtifacts,
)
from cli_agent.os_operations import Workspace


def _base(tmp_path):
    return DataOperations(Workspace.from_directory(
        tmp_path, McpServerConfig(name="data", config={"allow_write_files": True}),
    ))


def _write_json(tmp_path, name, rows):
    (tmp_path / name).write_text(json.dumps({"measurements": rows}), encoding="utf-8")


def test_json_file_and_pointer_work_with_existing_tools(tmp_path):
    _write_json(tmp_path, "readings.json", [
        {"t": "2026-06-24T07:00:00", "w": -450},
        {"t": "2026-06-24T07:01:00", "w": -430},
    ])
    operations = _base(tmp_path)
    discovered = json.loads(operations.inspect_json("readings.json"))
    assert discovered["record_sets"] == [{
        "records_path": "/measurements", "row_count": 2,
        "columns": ["t", "w"], "columns_complete": True,
    }]
    assert "2026-06-24" not in json.dumps(discovered)
    sample = json.loads(operations.inspect_data(
        "readings.json", records_path="/measurements", sample_rows=1,
    ))
    assert sample["sample"] == [{"t": "2026-06-24T07:00:00", "w": -450}]
    assert "w" in operations.select_data("readings.json", records_path="/measurements")
    assert json.loads(operations.inspect_schema(
        "readings.json", records_path="/measurements",
    ))["row_count"] == 2
    with pytest.raises(DataOperationError, match="records_path"):
        operations.inspect_data("readings.json", records_path="/missing")
    with pytest.raises(DataOperationError, match="JSON-Pointer"):
        operations.inspect_data("readings.json", records_path="/measurements/~2")


def test_json_pointer_escaping_and_file_access_boundary(tmp_path):
    (tmp_path / "nested.json").write_text(json.dumps({
        "a/b": {"x~y": [{"value": 10}]},
    }), encoding="utf-8")
    ops = _base(tmp_path)
    assert json.loads(ops.inspect_json("nested.json"))["record_sets"][0]["records_path"] == "/a~1b/x~0y"
    assert ops._read_records("nested.json", records_path="/a~1b/x~0y")[1] == [{"value": 10}]
    (tmp_path / "table.csv").write_text("value\n10\n", encoding="utf-8")
    with pytest.raises(DataOperationError, match="nur für JSON"):
        ops.inspect_data("table.csv", records_path="/rows")
    with pytest.raises(Exception):
        ops.inspect_json("../nested.json")


def test_inspect_json_reports_incomplete_when_traversal_is_truncated(tmp_path):
    ops = _base(tmp_path)
    cases = {
        "properties.json": {**{f"field{i}": i for i in range(100)}, "measurements": [{"power": 1}]},
        "elements.json": [*range(20), {"measurements": [{"power": 1}]}],
        "discovered.json": {f"series{i}": [{"value": i}] for i in range(51)},
    }
    deep = [{"value": 1}]
    for _ in range(17):
        deep = {"nested": deep}
    cases["depth.json"] = deep

    for name, document in cases.items():
        (tmp_path / name).write_text(json.dumps(document), encoding="utf-8")
        result = json.loads(ops.inspect_json(name))
        assert result["complete"] is False, name

    assert json.loads(ops.inspect_json("properties.json"))["record_sets"] == []
    assert json.loads(ops.inspect_json("elements.json"))["record_sets"] == []
    assert len(json.loads(ops.inspect_json("discovered.json"))["record_sets"]) == 50


def test_inspect_json_reports_complete_for_fully_examined_documents(tmp_path):
    (tmp_path / "complete.json").write_text(json.dumps({
        "metadata": {"source": "sensor"},
        "measurements": [{"value": 1}, {"value": 2}],
    }), encoding="utf-8")
    result = json.loads(_base(tmp_path).inspect_json("complete.json"))
    assert result["complete"] is True
    assert result["record_sets"][0]["records_path"] == "/measurements"


def test_inline_json_pointer_and_root_json_array(tmp_path):
    ops = DataOperations(None)
    assert ops._read_records(data='[{"x":1}]', data_format="json")[1] == [{"x": 1}]
    assert ops._read_records(data='{"rows":[{"x":2}]}', data_format="json", records_path="/rows")[1] == [{"x": 2}]
    with pytest.raises(DataOperationError):
        ops._read_records(data='{}', data_format="csv", records_path="/rows")


@pytest.fixture
def frames(tmp_path):
    _write_json(tmp_path, "l1.json", [
        {"timestamp": "2026-06-24T07:00:00", "power": -500},
        {"timestamp": "2026-06-24T07:01:00", "power": -400},
        {"timestamp": "2026-06-24T07:02:00", "power": -300},
    ])
    _write_json(tmp_path, "l3.json", [
        {"timestamp": "2026-06-24T07:00:00", "power": -505},
        {"timestamp": "2026-06-24T07:01:00", "power": -1000},
        {"timestamp": "2026-06-24T07:02:00", "power": -1000},
    ])
    _write_json(tmp_path, "pv.json", [
        {"timestamp": "2026-06-24T07:00:20", "power": 1300},
        {"timestamp": "2026-06-24T07:03:00", "power": 1800},
    ])
    return DataFrameOperations(_base(tmp_path))


def _source(filename, alias):
    return DataFrameSource(filename, alias, value_column="power", records_path="/measurements")


def test_exact_join_derived_difference_and_session_cleanup(frames, tmp_path):
    joined = frames.join_data([_source("l1.json", "L1"), _source("l3.json", "L3")])
    assert joined.structuredContent["columns"] == ["timestamp", "L1", "L3"]
    assert joined.structuredContent["rows"][1]["L3"] == -1000
    assert joined.meta[ONLY_METADATA_TO_LLM_KEY] is True
    assert joined.meta[METADATA_FOR_LLM_KEY]["records_path"] == "/rows"

    store = McpResultArtifacts(tmp_path, ())
    serialized = json.dumps(joined.structuredContent, ensure_ascii=False)
    metadata = json.loads(store.metadata_result(joined, serialized))
    path = metadata["workspace_path"]
    assert (tmp_path / path).is_file()
    derived = frames.derive_columns(
        path, [DerivedColumn("D", "subtract", "L1", other_column="L3")],
        records_path=metadata["records_path"],
    )
    assert [row["D"] for row in derived.structuredContent["rows"]] == [5, 600, 700]
    assert json.loads(store.metadata_result(derived, json.dumps(derived.structuredContent)))["rows"] == derived.structuredContent["rows"]
    store.cleanup()
    assert not (tmp_path / path).exists()
    assert not list(tmp_path.glob("mcp-data-*"))


def test_nearest_join_requires_explicit_tolerance(frames):
    sources = [_source("l1.json", "L1"), _source("pv.json", "PV")]
    with pytest.raises(DataOperationError, match="tolerance_seconds"):
        frames.join_data(sources, direction="nearest", how="left")
    with pytest.raises(DataOperationError, match="left"):
        frames.join_data(sources, direction="nearest", how="inner", tolerance_seconds=30)
    result = frames.join_data(sources, direction="nearest", how="left", tolerance_seconds=30)
    assert [row["PV"] for row in result.structuredContent["rows"]] == [1300, None, None]


def test_duplicate_join_timestamps_rejected(frames, tmp_path):
    _write_json(tmp_path, "dup.json", [
        {"timestamp": "2026-06-24T07:00:00", "power": 2},
        {"timestamp": "2026-06-24T07:00:00", "power": 3},
    ])
    with pytest.raises(DataOperationError, match="doppelte"):
        frames.join_data([_source("l1.json", "L1"), _source("dup.json", "dup")])


@pytest.mark.parametrize("missing_timestamp", ["", "NaT"])
def test_join_and_resample_reject_timestamps_missing_after_conversion(frames, tmp_path, missing_timestamp):
    _write_json(tmp_path, "missing_time.json", [
        {"timestamp": "2026-06-24T07:00:00", "power": 1},
        {"timestamp": missing_timestamp, "power": 2},
    ])
    with pytest.raises(DataOperationError, match="Zeitstempel dürfen nicht leer"):
        frames.join_data([_source("missing_time.json", "A"), _source("l1.json", "B")])
    with pytest.raises(DataOperationError, match="Zeitstempel dürfen nicht leer"):
        frames.resample_data("missing_time.json", "1min", [
            ResampleAggregation("power", "sum", "total"),
        ], records_path="/measurements")


def test_resample_and_rolling_operations(frames):
    result = frames.resample_data(
        "l1.json", "2min", [ResampleAggregation("power", "mean", "avg")],
        records_path="/measurements",
    )
    assert [row["avg"] for row in result.structuredContent["rows"]] == [-450, -300]
    result = frames.derive_columns(
        "l1.json", [
            DerivedColumn("moving", "rolling_mean", "power", window=2),
            DerivedColumn("change", "diff", "power"),
        ], records_path="/measurements",
    )
    assert [row["moving"] for row in result.structuredContent["rows"]] == [None, -450, -350]
    assert [row["change"] for row in result.structuredContent["rows"]] == [None, 100, 100]


def test_division_by_zero_and_invalid_frequency_rejected(frames):
    with pytest.raises(DataOperationError, match="null"):
        frames.derive_columns("l1.json", [DerivedColumn("x", "divide", "power", constant=0)], records_path="/measurements")
    with pytest.raises(DataOperationError, match="frequency"):
        frames.resample_data("l1.json", "1D; import os", [ResampleAggregation("power", "mean", "avg")], records_path="/measurements")


def test_resample_empty_buckets_do_not_invent_power_values(frames, tmp_path):
    _write_json(tmp_path, "sparse.json", [
        {"timestamp": "2026-06-24T07:00:00", "power": 100},
        {"timestamp": "2026-06-24T07:10:00", "power": 300},
    ])
    result = frames.resample_data("sparse.json", "5min", [
        ResampleAggregation("power", "sum", "total"),
        ResampleAggregation("power", "count", "samples"),
    ], records_path="/measurements")
    assert [row["total"] for row in result.structuredContent["rows"]] == [100, None, 300]
    assert [row["samples"] for row in result.structuredContent["rows"]] == [1, 0, 1]


def test_resample_sums_preserve_nulls_per_column(frames, tmp_path):
    _write_json(tmp_path, "nullable.json", [
        {"timestamp": "2026-06-24T07:00:00", "L1": 100, "L3": None},
        {"timestamp": "2026-06-24T07:00:20", "L1": 200, "L3": None},
        {"timestamp": "2026-06-24T07:01:00", "L1": 0, "L3": 0},
        {"timestamp": "2026-06-24T07:01:20", "L1": None, "L3": None},
        {"timestamp": "2026-06-24T07:02:00", "L1": 300, "L3": 100},
        {"timestamp": "2026-06-24T07:04:00", "L1": 400, "L3": None},
    ])
    result = frames.resample_data("nullable.json", "1min", [
        ResampleAggregation("L1", "sum", "l1_total"),
        ResampleAggregation("L3", "sum", "l3_total"),
        ResampleAggregation("L3", "count", "l3_samples"),
    ], records_path="/measurements")
    rows = result.structuredContent["rows"]
    assert [row["l1_total"] for row in rows] == [300, 0, 300, None, 400]
    assert [row["l3_total"] for row in rows] == [None, 0, 100, None, None]
    assert [row["l3_samples"] for row in rows] == [0, 1, 1, 0, 0]


def test_resample_rejects_huge_sparse_span_before_allocating(frames, tmp_path, monkeypatch):
    _write_json(tmp_path, "year.json", [
        {"timestamp": "2025-01-01T00:00:00", "power": 100},
        {"timestamp": "2026-01-01T00:00:00", "power": 200},
    ])

    def unexpected_resample(*args, **kwargs):
        raise AssertionError("Dense resampling must not start for an oversized span")

    monkeypatch.setattr(pd.DataFrame, "resample", unexpected_resample)
    with pytest.raises(DataOperationError, match="Zeilenlimit"):
        frames.resample_data("year.json", "1s", [
            ResampleAggregation("power", "sum", "total"),
        ], records_path="/measurements")


def test_resample_checks_exact_bucket_count_at_limit(frames, tmp_path, monkeypatch):
    from cli_agent import dataframe_operations

    monkeypatch.setattr(dataframe_operations, "MAX_FRAME_ROWS", 3)
    _write_json(tmp_path, "three.json", [
        {"timestamp": "2026-06-24T00:00:59", "power": 1},
        {"timestamp": "2026-06-24T00:02:00", "power": 2},
    ])
    result = frames.resample_data("three.json", "1min", [
        ResampleAggregation("power", "sum", "total"),
    ], records_path="/measurements")
    assert [row["total"] for row in result.structuredContent["rows"]] == [1, None, 2]

    _write_json(tmp_path, "four.json", [
        {"timestamp": "2026-06-24T00:00:59", "power": 1},
        {"timestamp": "2026-06-24T00:03:00", "power": 2},
    ])
    with pytest.raises(DataOperationError, match="Zeilenlimit"):
        frames.resample_data("four.json", "1min", [
            ResampleAggregation("power", "sum", "total"),
        ], records_path="/measurements")


def test_exact_join_retains_fractional_second_timestamps(frames, tmp_path):
    _write_json(tmp_path, "nanoseconds.json", [
        {"timestamp": "2026-06-24T07:00:00.123456789", "power": 1},
    ])
    result = frames.join_data([
        _source("nanoseconds.json", "A"),
        _source("nanoseconds.json", "B"),
    ])
    assert result.structuredContent["rows"][0]["timestamp"].endswith(".123456789")


def test_pandas_output_bound_and_non_numeric_data_are_rejected(frames, tmp_path, monkeypatch):
    from cli_agent import dataframe_operations

    _write_json(tmp_path, "bad.json", [
        {"timestamp": "2026-06-24T07:00:00", "power": "not-a-number"},
    ])
    with pytest.raises(DataOperationError, match="nichtnumerische"):
        frames.join_data([_source("bad.json", "A"), _source("l1.json", "B")])
    monkeypatch.setattr(dataframe_operations, "MAX_FRAME_RESULT_CHARS", 10)
    with pytest.raises(DataOperationError, match="zu groß"):
        frames.join_data([_source("l1.json", "A"), _source("l3.json", "B")])


def test_large_frame_metadata_omits_rows_while_retaining_full_artifact(frames, tmp_path):
    _write_json(tmp_path, "large.json", [
        {"timestamp": f"2026-06-24T07:{i // 60:02d}:{i % 60:02d}", "power": i}
        for i in range(200)
    ])
    result = frames.join_data([_source("large.json", "A"), _source("large.json", "B")])
    assert len(result.structuredContent["rows"]) == 200
    assert "rows" not in result.meta[METADATA_FOR_LLM_KEY]


@pytest.mark.asyncio
async def test_fastmcp_schema_and_result_meta_roundtrip(frames):
    from cli_agent.data_mcp_server import create_server

    server = create_server(frames.base, access="write")
    tool = server._tool_manager.get_tool("join_data")
    assert tool is not None
    assert "sources" in tool.parameters["properties"]
    result = await server._tool_manager.call_tool("join_data", {
        "sources": [
            {"path": "l1.json", "alias": "L1", "value_column": "power", "records_path": "/measurements"},
            {"path": "l3.json", "alias": "L3", "value_column": "power", "records_path": "/measurements"},
        ],
    }, convert_result=True)
    assert result.meta[ONLY_METADATA_TO_LLM_KEY] is True
    assert result.structuredContent["rows"][1]["L3"] == -1000
