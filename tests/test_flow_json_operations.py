from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from cli_agent.workflows.flow import load_flow, run_flow, validate_flow
from cli_agent.workflows.flow_json_operations import transform_json


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
    with pytest.raises(ValueError, match="flatten expects only lists\\."):
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
    from cli_agent.workflows import flow as flow_module

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
    import cli_agent.workflows.flow_json_operations as module

    with pytest.raises(ValueError, match="batch exceeds the batch count limit\\."):
        module.transform_json(
            kind="batch",
            value=list(range(module.MAX_BATCHES + 1)),
            batch_size=1,
        )


def test_batch_result_limit_is_enforced_incrementally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli_agent.workflows.flow_json_operations as module

    monkeypatch.setattr(module, "MAX_RESULT_BYTES", 200)
    with pytest.raises(ValueError, match="batch exceeds the JSON result limit\\."):
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
    from cli_agent.workflows.flow import _JsonNumber, _json_dumps_preserving_numbers

    source = [_JsonNumber("1e-200")] * 5
    result = transform_json(
        kind="batch", value=source, max_batch_bytes=1024,
        serializer=_json_dumps_preserving_numbers,
    )
    assert len(result["batches"]) == 1
    serialized = _json_dumps_preserving_numbers(result)
    assert '"items":[1e-200,1e-200' in serialized


def test_batch_utf8_boundary_accounts_for_id_and_payload() -> None:
    from cli_agent.workflows.flow import _json_dumps_preserving_numbers

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
    with pytest.raises(ValueError, match="Routing metadata for route 0 exceed max_batch_bytes\\."):
        transform_json(kind="batch_routes", value=source, max_batch_bytes=1024)


def test_batch_routes_rejects_malformed_records() -> None:
    with pytest.raises(ValueError, match="batch_routes expects routing objects; entry 0 is invalid\\."):
        transform_json(kind="batch_routes", value=[2])
    with pytest.raises(ValueError, match="knowledge_items"):
        transform_json(kind="batch_routes", value=[
            {"document_id": "m001", "chunk_id": "c001", "folder_id": "f",
             "knowledge_items": {}}
        ])


def test_batch_routes_enforces_fragment_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import cli_agent.workflows.flow_json_operations as module

    monkeypatch.setattr(module, "MAX_ROUTE_FRAGMENTS", 2)
    source = [{"document_id": "m001", "chunk_id": "c001", "folder_id": "f",
               "knowledge_items": [
                   {"id": str(i), "description": "x" * 600} for i in range(4)
               ]}]
    with pytest.raises(ValueError, match="batch_routes exceeds the fragment count limit\\."):
        transform_json(kind="batch_routes", value=source, max_batch_bytes=1024)




def test_batch_arrays_is_schema_independent_and_lossless():
    from cli_agent.workflows.flow import _json_dumps_preserving_numbers
    original = {
        "id": "record-1",
        "context": {"origin": "agent", "trace": "é"},
        "data": {
            "rows": [{"value": i, "note": "ä" * 350} for i in range(10)],
        },
        "errors": [{"reason": "x" * 300}, {"reason": "y" * 500}],
    }
    result = transform_json(
        kind="batch_arrays", value=[original],
        array_paths=["/data/rows", "/errors"], batch_size=4,
        max_batch_bytes=3000, target_fragment_bytes=1100,
        serializer=_json_dumps_preserving_numbers,
    )
    fragments = [f for batch in result["batches"] for f in batch["items"]]
    assert len(fragments) > 1
    assert all(f["id"] == "record-1" for f in fragments)
    assert all(f["context"] == original["context"] for f in fragments)
    assert [row for f in fragments for row in f["data"]["rows"]] == original["data"]["rows"]
    assert [e for f in fragments for e in f["errors"]] == original["errors"]
    assert all(
        len(_json_dumps_preserving_numbers(batch).encode("utf-8")) <= 3000
        for batch in result["batches"]
    )
    assert original["data"]["rows"][0]["note"] == "ä" * 350


def test_batch_arrays_supports_primitive_array_members_and_empty_arrays():
    data = [
        {"meta": 123, "items": [1, True, None, ["nested"], {"x": 1}],
         "empty": []},
        {"meta": 456, "items": [], "empty": []},
    ]
    result = transform_json(
        kind="batch_arrays", value=data,
        array_paths=["/items", "/empty"], max_batch_bytes=1024,
    )
    fragments = [f for batch in result["batches"] for f in batch["items"]]
    assert fragments == data


def test_batch_arrays_handles_json_pointer_escaping():
    data = [{"meta": "ok", "nested": {"a/b": {"~list": list(range(20))}}}]
    result = transform_json(
        kind="batch_arrays", value=data, array_paths=["/nested/a~1b/~0list"],
        max_batch_bytes=1024, target_fragment_bytes=1024,
    )
    fragments = [f for batch in result["batches"] for f in batch["items"]]
    assert [i for f in fragments for i in f["nested"]["a/b"]["~list"]] == list(range(20))


def test_batch_arrays_rejects_oversized_atomic_value_and_metadata():
    with pytest.raises(ValueError, match="indivisible array entry"):
        transform_json(
            kind="batch_arrays", value=[{"items": ["x" * 2000]}],
            array_paths=["/items"], max_batch_bytes=1024,
        )
    with pytest.raises(ValueError, match="non-selected fields"):
        transform_json(
            kind="batch_arrays", value=[{"meta": "x" * 2000, "items": []}],
            array_paths=["/items"], max_batch_bytes=1024,
        )


@pytest.mark.parametrize("value,paths,pattern", [
    ([{"items": []}], [], "array_paths"),
    ([{"items": []}], ["/items", "/items"], "duplicate"),
    ([{"items": []}], ["/items", "/items/sub"], "overlap"),
    ([{"items": []}], ["items"], "start"),
    ([{"items": []}], ["/bad~3escape"], "invalid"),
    ([{"items": None}], ["/items"], "expects an array"),
    ([{"items": []}], ["/unknown"], "has no object array"),
    ([4], ["/items"], "JSON object"),
])
def test_batch_arrays_rejects_bad_input(value, paths, pattern):
    with pytest.raises(ValueError, match=pattern):
        transform_json(kind="batch_arrays", value=value,
                       array_paths=paths, max_batch_bytes=1024)


def test_batch_arrays_flow_parses_paths_and_limits(tmp_path: Path):
    path = tmp_path / "flow.toml"
    original = (
        'version = 1\n[[steps]]\nid = "pack"\noutput = "packed.json"\n'
        '[steps.json_operation]\nkind = "batch_arrays"\n'
        'input = "merges"\narray_paths = ["/topics", "/warnings"]\n'
        'batch_size = 4\nmax_batch_bytes = 30000\n'
        'target_fragment_bytes = 12000\n'
        '[steps.json_inputs.merges]\npath = "merged.json"\n'
    )
    path.write_text(original, encoding="utf-8")
    flow = load_flow(path, workspace=tmp_path)
    assert flow.steps[0].json_operation["array_paths"] == ["/topics", "/warnings"]
    path.write_text(original.replace("target_fragment_bytes = 12000",
                                    "target_fragment_bytes = 30001"))
    with pytest.raises(ValueError, match="target_fragment_bytes"):
        load_flow(path, workspace=tmp_path)
    path.write_text(original.replace("array_paths = [\"/topics\", \"/warnings\"]",
                                    'array_paths = ["/topics", "/topics"]'))
    with pytest.raises(ValueError, match="duplicate"):
        load_flow(path, workspace=tmp_path)


def test_batch_arrays_preserves_old_checkpoint_without_llm(tmp_path, monkeypatch):
    (tmp_path / "source.md").write_text("Not executed", encoding="utf-8")
    original = {"status": "needs_attention", "source_path": "doc.md",
                "topics": [{"id": i, "text": "x" * 590} for i in range(4)],
                "warnings": ["unresolved"] }
    (tmp_path / "source.json").write_text(json.dumps([original]), encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        'version = 1\n'
        '[[steps]]\nid = "source"\nprompt_file = "source.md"\n'
        'output = "source.json"\nresponse_format = "json"\n'
        '[[steps]]\nid = "pack"\noutput = "batch.json"\n'
        '[steps.json_operation]\nkind = "batch_arrays"\ninput = "merges"\n'
        'array_paths = ["/topics", "/warnings"]\n'
        'batch_size = 3\nmax_batch_bytes = 1800\n'
        'target_fragment_bytes = 1100\n'
        '[steps.json_inputs.merges]\npath = "source.json"\n',
        encoding="utf-8",
    )
    flow = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    import cli_agent.workflows.flow as fm

    async def fail_model(*args, **kwargs):
        raise AssertionError("A checkpoint should be reused")

    monkeypatch.setattr(fm, "run_once", fail_model)
    asyncio.run(run_flow(flow, workspace=tmp_path))
    result = json.loads((tmp_path / "batch.json").read_text())
    frags = [f for b in result["batches"] for f in b["items"]]
    assert all(f["status"] == "needs_attention" for f in frags)
    assert [t for f in frags for t in f["topics"]] == original["topics"]
    assert [w for f in frags for w in f["warnings"]] == original["warnings"]
    asyncio.run(run_flow(flow, workspace=tmp_path))



def test_batch_arrays_splits_at_hard_limit_when_soft_target_is_higher():
    """Regression: a splittable JSON array must not fail as indivisible."""
    from cli_agent.workflows.flow import _json_dumps_preserving_numbers

    original = {"items": ["x" * 500, "y" * 500]}
    result = transform_json(
        kind="batch_arrays", value=[original], array_paths=["/items"],
        max_batch_bytes=1024, target_fragment_bytes=1024,
        serializer=_json_dumps_preserving_numbers,
    )
    fragments = [fragment for batch in result["batches"]
                 for fragment in batch["items"]]
    assert len(fragments) == 2
    assert [entry for f in fragments for entry in f["items"]] == original["items"]
    assert all(len(_json_dumps_preserving_numbers(batch).encode("utf-8")) <= 1024
               for batch in result["batches"])


def test_batch_arrays_fragment_count_can_exceed_packed_batch_count():
    """Regression: 1001 fragments are valid when packed into 201 batches."""
    original = [{"metadata": index, "items": []} for index in range(1001)]
    result = transform_json(
        kind="batch_arrays", value=original, array_paths=["/items"],
        max_batch_bytes=1024, target_fragment_bytes=1024, batch_size=5,
    )
    assert result["total_items"] == 1001
    assert len(result["batches"]) == 201
    assert [fragment for batch in result["batches"]
            for fragment in batch["items"]] == original


def test_batch_arrays_keeps_independent_fragment_resource_guard(monkeypatch):
    import cli_agent.workflows.flow_json_arrays as arrays

    monkeypatch.setattr(arrays, "MAX_ARRAY_FRAGMENTS", 2)
    with pytest.raises(ValueError, match="fragment count limit"):
        transform_json(
            kind="batch_arrays", value=[
                {"items": []}, {"items": []}, {"items": []},
            ], array_paths=["/items"], max_batch_bytes=1024,
        )



def test_batch_arrays_serialization_work_scales_with_array_elements():
    """Regression: never re-serialize every growing prefix of a large array."""
    from cli_agent.workflows.flow import _json_dumps_preserving_numbers

    serialized_bytes = 0

    def counting_serializer(value):
        nonlocal serialized_bytes
        result = _json_dumps_preserving_numbers(value)
        serialized_bytes += len(result.encode("utf-8"))
        return result

    values = list(range(8000))
    result = transform_json(
        kind="batch_arrays",
        value=[{"metadata": {"source": "sample"}, "data": {"rows": values}}],
        array_paths=["/data/rows"],
        batch_size=4,
        max_batch_bytes=100_000,
        target_fragment_bytes=100_000,
        serializer=counting_serializer,
    )
    fragments = [fragment for batch in result["batches"]
                 for fragment in batch["items"]]
    assert [value for fragment in fragments for value in fragment["data"]["rows"]] == values
    assert len(fragments) == 1
    # Incremental element sizing is linear. Re-serializing all prefixes
    # would process many megabytes for this small input.
    assert serialized_bytes < 150_000


def test_batch_arrays_incremental_sizing_matches_exact_utf8_output():
    from cli_agent.workflows.flow import _JsonNumber, _json_dumps_preserving_numbers

    original = {
        "metadata": "ä" * 18,
        "nested": {
            "entries": [
                {"label": "é" * 160, "value": _JsonNumber("1e-200")}
                for _ in range(12)
            ],
            "flags": [True, None, False, "a\\b", "🦋" * 12],
        },
    }
    result = transform_json(
        kind="batch_arrays", value=[original],
        array_paths=["/nested/entries", "/nested/flags"],
        max_batch_bytes=1024, target_fragment_bytes=1024,
        serializer=_json_dumps_preserving_numbers,
    )
    fragments = [fragment for batch in result["batches"]
                 for fragment in batch["items"]]
    assert len(fragments) > 1
    assert [item for fragment in fragments for item in fragment["nested"]["entries"]] == original["nested"]["entries"]
    assert [item for fragment in fragments for item in fragment["nested"]["flags"]] == original["nested"]["flags"]
    for batch in result["batches"]:
        assert len(_json_dumps_preserving_numbers(batch).encode("utf-8")) <= 1024


def test_deterministic_only_flow_never_loads_model_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """JSON-only flows work even with a broken or absent default config."""
    from cli_agent.workflows import flow as flow_module

    (tmp_path / "source.json").write_text(
        json.dumps(["alpha", "beta"]), encoding="utf-8",
    )
    (tmp_path / "flow.toml").write_text(
        'version = 1\n'
        '[[steps]]\nid = "batch"\noutput = "batches.json"\n'
        '[steps.json_operation]\nkind = "batch"\ninput = "source"\n'
        'batch_size = 1\n'
        '[steps.json_inputs.source]\npath = "source.json"\n',
        encoding="utf-8",
    )
    flow = load_flow(Path("flow.toml"), workspace=tmp_path)

    def unexpected_config(*args: object, **kwargs: object) -> None:
        raise AssertionError("Deterministic execution must not read model settings")

    def unexpected_telemetry(*args: object, **kwargs: object) -> None:
        raise AssertionError("Deterministic execution must not initialize exporter")

    monkeypatch.setattr(flow_module, "load_config", unexpected_config)
    monkeypatch.setattr(flow_module, "configure_telemetry", unexpected_telemetry)
    dependencies = flow_module.ExecutionDependencies(load_config=unexpected_config)
    asyncio.run(run_flow(flow, workspace=tmp_path, dependencies=dependencies))
    result = json.loads((tmp_path / "batches.json").read_text(encoding="utf-8"))
    assert result["total_items"] == 2


def test_deterministic_only_subflow_does_not_load_model_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model-free child flows do not force the default configuration either."""
    from cli_agent.workflows import flow as flow_module

    (tmp_path / "source.json").write_text(
        json.dumps(["alpha"]), encoding="utf-8",
    )
    (tmp_path / "child.toml").write_text(
        'version = 1\nresult = "steps.batch.output"\n'
        '[[steps]]\nid = "batch"\noutput = "batches.json"\n'
        '[steps.json_operation]\nkind = "batch"\ninput = "source"\n'
        '[steps.json_inputs.source]\npath = "source.json"\n',
        encoding="utf-8",
    )
    (tmp_path / "flow.toml").write_text(
        'version = 1\n[[steps]]\nid = "child"\nflow = "child.toml"\n',
        encoding="utf-8",
    )
    flow = load_flow(Path("flow.toml"), workspace=tmp_path)

    def unexpected_config(*args: object, **kwargs: object) -> None:
        raise AssertionError("Deterministic nested flow must not read model settings")

    monkeypatch.setattr(flow_module, "load_config", unexpected_config)
    dependencies = flow_module.ExecutionDependencies(load_config=unexpected_config)
    asyncio.run(run_flow(flow, workspace=tmp_path, dependencies=dependencies))
    result = json.loads((tmp_path / "batches.json").read_text(encoding="utf-8"))
    assert result["total_items"] == 1
