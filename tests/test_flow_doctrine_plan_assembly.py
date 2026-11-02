from __future__ import annotations
import pytest
from cli_agent.workflows.flow_json_validation import (
    prepare_doctrine_concept_batches, assemble_doctrine_concept_plan,
)


def outline():
    base = "doctrines/doc-123"
    return {
        "status": "ok", "source_path": "sources/doc.md",
        "folder": {"id": "doc-123", "path": base, "title": "Doc"},
        "folders": [], "summary": "Summary",
        "concepts": [
            {"id": x, "path": f"{base}/{x}.md", "title": x,
             "description": x, "type": "concept", "tags": ["topic"],
             "section_ids": ["s001"] if x != "c" else ["s002"]}
            for x in ("a", "b", "c")
        ],
    }


def analyses():
    return {"iterations": [
        {"id": sid, "output": {
            "status": "ok", "section_id": sid,
            "source_path": "sources/doc.md",
            "start_line": start, "end_line": end,
            "summary": f"Range {sid}", "topics": [],
        }} for sid, start, end in
        (("s001", 1, 20), ("s002", 21, 40))
    ]}


def test_batched_plan_preserves_global_concepts_and_evidence():
    plan = outline()
    batches = prepare_doctrine_concept_batches(plan, analyses(), batch_size=2)
    assert [len(b["concepts"]) for b in batches["batches"]] == [2, 1]
    assert [r["section_id"] for r in batches["batches"][0]["sections"]] == ["s001"]
    assert [r["section_id"] for r in batches["batches"][1]["sections"]] == ["s002"]
    results = {"iterations": [
        {"id": batch["id"], "output": {
            "status": "ok",
            "concepts": [
                {"id": c["id"], "evidence_ranges": [
                    {"doctrine": "sources/doc.md", "start_line": 21 if c["id"] == "c" else 1, "end_line": 22 if c["id"] == "c" else 2}
                ]} for c in batch["concepts"]
            ],
        }} for batch in batches["batches"]
    ]}
    merged = assemble_doctrine_concept_plan(plan, results, batches)
    assert [c["id"] for c in merged["concepts"]] == ["a", "b", "c"]
    assert "section_ids" not in merged["concepts"][0]
    assert all(c["evidence_ranges"] for c in merged["concepts"])


def test_missing_or_duplicate_concepts_rejected():
    plan = outline()
    item = {"id": "a", "evidence_ranges": [
        {"doctrine": "sources/doc.md", "start_line": 1, "end_line": 2}
    ]}
    with pytest.raises(ValueError, match="nicht alle"):
        assemble_doctrine_concept_plan(plan, {"iterations": [
            {"id": "b0001", "output": {"status": "ok", "concepts": [item]}}
        ]}, prepare_doctrine_concept_batches(plan, analyses(), batch_size=2))
    with pytest.raises(ValueError, match="mehrfach"):
        assemble_doctrine_concept_plan(plan, {"iterations": [
            {"id": "b0001", "output": {"status": "ok", "concepts": [item, item]}}
        ]}, prepare_doctrine_concept_batches(plan, analyses()))


def test_bad_section_refs_and_source_are_rejected():
    plan = outline()
    plan["concepts"][0]["section_ids"] = ["missing"]
    with pytest.raises(ValueError, match="Abschnittsreferenzen"):
        prepare_doctrine_concept_batches(plan, analyses())
    plan = outline()
    wrong = analyses()
    wrong["iterations"][0]["output"]["source_path"] = "other.md"
    with pytest.raises(ValueError, match="Abschnittsanalyse"):
        prepare_doctrine_concept_batches(plan, wrong)


def test_short_document_empty_sections_is_supported():
    plan = outline()
    for concept in plan["concepts"]:
        concept["section_ids"] = []
    batch = prepare_doctrine_concept_batches(
        plan, {"iterations": []}, batch_size=3,
    )
    assert len(batch["batches"]) == 1
    assert batch["batches"][0]["sections"] == []
