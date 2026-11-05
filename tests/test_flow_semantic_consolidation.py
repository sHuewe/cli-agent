from cli_agent.workflows.flow_json_validation import (
    build_doctrine_taxonomy_profile,
    prepare_doctrine_consolidation_batches,
    assemble_doctrine_consolidation,
)
import pytest


def _outline():
    return {
        "status": "ok", "source_path": "sources/doc.md",
        "folder": {"id": "doc-123", "path": "doctrines/doc-123"},
        "folders": [], "concepts": [
            {"id": k, "path": f"doctrines/doc-123/{k}.md",
             "title": k, "description": "Specific definition and rule",
             "type": "concept", "tags": ["topic"], "section_ids": [s]}
            for k, s in [("approval-rules", "s001"), ("approval-conditions", "s002"),
                          ("operational-flow", "s003"), ("operational-guide", "s003")]
        ]
    }


def _taxonomy():
    return {"status": "ok", "folders": [
        {"id": "governance", "title": "Governance", "description": "Decisions and approval"},
        {"id": "operations", "title": "Operations", "description": "Procedures"},
    ]}


def test_profile_is_compact_and_grouping_complete():
    plan = _outline()
    profile = build_doctrine_taxonomy_profile(plan)
    assert profile["candidate_count"] == 4
    batches = prepare_doctrine_consolidation_batches(plan, _taxonomy(), batch_size=4)
    assert sum(len(b["candidates"]) for b in batches["batches"]) == 4


def test_semantic_grouping_preserves_every_source_and_folders():
    plan = _outline()
    batches = prepare_doctrine_consolidation_batches(plan, _taxonomy(), batch_size=4)
    result = {"iterations": [{
        "id": batches["batches"][0]["id"],
        "output": {"status": "ok", "groups": [
            {"member_ids": ["approval-rules", "approval-conditions"],
             "title": "Approval", "description": "All approval rules and conditions",
             "folder_id": "governance"},
            {"member_ids": ["operational-flow", "operational-guide"],
             "title": "Operational process", "description": "Both workflows",
             "folder_id": "operations"},
        ]},
    }]}
    merged = assemble_doctrine_consolidation(plan, _taxonomy(), batches, result)
    assert len(merged["concepts"]) == 2
    assert len(merged["folders"]) == 2
    assert merged["concepts"][0]["merged_from"] == [
        "approval-rules", "approval-conditions",
    ]
    assert merged["concepts"][0]["section_ids"] == ["s001", "s002"]
    assert merged["concepts"][0]["path"] == "doctrines/doc-123/governance/approval-rules.md"


def test_incomplete_batch_is_rejected():
    plan = _outline()
    batches = prepare_doctrine_consolidation_batches(plan, _taxonomy(), batch_size=4)
    response = {"iterations": [{
        "id": batches["batches"][0]["id"], "output": {
            "status": "ok", "groups": [{
                "member_ids": ["approval-rules"], "title": "Approval",
                "description": "Description", "folder_id": "governance",
            }],
        },
    }]}
    with pytest.raises(ValueError, match="not all"):
        assemble_doctrine_consolidation(plan, _taxonomy(), batches, response)
