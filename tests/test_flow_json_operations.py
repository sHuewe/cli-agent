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



def test_batch_topic_profiles_splits_large_merge_without_losing_evidence():
    from cli_agent.workflows.flow import _json_dumps_preserving_numbers

    original = {
        "status": "needs_attention", "source_path": "doctrines/ä.md",
        "topics": [
            {"key": f"t{i:02d}", "title": "Übersicht", "summary": "ä" * 700,
             "keywords": ["alpha", "beta"],
             "evidence": [{"path": "doctrines/ä.md",
                           "start_line": i + 1, "end_line": i + 2,
                           "claim": "proof-" + "é" * 130}]}
            for i in range(10)
        ],
        "warnings": ["warning-" + ("é" * 550), "incomplete scope"],
        "extra_provenance": {"origin": "b0005"},
    }
    result = transform_json(
        kind="batch_topic_profiles", value=[original], batch_size=4,
        max_batch_bytes=4400, target_fragment_bytes=1900,
        serializer=_json_dumps_preserving_numbers,
    )
    fragments = [v for b in result["batches"] for v in b["items"]]
    assert len(fragments) > 1
    assert all(f["status"] == "needs_attention" for f in fragments)
    assert all(f["source_path"] == original["source_path"] for f in fragments)
    assert all(f["extra_provenance"] == original["extra_provenance"] for f in fragments)
    assert [t for f in fragments for t in f["topics"]] == original["topics"]
    assert [w for f in fragments for w in f["warnings"]] == original["warnings"]
    assert result["total_items"] == len(fragments)
    assert all(
        len(_json_dumps_preserving_numbers(b).encode("utf-8")) <= 4400
        for b in result["batches"]
    )


def test_batch_topic_profiles_preserves_empty_and_ok_profiles():
    profiles = [
        {"status": "ok", "source_path": "doc-a", "topics": [], "warnings": []},
        {"status": "ok", "source_path": "doc-b",
         "topics": [{"key": "x", "evidence": []}], "warnings": []},
    ]
    result = transform_json(
        kind="batch_topic_profiles", value=profiles, max_batch_bytes=1024,
        target_fragment_bytes=1024,
    )
    flat = [x for group in result["batches"] for x in group["items"]]
    assert flat == profiles


def test_batch_topic_profiles_fails_on_indivisible_topic():
    profile = {
        "status": "ok", "source_path": "doc",
        "topics": [{"evidence": [{"claim": "x" * 3000}]}],
        "warnings": [],
    }
    with pytest.raises(ValueError, match=r"indivisible topics\[0\]"):
        transform_json(
            kind="batch_topic_profiles", value=[profile],
            max_batch_bytes=1024, target_fragment_bytes=1024,
        )


def test_batch_topic_profiles_fails_on_indivisible_warning():
    profile = {
        "status": "needs_attention", "source_path": "doc",
        "topics": [], "warnings": ["x" * 3000],
    }
    with pytest.raises(ValueError, match=r"indivisible warnings\[0\]"):
        transform_json(
            kind="batch_topic_profiles", value=[profile],
            max_batch_bytes=1024,
        )


@pytest.mark.parametrize("profile", [
    {"status": "unknown", "source_path": "doc", "topics": [], "warnings": []},
    {"status": "ok", "source_path": "doc", "topics": "invalid", "warnings": []},
    {"status": "ok", "source_path": "doc", "topics": [], "warnings": None},
    {"status": "ok", "source_path": "", "topics": [], "warnings": []},
    {"status": "ok", "source_path": "doc", "topics": [None], "warnings": []},
])
def test_batch_topic_profiles_rejects_bad_record(profile):
    with pytest.raises(ValueError, match="batch_topic_profiles"):
        transform_json(kind="batch_topic_profiles", value=[profile])


def test_topic_batch_flow_step_requires_legal_fragment_limits(tmp_path: Path) -> None:
    (tmp_path / "sample.toml").write_text(
        'version = 1\n[[steps]]\nid = "pack"\noutput = "batch.json"\n'
        '[steps.json_operation]\nkind = "batch_topic_profiles"\n'
        'input = "merges"\nbatch_size = 4\nmax_batch_bytes = 30000\n'
        'target_fragment_bytes = 12000\n'
        '[steps.json_inputs.merges]\npath = "merged.json"\n',
        encoding="utf-8",
    )
    flow = load_flow(Path("sample.toml"), workspace=tmp_path)
    assert flow.steps[0].json_operation["target_fragment_bytes"] == 12000
    file = tmp_path / "sample.toml"
    content = file.read_text()
    file.write_text(content.replace(
        "target_fragment_bytes = 12000", "target_fragment_bytes = 30001"
    ))
    with pytest.raises(ValueError, match="target_fragment_bytes"):
        load_flow(Path("sample.toml"), workspace=tmp_path)


def test_batch_topic_profiles_reuses_upstream_completed_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "source.md").write_text("The model must not run", encoding="utf-8")
    source = {
        "status": "ok", "source_path": "doctrines/doc.md",
        "topics": [{"key": "x", "summary": "x" * 620,
                    "evidence": [{"path": "doctrines/doc.md",
                                  "start_line": 1, "end_line": 2,
                                  "claim": "observed"}]}
                   for _ in range(4)],
        "warnings": [],
    }
    (tmp_path / "source.json").write_text(json.dumps(source), encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        'version = 1\n'
        '[[steps]]\nid = "source"\nprompt_file = "source.md"\n'
        'output = "source.json"\nresponse_format = "json"\n'
        '[[steps]]\nid = "pack"\noutput = "batch.json"\n'
        '[steps.json_operation]\nkind = "batch_topic_profiles"\ninput = "merges"\n'
        'batch_size = 3\nmax_batch_bytes = 1800\n'
        'target_fragment_bytes = 1100\n'
        '[steps.json_inputs.merges]\npath = "source.json"\n',
        encoding="utf-8",
    )
    flow = load_flow(Path("flow.toml"), workspace=tmp_path)

    from cli_agent.workflows import flow as flow_module

    async def no_model(*args: object, **kwargs: object) -> None:
        raise AssertionError("Should resume source checkpoint; no LLM needed")

    monkeypatch.setattr(flow_module, "run_once", no_model)
    asyncio.run(run_flow(flow, workspace=tmp_path))
    result = json.loads((tmp_path / "batch.json").read_text())
    fragments = [x for group in result["batches"] for x in group["items"]]
    assert [t for f in fragments for t in f["topics"]] == source["topics"]
    asyncio.run(run_flow(flow, workspace=tmp_path))
