from __future__ import annotations

import json

import pytest

from cli_agent.flow_json_grouping import group_assigned_items, verify_assigned_coverage


def dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def source_fixture() -> tuple[list[dict], list[dict], dict]:
    routes = [
        {
            "document_id": "m001",
            "chunk_id": "c001",
            "folder_id": "domain",
            "knowledge_items": [
                {
                    "id": "c001:rule001",
                    "kind": "rule",
                    "name": "Requirement",
                    "description": "An explicit requirement",
                    "normative": True,
                    "evidence_ranges": [
                        {"doctrine": "docs/example.md", "start_line": 1, "end_line": 2}
                    ],
                },
                {
                    "id": "c001:fact001",
                    "kind": "fact",
                    "name": "Explanation",
                    "description": "A descriptive explanation",
                    "normative": False,
                    "evidence_ranges": [
                        {"doctrine": "docs/example.md", "start_line": 3, "end_line": 4}
                    ],
                },
            ],
        }
    ]
    assignments = [{"batch_id": "b0001", "assignments": [
        {"item_id": "m001:c001:rule001", "topic_id": "t001"},
        {"item_id": "m001:c001:fact001", "topic_id": "t001"},
    ]}]
    topics = {
        "folder": {"id": "domain", "path": "domain", "title": "Domain"},
        "topics": [{
            "id": "t001",
            "path": "domain/requirements.md",
            "type": "concept",
            "title": "Requirements",
            "description": "Constraints and meanings",
            "tags": ["domain"],
        }],
    }
    return routes, assignments, topics


def build(routes: list[dict], assignments: list[dict], topics: dict,
          max_bytes: int = 60000) -> dict:
    return group_assigned_items(
        routes=routes, assignments=assignments, topic_plan=topics,
        max_concept_bytes=max_bytes, serializer=dump,
    )


def test_group_combines_across_batches_and_keeps_original_evidence() -> None:
    routes, assignments, topics = source_fixture()
    assignments = [
        {"batch_id": "b0001", "assignments": assignments[0]["assignments"][:1]},
        {"batch_id": "b0002", "assignments": assignments[0]["assignments"][1:]},
    ]
    result = build(routes, assignments, topics)
    assert result["total_items"] == 2
    assert len(result["concepts"]) == 1
    concept = result["concepts"][0]
    assert [item["id"] for item in concept["coverage_items"]] == [
        "m001:c001:rule001", "m001:c001:fact001",
    ]
    assert len(concept["evidence_ranges"]) == 2
    assert result["concept_summaries"][0]["path"] == "domain/requirements.md"


@pytest.mark.parametrize("mode,match", [
    ("missing", "ohne Topic-Zuweisung"),
    ("duplicate", "mehrfach zugewiesen"),
    ("unknown", "Unbekanntes Knowledge Item"),
    ("bad_topic", "Unbekannte globale Topic-ID"),
])
def test_group_rejects_invalid_assignment(mode: str, match: str) -> None:
    routes, assignments, topics = source_fixture()
    if mode == "missing":
        assignments[0]["assignments"].pop()
    elif mode == "duplicate":
        assignments[0]["assignments"].append(assignments[0]["assignments"][0])
    elif mode == "unknown":
        assignments[0]["assignments"][0]["item_id"] = "m009:unknown"
    else:
        assignments[0]["assignments"][0]["topic_id"] = "t999"
    with pytest.raises(ValueError, match=match):
        build(routes, assignments, topics)


def test_group_splits_oversized_topic_at_item_boundary() -> None:
    routes, assignments, topics = source_fixture()
    routes[0]["knowledge_items"][0]["description"] = "A" * 1300
    routes[0]["knowledge_items"][1]["description"] = "B" * 1300
    result = build(routes, assignments, topics, max_bytes=1900)
    assert len(result["concepts"]) == 2
    assert result["concepts"][0]["id"] == "t001"
    assert result["concepts"][1]["id"] == "t001-p002"
    assert result["concepts"][1]["path"] == "domain/requirements-part-2.md"
    assert all(len(dump(concept).encode()) <= 1900 for concept in result["concepts"])


def test_group_rejects_individually_oversized_item() -> None:
    routes, assignments, topics = source_fixture()
    routes[0]["knowledge_items"][0]["description"] = "A" * 3000
    with pytest.raises(ValueError, match="max_concept_bytes"):
        build(routes, assignments, topics, max_bytes=1900)


def test_coverage_checks_actual_repair_results() -> None:
    routes, assignments, topics = source_fixture()
    plan = build(routes, assignments, topics)
    ok = verify_assigned_coverage(plan=plan, results=[
        {
            "concept_id": "t001", "status": "repaired",
            "documented_item_ids": ["m001:c001:rule001", "m001:c001:fact001"],
            "remaining_item_ids": [], "remaining_warnings": [],
        }
    ])
    assert ok["status"] == "ok"
    assert ok["documented_normative_items"] == 1
    assert ok["documented_descriptive_items"] == 1

    failed = verify_assigned_coverage(plan=plan, results=[
        {
            "concept_id": "t001", "status": "repaired",
            "documented_item_ids": ["m001:c001:rule001"],
            "remaining_item_ids": [], "remaining_warnings": [],
        }
    ])
    assert failed["status"] == "needs_attention"
    assert failed["missing_item_ids"] == ["m001:c001:fact001"]
