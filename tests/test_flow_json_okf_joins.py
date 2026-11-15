"""Coverage-safe joins used by the separated OKF document Flow."""
import json

import pytest
from cli_agent.workflows.flow_json_operations import _json_dumps_preserving_numbers

from cli_agent.workflows.flow_json_okf_joins import (
    group_consolidated_knowledge,
    validate_theme_registry,
)


def _scan(sid: str, start: int, end: int, key: str, item_id: str) -> dict:
    return {
        "status": "ok", "document_id": "doc-a",
        "folder_id": "doc-a", "source_path": "doctrines/a.md",
        "section_id": sid, "chunk_id": sid,
        "start_line": start, "end_line": end,
        "candidate_concepts": [{"topic_key": key}],
        "knowledge_items": [{
            "id": f"{sid}:{item_id}", "topic_key": key, "kind": "rule",
            "name": item_id, "description": f"Source {item_id}",
            "normative": True,
            "evidence_ranges": [{
                "doctrine": "doctrines/a.md",
                "start_line": start, "end_line": min(end, start + 1),
                "reason": "source text",
            }],
        }],
        "warnings": [],
    }


def _inputs():
    sections = {"source_path": "doctrines/a.md", "mode": "sections", "total_lines": 20,
                "sections": [
                    {"id": "s001", "start_line": 1, "end_line": 10},
                    {"id": "s002", "start_line": 11, "end_line": 20},
                ]}
    scans = [
        _scan("s001", 1, 10, "approval", "rule001"),
        _scan("s002", 11, 20, "exceptions", "rule001"),
    ]
    consolidated = {
        "status": "ok", "source_path": "doctrines/a.md",
        "folder": {"id": "doc-a", "path": "doctrines/doc-a", "title": "A"},
        "folders": [],
        "concepts": [{
            "id": "approval", "path": "doctrines/doc-a/approval.md",
            "title": "Approval", "description": "Rules and exceptions",
            "type": "concept", "tags": [],
            "merged_from": ["approval", "exceptions"],
        }],
    }
    return sections, scans, consolidated


def _group(sections, scans, consolidated):
    return group_consolidated_knowledge(
        sections=sections, scans=scans, consolidated=consolidated,
        max_concept_bytes=12000, serializer=_json_dumps_preserving_numbers,
    )


def test_section_merge_preserves_all_source_items_and_evidence():
    sections, scans, consolidated = _inputs()
    result = _group(sections, scans, consolidated)
    assert len(result["concepts"]) == 1
    concept = result["concepts"][0]
    assert concept["path"] == "doctrines/doc-a/approval.md"
    assert {x["id"] for x in concept["coverage_items"]} == {
        "doc-a:s001:rule001", "doc-a:s002:rule001"
    }
    assert {(e["start_line"], e["end_line"]) for e in concept["evidence_ranges"]} == {
        (1, 2), (11, 12)
    }


def test_missing_and_duplicate_scans_fail_closed():
    sections, scans, consolidated = _inputs()
    with pytest.raises(ValueError, match="Missing section scans"):
        _group(sections, scans[:1], consolidated)
    with pytest.raises(ValueError, match="duplicate scanned section"):
        _group(sections, [*scans, scans[0]], consolidated)


def test_unknown_topic_key_or_line_escape_fails_closed():
    sections, scans, consolidated = _inputs()
    scans[1]["knowledge_items"][0]["topic_key"] = "not-in-plan"
    with pytest.raises(ValueError, match="unknown local topic_key"):
        _group(sections, scans, consolidated)
    sections, scans, consolidated = _inputs()
    scans[1]["knowledge_items"][0]["evidence_ranges"][0]["start_line"] = 1
    with pytest.raises(ValueError, match="invalid source range"):
        _group(sections, scans, consolidated)


def _registry_batch():
    return {
        "id": "b0001", "items": [
            {"status": "ok", "groups": [
                {"topic": "authorization", "members": [
                    {"source_slug": "a", "path": "doctrines/a/x.md"},
                    {"source_slug": "b", "path": "doctrines/b/y.md"},
                ]},
                {"topic": "alone", "members": [
                    {"source_slug": "a", "path": "doctrines/a/z.md"},
                ]},
            ], "warnings": []}
        ],
    }


def _registry_result():
    return {
        "status": "ok", "themes": [
            {"id": "b0001-auth", "path": "themen/b0001-auth.md",
             "title": "Authorization", "description": "Shared knowledge",
             "group_refs": ["i0001-g0001"],
             "members": [
                 {"source_slug": "a", "path": "doctrines/a/x.md"},
                 {"source_slug": "b", "path": "doctrines/b/y.md"},
             ]},
        ],
        "excluded_group_refs": ["i0001-g0002"], "warnings": [],
    }


def test_registry_requires_every_eligible_group_and_excluded_singleton():
    batch, result = _registry_batch(), _registry_result()
    check = validate_theme_registry(batch=batch, registry=result)
    assert check["status"] == "ok"
    assert check["input_group_count"] == 2
    assert check["eligible_group_count"] == 1
    result["themes"] = []
    with pytest.raises(ValueError, match="Registry missing input groups"):
        validate_theme_registry(batch=batch, registry=result)


def test_registry_does_not_silently_omit_a_member_or_exclude_eligible():
    batch, result = _registry_batch(), _registry_result()
    result["themes"][0]["members"].pop()
    with pytest.raises(ValueError, match="omitted or invented"):
        validate_theme_registry(batch=batch, registry=result)
    batch, result = _registry_batch(), _registry_result()
    result["excluded_group_refs"].append("i0001-g0001")
    with pytest.raises(ValueError):
        validate_theme_registry(batch=batch, registry=result)


def test_registry_never_overrides_incomplete_upstream_status():
    batch, result = _registry_batch(), _registry_result()
    batch["items"][0]["status"] = "needs_attention"
    with pytest.raises(ValueError, match="converted upstream"):
        validate_theme_registry(batch=batch, registry=result)
    result["status"] = "needs_attention"
    assert validate_theme_registry(batch=batch, registry=result)["status"] == "needs_attention"

def test_direct_tasks_and_outline_are_deterministic():
    from cli_agent.workflows.flow_json_okf_joins import (
        prepare_okf_scan_tasks, assemble_okf_section_outline,
    )
    metadata = {"source_path": "doctrines/a.md", "mode": "direct", "total_lines": 12}
    assert prepare_okf_scan_tasks(metadata)["tasks"] == [
        {"id": "direct", "start_line": 1, "end_line": 12}
    ]
    scan = _scan("direct", 1, 12, "direct-t001", "rule001")
    scan["candidate_concepts"] = [{
        "topic_key": "direct-t001", "name": "Approval", "description": "Approval policies"
    }]
    scan["scan_checks"] = {"normative_sweep": "completed", "descriptive_sweep": "completed"}
    outline = assemble_okf_section_outline(
        sections=metadata, scans=[scan],
        source_path="doctrines/a.md", source_slug="doc-a",
    )
    assert outline["concepts"][0]["id"] == "direct-t001"
    assert outline["concepts"][0]["section_ids"] == []


def test_outline_rejects_incomplete_normative_sweep_and_missing_sections():
    from cli_agent.workflows.flow_json_okf_joins import assemble_okf_section_outline
    sections, scans, _ = _inputs()
    for scan in scans:
        scan["candidate_concepts"] = [{
            "topic_key": scan["candidate_concepts"][0]["topic_key"],
            "name": "Rules", "description": "Source rules",
        }]
        scan["scan_checks"] = {"normative_sweep": "completed", "descriptive_sweep": "completed"}
    with pytest.raises(ValueError, match="Incomplete scans"):
        assemble_okf_section_outline(
            sections=sections, scans=scans[:1],
            source_path="doctrines/a.md", source_slug="doc-a",
        )
    scans[0]["scan_checks"]["normative_sweep"] = "failed"
    with pytest.raises(ValueError, match="lacks a complete knowledge sweep"):
        assemble_okf_section_outline(
            sections=sections, scans=scans,
            source_path="doctrines/a.md", source_slug="doc-a",
        )


def test_batch_arrays_caps_number_of_complete_entries_without_dropping_anything():
    from cli_agent.workflows.flow_json_operations import transform_json
    groups = [{"topic": f"g{i}", "members": ["a","b"]} for i in range(23)]
    result = transform_json(
        kind="batch_arrays", value=[{"status":"ok","groups":groups,"warnings":[]}],
        array_paths=["/groups", "/warnings"], batch_size=1,
        target_fragment_bytes=3000, max_batch_bytes=8000, max_fragment_items=5,
    )
    entries = [
        group for batch in result["batches"]
        for fragment in batch["items"]
        for group in fragment["groups"]
    ]
    assert entries == groups
    assert all(
        len(fragment["groups"]) <= 5
        for batch in result["batches"] for fragment in batch["items"]
    )
