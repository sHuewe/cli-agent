"""Regression scenarios for chaining metadata-only Data MCP artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from cli_agent.config import McpServerConfig
from cli_agent.data_mcp_server import _server_instructions
from cli_agent.data_operations import DataOperations
from cli_agent.data_result_metadata import structured_table_result
from cli_agent.dataframe_operations import DataFrameOperations
from cli_agent.dataframe_types import DataFrameInput, DataFrameSource, ResampleAggregation
from cli_agent.mcp_result_artifacts import McpResultArtifacts
from cli_agent.os_operations import Workspace


def _frames(tmp_path):
    workspace = Workspace.from_directory(
        tmp_path, McpServerConfig(name="data", config={"allow_write_files": True}),
    )
    operations = DataOperations(workspace)
    return DataFrameOperations(operations), operations


def _save_artifact(store, result):
    metadata = store.metadata_result(
        result, json.dumps(result.structuredContent, ensure_ascii=False),
    )
    assert metadata is not None
    model_metadata = json.loads(metadata)
    assert model_metadata["records_path"] == "/rows"
    assert "rows" not in model_metadata
    return model_metadata


def test_known_pointer_join_resample_select_chain_without_json_discovery(tmp_path):
    timestamps = ["2026-07-28T18:00:00Z", "2026-07-28T18:04:00Z", "2026-07-28T18:12:00Z"]
    for name, values in (("l1.json", [100, 200, 300]), ("l3.json", [20, 30, 40])):
        (tmp_path / name).write_text(json.dumps({
            "points": [
                {"timestamp": timestamp, "value": value}
                for timestamp, value in zip(timestamps, values)
            ],
        }), encoding="utf-8")

    frames, operations = _frames(tmp_path)
    store = McpResultArtifacts(tmp_path, ())
    try:
        joined = frames.join_data([
            DataFrameSource("l1.json", "L1", records_path="/points"),
            DataFrameSource("l3.json", "L3", records_path="/points"),
        ])
        joined_metadata = _save_artifact(store, joined)
        assert joined_metadata["row_count"] == 3

        resampled = frames.resample_data(
            joined_metadata["workspace_path"], "10min",
            [ResampleAggregation("L1", "mean", "avg_L1")],
            records_path=joined_metadata["records_path"],
        )
        resampled_metadata = _save_artifact(store, resampled)
        assert [row["avg_L1"] for row in resampled.structuredContent["rows"]] == [150.0, 300.0]

        columns, filtered = operations._select_records(
            resampled_metadata["workspace_path"],
            records_path=resampled_metadata["records_path"],
            columns=["timestamp", "avg_L1"],
            filters=[{"column": "avg_L1", "op": "gt", "value": 200}],
        )
        selected_metadata = _save_artifact(
            store, structured_table_result(columns, filtered, operation="select_data"),
        )
        assert selected_metadata["row_count"] == 1
        assert operations._read_records(
            selected_metadata["workspace_path"], records_path=selected_metadata["records_path"],
        )[1] == filtered
    finally:
        store.cleanup()


def test_thirteen_history_blocks_concat_in_two_stages_then_resample(tmp_path):
    start = datetime(2026, 1, 5, 12, tzinfo=timezone.utc)
    for index in range(13):
        timestamp = (start + timedelta(weeks=index)).isoformat()
        (tmp_path / f"week-{index}.json").write_text(json.dumps({
            "points": [{"timestamp": timestamp, "value": index + 1}],
        }), encoding="utf-8")

    frames, operations = _frames(tmp_path)
    store = McpResultArtifacts(tmp_path, ())
    try:
        intermediate = []
        for indexes in (range(8), range(8, 13)):
            result = frames.concat_data([
                DataFrameInput(f"week-{index}.json", records_path="/points")
                for index in indexes
            ])
            intermediate.append(_save_artifact(store, result))

        combined = frames.concat_data([
            DataFrameInput(item["workspace_path"], records_path=item["records_path"])
            for item in intermediate
        ], sort_by=["timestamp"])
        combined_metadata = _save_artifact(store, combined)
        assert combined_metadata["row_count"] == 13

        daily = frames.resample_data(
            combined_metadata["workspace_path"], "24h",
            [ResampleAggregation("value", "mean", "average")],
            records_path=combined_metadata["records_path"],
        )
        daily_metadata = _save_artifact(store, daily)
        assert [row["average"] for row in daily.structuredContent["rows"] if row["average"] is not None] == list(range(1, 14))
        assert len(operations._read_records(
            daily_metadata["workspace_path"], records_path=daily_metadata["records_path"],
        )[1]) == daily_metadata["row_count"]

        # Inspect one promising window in the *original* combined samples.
        columns, original_rows = operations._select_records(
            combined_metadata["workspace_path"],
            records_path=combined_metadata["records_path"],
            filters=[
                {"column": "timestamp", "op": "gte", "value": (start + timedelta(weeks=6)).isoformat()},
                {"column": "timestamp", "op": "lte", "value": (start + timedelta(weeks=7)).isoformat()},
            ],
        )
        selected = _save_artifact(
            store, structured_table_result(columns, original_rows, operation="select_data"),
        )
        preview = json.loads(operations.inspect_data(
            selected["workspace_path"], records_path=selected["records_path"],
            sample_rows=1, offset=1,
        ))
        assert [row["value"] for row in preview["sample"]] == [8]
        assert preview["row_count"] == 2
        assert preview["offset"] == 1
        assert preview["next_offset"] is None
    finally:
        store.cleanup()


def test_data_mcp_instructions_remain_compact_and_reuse_known_pointers():
    instructions = _server_instructions("read")
    assert len(instructions) <= 1_743  # Size of the instructions before D1/D4.
    assert "Always propagate both" in instructions
    assert "concat_data blocks" in instructions
    assert "refetch only if insufficient" in instructions
    assert "Sort/filter with select_data, then page with preview_data" in instructions
    assert "inspect_json(path) can discover it" in instructions
