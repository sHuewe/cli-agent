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


def test_split_ids_avoid_reserved_topic_ids_and_paths() -> None:
    routes, assignments, topics = source_fixture()
    routes[0]["knowledge_items"][0]["description"] = "A" * 1300
    routes[0]["knowledge_items"][1]["description"] = "B" * 1300
    topics["topics"].append({
        "id": "t001-p002", "path": "domain/another.md",
        "type": "concept", "title": "Other", "description": "Other topic",
        "tags": [],
    })
    result = build(routes, assignments, topics, max_bytes=1900)
    assert [concept["id"] for concept in result["concepts"]] == [
        "t001", "t001-p003"
    ]


def test_split_paths_avoid_reserved_topic_paths() -> None:
    routes, assignments, topics = source_fixture()
    routes[0]["knowledge_items"][0]["description"] = "A" * 1300
    routes[0]["knowledge_items"][1]["description"] = "B" * 1300
    topics["topics"].append({
        "id": "t002", "path": "domain/requirements-part-2.md",
        "type": "concept", "title": "Other", "description": "Other topic",
        "tags": [],
    })
    result = build(routes, assignments, topics, max_bytes=1900)
    assert result["concepts"][1]["path"] == "domain/requirements-part-3.md"


def test_max_concepts_checked_before_appending_each_part(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli_agent.flow_json_grouping as grouping

    routes, assignments, topics = source_fixture()
    for i in range(10):
        source = dict(routes[0]["knowledge_items"][0])
        source["id"] = f"c001:extra{i:03d}"
        source["description"] = "X" * 1300
        routes[0]["knowledge_items"].append(source)
        assignments[0]["assignments"].append({
            "item_id": f"m001:c001:extra{i:03d}", "topic_id": "t001"
        })
    monkeypatch.setattr(grouping, "MAX_CONCEPTS", 2)
    with pytest.raises(ValueError, match="Zu viele resultierende Konzepte"):
        build(routes, assignments, topics, max_bytes=1900)


def test_group_serialization_is_linear_in_item_count() -> None:
    routes, assignments, topics = source_fixture()
    routes[0]["knowledge_items"] = []
    assignments[0]["assignments"] = []
    for i in range(500):
        routes[0]["knowledge_items"].append({
            "id": f"c001:fact{i:04d}", "kind": "fact",
            "name": "Fact", "description": "A", "normative": False,
            "evidence_ranges": [],
        })
        assignments[0]["assignments"].append({
            "item_id": f"m001:c001:fact{i:04d}", "topic_id": "t001",
        })

    calls = 0

    def counted(value: object) -> str:
        nonlocal calls
        calls += 1
        return dump(value)

    result = group_assigned_items(
        routes=routes, assignments=assignments, topic_plan=topics,
        max_concept_bytes=200_000, serializer=counted,
    )
    assert result["total_items"] == 500
    assert calls < 1500


def test_incremental_sizes_match_exact_json_for_duplicates_and_unicode() -> None:
    routes, assignments, topics = source_fixture()
    routes[0]["knowledge_items"][0]["description"] = "é\u20ac漢"
    routes[0]["knowledge_items"][1]["evidence_ranges"] = (
        routes[0]["knowledge_items"][0]["evidence_ranges"]
    )
    result = build(routes, assignments, topics, max_bytes=1024)
    for concept in result["concepts"]:
        assert len(dump(concept).encode("utf-8")) <= 1024


@pytest.mark.parametrize("folder_path", [
    "doctrines/example-md",
    "doctrines/app-28-edb-v1-03-2024-md",
    "a/b/c",
])
def test_group_accepts_safe_nested_folder_paths(folder_path: str) -> None:
    routes, assignments, topics = source_fixture()
    topics["folder"]["path"] = folder_path
    topics["topics"][0]["path"] = folder_path + "/requirements.md"
    result = build(routes, assignments, topics)
    assert result["folder"]["path"] == folder_path
    assert result["concepts"][0]["path"] == folder_path + "/requirements.md"


@pytest.mark.parametrize("folder_path", [
    "/absolute", "../escape", "a/../escape", "a/./b", "a//b",
    "a/", "a\\\\b", "a/b_c", "a/b.md", "a/UPPER",
])
def test_group_rejects_unsafe_nested_folder_paths(folder_path: str) -> None:
    routes, assignments, topics = source_fixture()
    topics["folder"]["path"] = folder_path
    topics["topics"][0]["path"] = folder_path + "/requirements.md"
    with pytest.raises(ValueError, match="Ungültiger semantischer Ordnerpfad"):
        build(routes, assignments, topics)


def test_group_rejects_topic_path_outside_nested_folder() -> None:
    routes, assignments, topics = source_fixture()
    topics["folder"]["path"] = "doctrines/example"
    topics["topics"][0]["path"] = "doctrines/other/requirements.md"
    with pytest.raises(ValueError, match="Topic-Pfad außerhalb"):
        build(routes, assignments, topics)


@pytest.mark.parametrize("reserved", [
    "con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9",
])
def test_group_rejects_reserved_windows_folder_names(reserved: str) -> None:
    routes, assignments, topics = source_fixture()
    topics["folder"]["path"] = f"doctrines/{reserved}"
    topics["topics"][0]["path"] = f"doctrines/{reserved}/requirements.md"
    with pytest.raises(ValueError, match="Ungültiger semantischer Ordnerpfad"):
        build(routes, assignments, topics)


@pytest.mark.parametrize("reserved", ["con", "nul", "com1", "lpt9"])
def test_group_rejects_reserved_windows_topic_names(reserved: str) -> None:
    routes, assignments, topics = source_fixture()
    topics["folder"]["path"] = "doctrines/example"
    topics["topics"][0]["path"] = f"doctrines/example/{reserved}.md"
    with pytest.raises(ValueError, match="Reservierter Windows-Gerätename"):
        build(routes, assignments, topics)


def test_group_declared_semantic_subfolders():
    routes, assignments, topics = source_fixture()
    topics["folder"]["path"] = "doctrines/example"
    topics["folders"] = [{"id": "permissions", "path": "doctrines/example/permissions", "title": "Permissions"}]
    topics["topics"][0]["path"] = "doctrines/example/permissions/requirements.md"
    result = build(routes, assignments, topics)
    assert result["folders"] == topics["folders"]
    assert result["concepts"][0]["path"] == topics["topics"][0]["path"]


@pytest.mark.parametrize("subfolder", ["../escape", "con", "has_under", "a/b", ""])
def test_group_rejects_bad_semantic_subfolders(subfolder):
    routes, assignments, topics = source_fixture()
    topics["folder"]["path"] = "doctrines/example"
    topics["folders"] = [{"id": subfolder, "path": f"doctrines/example/{subfolder}", "title": "Bad"}]
    topics["topics"][0]["path"] = f"doctrines/example/{subfolder}/requirements.md"
    with pytest.raises(ValueError, match="Ungültiger semantischer Unterordner"):
        build(routes, assignments, topics)


def test_group_rejects_undeclared_semantic_topic_folder():
    routes, assignments, topics = source_fixture()
    topics["folder"]["path"] = "doctrines/example"
    topics["folders"] = [{"id": "rights", "path": "doctrines/example/rights", "title": "Rights"}]
    topics["topics"][0]["path"] = "doctrines/example/other/requirements.md"
    with pytest.raises(ValueError, match="Topic-Pfad außerhalb"):
        build(routes, assignments, topics)
