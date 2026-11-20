"""Preconsolidation candidates must not be confused with publishable concepts."""
import pytest

from cli_agent.workflows.flow_json_validation import (
    MAX_DOCTRINE_CONCEPTS,
    MAX_PRECONSOLIDATION_CANDIDATES,
    _validate_doctrine,
    assemble_section_outlines,
    prepare_doctrine_consolidation_batches,
)


def make_plan(count: int) -> dict:
    return {
        "status": "ok",
        "folder": {"id": "example", "path": "doctrines/example"},
        "folders": [],
        "source_path": "doctrines/example.md",
        "concepts": [
            {"id": f"s{i // 8 + 1:03d}-t{i % 8 + 1:03d}",
             "path": f"doctrines/example/s{i // 8 + 1:03d}-t{i % 8 + 1:03d}.md",
             "title": f"Topic {i}", "description": "Recorded subject", "tags": []}
            for i in range(count)
        ],
    }


def test_preconsolidation_accepts_1450_but_publish_limit_stays_1000():
    plan = make_plan(1450)
    with pytest.raises(ValueError, match="exceeds 1000"):
        _validate_doctrine(plan)
    _validate_doctrine(plan, max_concepts=MAX_PRECONSOLIDATION_CANDIDATES)
    assert MAX_DOCTRINE_CONCEPTS == 1000
    assert MAX_PRECONSOLIDATION_CANDIDATES == 1500


def test_section_outline_assembly_accepts_1450_candidates():
    tasks = {"tasks": [
        {"id": f"s{i:03d}", "direct": False} for i in range(1, 223)
    ]}
    iterations = {"iterations": []}
    for i in range(1, 223):
        count = 7 if i <= 118 else 6  # 118*7 + 104*6 = 1450
        iterations["iterations"].append({
            "id": f"s{i:03d}",
            "output": {
                "status": "ok", "section_id": f"s{i:03d}",
                "concepts": [
                    {"topic_key": f"s{i:03d}-t{j:03d}",
                     "title": f"Section {i} topic {j}",
                     "description": "Meaningful topic"}
                    for j in range(1, count + 1)
                ],
            },
        })
    result = assemble_section_outlines(
        tasks, iterations, source_path="doctrines/example.md",
        source_slug="example",
    )
    assert len(result["concepts"]) == 1450


def test_preconsolidation_has_bounded_batches():
    plan = make_plan(1450)
    taxonomy = {"status": "ok", "folders": [
        {"id": "a", "title": "A", "description": "Group A"},
        {"id": "b", "title": "B", "description": "Group B"},
    ]}
    result = prepare_doctrine_consolidation_batches(plan, taxonomy, batch_size=6)
    assert len(result["batches"]) == 242
    assert result["candidate_count"] == 1450


def test_more_than_1500_candidates_fails_closed():
    with pytest.raises(ValueError, match="exceeds 1500"):
        _validate_doctrine(
            make_plan(1501),
            max_concepts=MAX_PRECONSOLIDATION_CANDIDATES,
        )
