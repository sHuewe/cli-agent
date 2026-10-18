from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cli_agent.flow import load_flow, run_flow, validate_flow
from cli_agent.flow_json_operations import transform_json


def test_json_batch_count_and_order() -> None:
    result = transform_json(
        kind="batch",
        value=[{"id": str(i)} for i in range(5)],
        batch_size=2,
    )
    assert [batch["id"] for batch in result["batches"]] == [
        "b0001", "b0002", "b0003",
    ]
    assert [
        item["id"] for batch in result["batches"]
        for item in batch["items"]
    ] == [str(i) for i in range(5)]
    assert result["total_items"] == 5


def test_json_batch_size_is_bounded() -> None:
    result = transform_json(
        kind="batch",
        value=["a" * 700, "b" * 700],
        batch_size=5,
        max_batch_bytes=1024,
    )
    assert len(result["batches"]) == 2


def test_json_flatten_and_empty_batches() -> None:
    assert transform_json(kind="flatten", value=[[1, 2], [], [3]]) == {
        "items": [1, 2, 3],
    }
    assert transform_json(kind="batch", value=[]) == {
        "batches": [], "total_items": 0,
    }


@pytest.mark.parametrize("source", [None, {}, "text"])
def test_json_transform_rejects_non_lists(source: object) -> None:
    with pytest.raises(ValueError, match="JSON-Liste"):
        transform_json(kind="batch", value=source)


def test_json_transform_rejects_oversized_entry() -> None:
    with pytest.raises(ValueError, match="max_batch_bytes"):
        transform_json(
            kind="batch", value=["x" * 3000], max_batch_bytes=1024,
        )


def test_json_transform_rejects_nonlist_flatten_entry() -> None:
    with pytest.raises(ValueError, match="ausschließlich Listen"):
        transform_json(kind="flatten", value=[[1], 2])


def test_deterministic_step_reads_completed_checkpoint_without_llm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "source.md").write_text("Prompt never executed", encoding="utf-8")
    (tmp_path / "source.json").write_text(
        json.dumps([{"id": x} for x in "abc"]), encoding="utf-8",
    )
    (tmp_path / "flow.toml").write_text(
        'version = 1\n'
        '[[steps]]\nid = "source"\nprompt_file = "source.md"\n'
        'output = "source.json"\nresponse_format = "json"\n'
        '[[steps]]\nid = "batch"\noutput = "batches.json"\n'
        '[steps.json_operation]\nkind = "batch"\ninput = "source_json"\n'
        'batch_size = 2\n'
        '[steps.json_inputs.source_json]\npath = "source.json"\n',
        encoding="utf-8",
    )
    flow = load_flow(Path("flow.toml"), workspace=tmp_path)
    assert flow.steps[-1].prompt_file is None
    from cli_agent import flow as flow_module

    async def unexpected_model(*args: object, **kwargs: object) -> None:
        raise AssertionError("No model should run")

    monkeypatch.setattr(flow_module, "run_once", unexpected_model)
    asyncio.run(run_flow(flow, workspace=tmp_path))
    value = json.loads((tmp_path / "batches.json").read_text(encoding="utf-8"))
    assert value["total_items"] == 3
    assert [row["id"] for group in value["batches"] for row in group["items"]] == list("abc")
    # Resuming uses the checkpoint rather than invoking the transformation again.
    asyncio.run(run_flow(flow, workspace=tmp_path))


def test_json_step_rejects_llm_and_tool_options(tmp_path: Path) -> None:
    (tmp_path / "flow.toml").write_text(
        'version = 1\n[[steps]]\nid = "bad"\nprompt_file = "no.md"\n'
        'output = "result.json"\n'
        '[steps.json_operation]\nkind = "batch"\ninput = "source"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="json_operation"):
        load_flow(Path("flow.toml"), workspace=tmp_path)
