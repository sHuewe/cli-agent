from cli_agent.workflows.flow_json_validation import (
    prepare_section_outline_tasks, assemble_section_outlines,
)
import pytest


def data():
    sections = {"mode": "sections", "source_path": "sources/doc.md", "sections": [
        {"id": "s001", "start_line": 1, "end_line": 20},
        {"id": "s002", "start_line": 21, "end_line": 40},
    ]}
    analyses = {"iterations": [
        {"id": sec["id"], "output": {
            "status": "ok", "section_id": sec["id"],
            "source_path": "sources/doc.md",
            "start_line": sec["start_line"], "end_line": sec["end_line"],
            "summary": "local", "topics": [],
        }} for sec in sections["sections"]
    ]}
    return sections, analyses


def test_section_tasks_include_only_matching_analysis():
    sections, analyses = data()
    tasks = prepare_section_outline_tasks(sections, analyses)
    assert len(tasks["tasks"]) == 2
    assert tasks["tasks"][0]["analysis"]["section_id"] == "s001"
    assert "s002" not in str(tasks["tasks"][0])


def test_short_doctrine_uses_single_direct_task():
    tasks = prepare_section_outline_tasks({
        "mode": "direct", "source_path": "sources/doc.md", "sections": [],
    }, {"iterations": []})
    assert tasks == {"tasks": [{
        "id": "direct", "direct": True, "source_path": "sources/doc.md",
    }]}


def test_assembled_outlines_merge_shared_concept_keys_without_llm():
    tasks = prepare_section_outline_tasks(*data())
    results = {"iterations": [
        {"id": sid, "output": {
            "status": "ok", "section_id": sid,
            "concepts": [
                {"topic_key": "common-procedure", "title": "Procedure",
                 "description": "Summary", "tags": ["workflow"]},
                {"topic_key": key, "title": key.title(),
                 "description": "Specific topic", "tags": []},
            ],
        }} for sid, key in (("s001", "first"), ("s002", "second"))
    ]}
    merged = assemble_section_outlines(
        tasks, results, source_path="sources/doc.md", source_slug="doc-123",
    )
    assert [c["id"] for c in merged["concepts"]] == [
        "common-procedure", "first", "second",
    ]
    assert merged["concepts"][0]["section_ids"] == ["s001", "s002"]
    assert all(c["path"].startswith("doctrines/doc-123/") for c in merged["concepts"])


def test_missing_or_wrong_section_outline_is_rejected():
    tasks = prepare_section_outline_tasks(*data())
    with pytest.raises(ValueError, match="fehlen"):
        assemble_section_outlines(
            tasks, {"iterations": []},
            source_path="sources/doc.md", source_slug="doc-123",
        )
    with pytest.raises(ValueError, match="inkonsistent"):
        assemble_section_outlines(
            tasks, {"iterations": [
                {"id": "s001", "output": {"status": "ok", "section_id": "s002",
                     "concepts": []}},
                {"id": "s002", "output": {"status": "ok", "section_id": "s002",
                     "concepts": []}},
            ]},
            source_path="sources/doc.md", source_slug="doc-123",
        )


def test_incomplete_analysis_blocks_outlines():
    sections, analyses = data()
    analyses["iterations"][0]["output"]["status"] = "needs_attention"
    with pytest.raises(ValueError, match="Unvollständige"):
        prepare_section_outline_tasks(sections, analyses)
