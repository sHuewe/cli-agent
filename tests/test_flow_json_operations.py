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


def test_batch_byte_limit_includes_json_envelope() -> None:
    result = transform_json(
        kind="batch",
        value=["x" * 900, "y" * 900],
        batch_size=5,
        max_batch_bytes=1024,
    )
    assert len(result["batches"]) == 2
    for batch in result["batches"]:
        assert len(json.dumps(batch, separators=(",", ":")).encode("utf-8")) <= 1024


def test_batch_count_is_bounded_before_unbounded_allocation() -> None:
    import cli_agent.flow_json_operations as module

    with pytest.raises(ValueError, match="Batch-Anzahllimit"):
        module.transform_json(
            kind="batch",
            value=list(range(module.MAX_BATCHES + 1)),
            batch_size=1,
        )


def test_batch_result_limit_is_enforced_incrementally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli_agent.flow_json_operations as module

    monkeypatch.setattr(module, "MAX_RESULT_BYTES", 200)
    with pytest.raises(ValueError, match="Ergebnislimit"):
        module.transform_json(
            kind="batch",
            value=list(range(30)),
            batch_size=1,
        )


def test_batch_unicode_uses_emitted_utf8_size() -> None:
    # Escaping this string would exceed the limit; UTF-8 output fits.
    source = ["é" * 200]
    result = transform_json(
        kind="batch", value=source, max_batch_bytes=1024,
    )
    assert result["batches"][0]["items"] == source
    assert len(json.dumps(
        result["batches"][0], ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")) <= 1024


def test_batch_uses_exact_flow_serializer_for_numbers() -> None:
    from cli_agent.flow import _JsonNumber, _json_dumps_preserving_numbers

    source = [_JsonNumber("1e-200")] * 5
    result = transform_json(
        kind="batch", value=source, max_batch_bytes=1024,
        serializer=_json_dumps_preserving_numbers,
    )
    assert len(result["batches"]) == 1
    serialized = _json_dumps_preserving_numbers(result)
    assert '"items":[1e-200,1e-200' in serialized


def test_batch_utf8_boundary_accounts_for_id_and_payload() -> None:
    from cli_agent.flow import _json_dumps_preserving_numbers

    source = ["é" * 490, "é" * 490]
    result = transform_json(
        kind="batch", value=source, max_batch_bytes=1024,
        serializer=_json_dumps_preserving_numbers,
    )
    assert len(result["batches"]) == 2
    for batch in result["batches"]:
        assert len(_json_dumps_preserving_numbers(batch).encode("utf-8")) <= 1024


def test_batch_routes_splits_large_route_without_losing_items() -> None:
    route = {
        "document_id": "m001", "chunk_id": "c003", "folder_id": "foundations",
        "candidate_concepts": [{"id": "topic001", "description": "x" * 400}],
        "knowledge_items": [
            {"id": f"c003:rule{i:03d}", "description": "é" * 300}
            for i in range(7)
        ],
        "warnings": ["warning-" + "x" * 250],
        "source_metadata": {"version": 1},
    }
    result = transform_json(
        kind="batch_routes", value=[route], batch_size=3,
        max_batch_bytes=1024,
    )
    fragments = [item for batch in result["batches"] for item in batch["items"]]
    assert len(fragments) > 1
    for fragment in fragments:
        assert fragment["document_id"] == "m001"
        assert fragment["chunk_id"] == "c003"
        assert fragment["folder_id"] == "foundations"
        assert fragment["source_metadata"] == route["source_metadata"]
    for field in ("candidate_concepts", "knowledge_items", "warnings"):
        assert [item for fragment in fragments for item in fragment[field]] == route[field]
    for batch in result["batches"]:
        assert len(json.dumps(batch, ensure_ascii=False, separators=(",", ":")).encode()) <= 1024


def test_batch_routes_uses_one_fragment_for_small_route() -> None:
    source = [{"document_id": "m001", "chunk_id": "c001", "folder_id": "f",
               "knowledge_items": [{"id": "c001:rule001", "name": "Rule"}]}]
    result = transform_json(kind="batch_routes", value=source)
    assert len(result["batches"]) == 1
    assert result["batches"][0]["items"][0]["knowledge_items"] == source[0]["knowledge_items"]


def test_batch_routes_rejects_indivisible_item() -> None:
    source = [{"document_id": "m001", "chunk_id": "c001", "folder_id": "f",
               "knowledge_items": [{"description": "x" * 2000}]}]
    with pytest.raises(ValueError, match="Ein einzelner Eintrag"):
        transform_json(kind="batch_routes", value=source, max_batch_bytes=1024)


def test_batch_routes_rejects_oversized_metadata() -> None:
    source = [{"document_id": "m001", "chunk_id": "c001", "folder_id": "f",
               "custom": "x" * 2000, "knowledge_items": []}]
    with pytest.raises(ValueError, match="Routing-Metadaten"):
        transform_json(kind="batch_routes", value=source, max_batch_bytes=1024)


def test_batch_routes_rejects_malformed_records() -> None:
    with pytest.raises(ValueError, match="Routing-Objekte"):
        transform_json(kind="batch_routes", value=[2])
    with pytest.raises(ValueError, match="knowledge_items"):
        transform_json(kind="batch_routes", value=[
            {"document_id": "m001", "chunk_id": "c001", "folder_id": "f",
             "knowledge_items": {}}
        ])


def test_batch_routes_enforces_fragment_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import cli_agent.flow_json_operations as module

    monkeypatch.setattr(module, "MAX_ROUTE_FRAGMENTS", 2)
    source = [{"document_id": "m001", "chunk_id": "c001", "folder_id": "f",
               "knowledge_items": [
                   {"id": str(i), "description": "x" * 600} for i in range(4)
               ]}]
    with pytest.raises(ValueError, match="Fragment-Anzahllimit"):
        transform_json(kind="batch_routes", value=source, max_batch_bytes=1024)
