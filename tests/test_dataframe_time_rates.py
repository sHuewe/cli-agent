"""D2/D3: time-normalized differences and evidence-based timezone metadata."""

from __future__ import annotations

import json

import pytest

from cli_agent.settings.config import McpServerConfig
from cli_agent.data.data_mcp_server import create_server
from cli_agent.data.data_operations import DataOperationError, DataOperations
from cli_agent.data.dataframe_operations import DataFrameOperations
from cli_agent.data.dataframe_types import DataFrameSource, DerivedColumn, ResampleAggregation
from cli_agent.security.mcp_result_artifacts import METADATA_FOR_LLM_KEY, McpResultArtifacts
from cli_agent.platform.os_operations import AllowedPath, Workspace


def _frames(tmp_path):
    workspace = Workspace.from_directory(
        tmp_path, McpServerConfig(name="data", config={"allow_write_files": True}),
    )
    return DataFrameOperations(DataOperations(workspace))


def _write(tmp_path, name, timestamps, values):
    (tmp_path / name).write_text(json.dumps({
        "measurements": [
            {"timestamp": timestamp, "value": value}
            for timestamp, value in zip(timestamps, values)
        ],
    }), encoding="utf-8")


def _rate(frames, name="series.json", **kwargs):
    return frames.derive_columns(
        name, [DerivedColumn("rate", "diff_per_time", "value", **kwargs)],
        records_path="/measurements",
    )


def test_rate_is_exposed_in_the_existing_derive_columns_mcp_schema(tmp_path):
    server = create_server(_frames(tmp_path).base, access="read")
    tool_schema = server._tool_manager.get_tool("derive_columns").parameters
    operation_schema = tool_schema["$defs"]["DerivedColumn"]
    assert "diff_per_time" in operation_schema["properties"]["operation"]["enum"]
    assert operation_schema["properties"]["time_unit"]["enum"] == [
        "seconds", "minutes", "hours",
    ]
    assert "max_gap_seconds" in operation_schema["properties"]
    assert tool_schema["required"] == ["path", "operations"]


def test_diff_per_time_uses_actual_irregular_intervals_and_preserves_diff(tmp_path):
    _write(tmp_path, "series.json", [
        "2026-07-28T18:00:00Z", "2026-07-28T18:10:00Z",
        "2026-07-28T18:22:00Z", "2026-07-28T18:23:00Z",
    ], [25, 24, 23, 22])
    frames = _frames(tmp_path)
    result = frames.derive_columns("series.json", [
        DerivedColumn("difference", "diff", "value"),
        DerivedColumn("rate", "diff_per_time", "value", max_gap_seconds=3600),
    ], records_path="/measurements")
    assert [row["difference"] for row in result.structuredContent["rows"]] == [None, -1, -1, -1]
    assert [row["rate"] for row in result.structuredContent["rows"]] == [None, -6, -5, -60]
    assert result.structuredContent["summary"] == {"rate_time_units": {"rate": "hours"}}
    assert result.meta[METADATA_FOR_LLM_KEY]["records_path"] == "/rows"


@pytest.mark.parametrize(("unit", "expected"), [
    ("seconds", -1 / 600), ("minutes", -0.1), ("hours", -6),
])
def test_rate_time_unit_is_explicit(tmp_path, unit, expected):
    _write(tmp_path, "series.json", [
        "2026-07-28T18:00:00Z", "2026-07-28T18:10:00Z",
    ], [25, 24])
    result = _rate(_frames(tmp_path), max_gap_seconds=3600, time_unit=unit)
    assert result.structuredContent["rows"][1]["rate"] == pytest.approx(expected)
    assert result.meta[METADATA_FOR_LLM_KEY]["summary"]["rate_time_units"] == {"rate": unit}


def test_rate_nulls_identical_timestamps_and_large_gaps_do_not_bridge(tmp_path):
    _write(tmp_path, "series.json", [
        "2026-07-28T18:00:00Z", "2026-07-28T18:10:00Z",
        "2026-07-28T18:10:00Z", "2026-07-28T18:20:00Z",
        "2026-07-28T18:30:00Z", "2026-07-28T20:00:00Z",
        "2026-07-28T20:10:00Z",
    ], [25, 24, 23, None, 21, 20, 19])
    rows = _rate(_frames(tmp_path), max_gap_seconds=1800).structuredContent["rows"]
    assert [row["rate"] for row in rows] == [None, -6, None, None, None, None, -6]


def test_rate_rejects_unsorted_timestamps_and_missing_timestamps(tmp_path):
    _write(tmp_path, "series.json", [
        "2026-07-28T18:10:00Z", "2026-07-28T18:00:00Z",
    ], [25, 24])
    with pytest.raises(DataOperationError, match="aufsteigende Zeitstempel"):
        _rate(_frames(tmp_path), max_gap_seconds=3600)

    _write(tmp_path, "series.json", [None, "2026-07-28T18:10:00Z"], [25, 24])
    with pytest.raises(DataOperationError, match="Zeitstempel dürfen nicht leer"):
        _rate(_frames(tmp_path), max_gap_seconds=3600)


@pytest.mark.parametrize("limit", [None, 0, -1, float("nan"), float("inf"), True, 604_801])
def test_rate_requires_finite_explicit_gap_limit(tmp_path, limit):
    _write(tmp_path, "series.json", [
        "2026-07-28T18:00:00Z", "2026-07-28T18:10:00Z",
    ], [25, 24])
    with pytest.raises(DataOperationError, match="max_gap_seconds"):
        _rate(_frames(tmp_path), max_gap_seconds=limit)


def test_rate_preserves_nanosecond_and_dst_elapsed_time(tmp_path):
    _write(tmp_path, "series.json", [
        "2026-07-28T18:00:00.000000000Z",
        "2026-07-28T18:00:00.000000001Z",
    ], [1, 2])
    result = _rate(_frames(tmp_path), max_gap_seconds=1, time_unit="seconds")
    assert result.structuredContent["rows"][1]["rate"] == pytest.approx(1e9)

    _write(tmp_path, "series.json", [
        "2026-10-25T02:30:00+02:00", "2026-10-25T02:30:00+01:00",
    ], [20, 19])
    result = _rate(_frames(tmp_path), max_gap_seconds=7200)
    assert result.structuredContent["rows"][1]["rate"] == -1


def test_rate_supports_a_custom_time_column_and_rejects_nonfinite_results(tmp_path):
    (tmp_path / "custom.json").write_text(json.dumps({
        "measurements": [
            {"observed_at": "2026-07-28T18:00:00Z", "temperature": 25},
            {"observed_at": "2026-07-28T18:12:00Z", "temperature": 24},
        ],
    }), encoding="utf-8")
    result = _frames(tmp_path).derive_columns("custom.json", [
        DerivedColumn(
            "delta", "diff_per_time", "temperature", time_column="observed_at",
            time_unit="minutes", max_gap_seconds=1800,
        ),
    ], records_path="/measurements")
    assert result.structuredContent["rows"][1]["delta"] == pytest.approx(-1 / 12)

    _write(tmp_path, "series.json", [
        "2026-07-28T18:00:00Z", "2026-07-28T18:00:01Z",
    ], [1e308, -1e308])
    with pytest.raises(DataOperationError, match="nichtendlichen Wert"):
        _rate(_frames(tmp_path), max_gap_seconds=60)


def test_rate_avoids_intermediate_overflow_for_finite_results(tmp_path):
    _write(tmp_path, "series.json", [
        "2026-07-28T18:00:00Z", "2026-07-28T19:00:00Z",
    ], [0, 1e306])
    result = _rate(_frames(tmp_path), max_gap_seconds=3600, time_unit="hours")
    assert result.structuredContent["rows"][1]["rate"] == pytest.approx(1e306)

    # The unscaled difference overflows float64, but its rate over two hours
    # is finite and must still be representable.
    _write(tmp_path, "series.json", [
        "2026-07-28T18:00:00Z", "2026-07-28T20:00:00Z",
    ], [-1e308, 1e308])
    result = _rate(_frames(tmp_path), max_gap_seconds=7200, time_unit="hours")
    assert result.structuredContent["rows"][1]["rate"] == pytest.approx(1e308)


def test_rate_rejects_irrelevant_binary_arguments_and_unknown_units(tmp_path):
    _write(tmp_path, "series.json", [
        "2026-07-28T18:00:00Z", "2026-07-28T18:10:00Z",
    ], [25, 24])
    with pytest.raises(DataOperationError, match="keine Binär-/Fensterparameter"):
        _rate(_frames(tmp_path), max_gap_seconds=3600, constant=5)
    with pytest.raises(DataOperationError, match="time_unit"):
        _rate(_frames(tmp_path), max_gap_seconds=3600, time_unit="weeks")
    with pytest.raises(DataOperationError, match="nur für diff_per_time"):
        _frames(tmp_path).derive_columns("series.json", [
            DerivedColumn("ordinary_diff", "diff", "value", max_gap_seconds=3600),
        ], records_path="/measurements")


def test_resample_then_diff_chains_through_json_artifact(tmp_path):
    _write(tmp_path, "series.json", [
        "2026-07-28T18:00:00Z", "2026-07-28T18:01:00Z",
        "2026-07-28T18:30:00Z", "2026-07-28T18:31:00Z",
    ], [25, 24, 23, 22])
    frames = _frames(tmp_path)
    store = McpResultArtifacts(tmp_path, ())
    frames.base.workspace = Workspace.from_directory(tmp_path,
        McpServerConfig(name="data", config={"allow_write_files": True}),
        allowed_paths=(AllowedPath(store.session_directory, "read"),))
    try:
        sampled = frames.resample_data(
            "series.json", "30min", [ResampleAggregation("value", "mean", "avg")],
            records_path="/measurements",
        )
        metadata = json.loads(store.metadata_result(
            sampled, json.dumps(sampled.structuredContent),
        ))
        changed = frames.derive_columns(metadata["workspace_path"], [
            DerivedColumn("change_30min", "diff", "avg"),
        ], records_path=metadata["records_path"])
        assert [row["change_30min"] for row in changed.structuredContent["rows"]] == [None, -2]
    finally:
        store.cleanup()


def test_aware_join_reports_utc_and_only_observed_source_offset(tmp_path):
    timestamps = ["2026-07-28T18:03:00+02:00", "2026-07-28T18:13:00+02:00"]
    _write(tmp_path, "l1.json", timestamps, [10, 20])
    _write(tmp_path, "l2.json", timestamps, [30, 40])
    result = _frames(tmp_path).join_data([
        DataFrameSource("l1.json", "L1", records_path="/measurements"),
        DataFrameSource("l2.json", "L2", records_path="/measurements"),
    ])
    metadata = result.meta[METADATA_FOR_LLM_KEY]
    assert metadata["timestamp_timezone"] == "UTC"
    assert metadata["source_timezone"] == "+02:00"
    assert result.structuredContent["rows"][0]["timestamp"].startswith("2026-07-28T16:03:00")
    assert result.structuredContent["rows"][0]["timestamp"].endswith("Z")

    _write(tmp_path, "l2.json", ["2026-07-28T16:03:00Z", "2026-07-28T16:13:00Z"], [30, 40])
    result = _frames(tmp_path).join_data([
        DataFrameSource("l1.json", "L1", records_path="/measurements"),
        DataFrameSource("l2.json", "L2", records_path="/measurements"),
    ])
    metadata = result.meta[METADATA_FOR_LLM_KEY]
    assert metadata["timestamp_timezone"] == "UTC"
    assert "source_timezone" not in metadata


def test_resample_timezone_metadata_for_offset_dst_change_and_naive_input(tmp_path):
    frames = _frames(tmp_path)
    _write(tmp_path, "series.json", [
        "2026-07-28T18:00:00+02:00", "2026-07-28T18:10:00+02:00",
    ], [10, 20])
    result = frames.resample_data(
        "series.json", "10min", [ResampleAggregation("value", "mean", "avg")],
        records_path="/measurements",
    )
    assert result.meta[METADATA_FOR_LLM_KEY]["timestamp_timezone"] == "UTC"
    assert result.meta[METADATA_FOR_LLM_KEY]["source_timezone"] == "+02:00"

    _write(tmp_path, "series.json", [
        "2026-10-25T02:30:00+02:00", "2026-10-25T02:30:00+01:00",
    ], [10, 20])
    result = frames.resample_data(
        "series.json", "30min", [ResampleAggregation("value", "mean", "avg")],
        records_path="/measurements",
    )
    metadata = result.meta[METADATA_FOR_LLM_KEY]
    assert metadata["timestamp_timezone"] == "UTC"
    assert "source_timezone" not in metadata
    assert [row["timestamp"][:16] for row in result.structuredContent["rows"]] == [
        "2026-10-25T00:30", "2026-10-25T01:00", "2026-10-25T01:30",
    ]

    _write(tmp_path, "series.json", [
        "2026-07-28T18:00:00", "2026-07-28T18:10:00",
    ], [10, 20])
    result = frames.resample_data(
        "series.json", "10min", [ResampleAggregation("value", "mean", "avg")],
        records_path="/measurements",
    )
    metadata = result.meta[METADATA_FOR_LLM_KEY]
    assert "timestamp_timezone" not in metadata
    assert "source_timezone" not in metadata
