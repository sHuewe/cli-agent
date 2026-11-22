"""Scan routing identity is reconciled only after checking all planned section metadata."""
import copy

import pytest

from cli_agent.workflows.flow_route_normalization import canonicalize_group_routes


def fixtures():
    sections = {
        "source_path": "md/example.md",
        "sections": [
            {"id": "s001", "start_line": 1, "end_line": 10},
            {"id": "s002", "start_line": 11, "end_line": 20},
        ],
    }
    topics = {"folder": {"id": "example", "path": "doctrines/example"}}
    routes = [
        {
            "status": "ok", "section_id": sid, "document_id": "model-document-id",
            "folder_id": "model-created-folder", "chunk_id": sid,
            "source_path": "md/example.md", "start_line": a, "end_line": b,
            "knowledge_items": [{"id": sid + ":rule001", "topic_key": sid + "-t001"}],
            "candidate_concepts": [],
        }
        for sid, a, b in (("s001", 1, 10), ("s002", 11, 20))
    ]
    return routes, sections, topics


def test_normalizes_both_routing_ids_without_mutating_model_checkpoints():
    routes, sections, topics = fixtures()
    before = copy.deepcopy(routes)
    normalized = canonicalize_group_routes(routes, sections, topics)
    assert [x["folder_id"] for x in normalized] == ["example", "example"]
    assert [x["document_id"] for x in normalized] == ["example", "example"]
    assert routes == before
    for old, new in zip(routes, normalized):
        assert {k: v for k, v in new.items() if k not in {"folder_id", "document_id"}} == {
            k: v for k, v in old.items() if k not in {"folder_id", "document_id"}
        }


@pytest.mark.parametrize("key,value", [
    ("source_path", "md/other.md"),
    ("chunk_id", "s002"),
    ("section_id", "s999"),
    ("start_line", 5),
    ("end_line", 11),
    ("status", "needs_attention"),
])
def test_rejects_other_metadata_changes(key, value):
    routes, sections, topics = fixtures()
    routes[0][key] = value
    with pytest.raises(ValueError):
        canonicalize_group_routes(routes, sections, topics)



@pytest.mark.parametrize("key,value", [
    ("document_id", ""),
    ("document_id", None),
    ("document_id", 123),
    ("folder_id", ""),
    ("folder_id", None),
    ("folder_id", 123),
])
def test_rejects_empty_or_nonstring_route_identity(key, value):
    routes, sections, topics = fixtures()
    routes[0][key] = value
    with pytest.raises(ValueError, match="must be a nonempty string"):
        canonicalize_group_routes(routes, sections, topics)


def test_noncanonical_document_id_preserves_assignment_id_prefix():
    routes, sections, topics = fixtures()
    assert routes[0]["document_id"] != topics["folder"]["id"]
    result = canonicalize_group_routes(routes, sections, topics)
    # join_by_alias uses the fixed source slug as ID prefix; group_items
    # must construct the same identity from the canonical document_id.
    assigned_item_id = topics["folder"]["id"] + ":" + routes[0]["knowledge_items"][0]["id"]
    grouped_item_id = result[0]["document_id"] + ":" + result[0]["knowledge_items"][0]["id"]
    assert assigned_item_id == grouped_item_id


def test_rejects_missing_sections_and_duplicate_sections():
    routes, sections, topics = fixtures()
    with pytest.raises(ValueError, match="Scan count"):
        canonicalize_group_routes(routes[:1], sections, topics)
    routes[1]["section_id"] = "s001"
    with pytest.raises(ValueError, match="duplicate"):
        canonicalize_group_routes(routes, sections, topics)


def test_rejects_untrusted_topic_folder_path():
    routes, sections, topics = fixtures()
    topics["folder"]["path"] = "doctrines/other"
    with pytest.raises(ValueError, match="folder or source"):
        canonicalize_group_routes(routes, sections, topics)


def test_group_operation_opt_in_contract(tmp_path):
    from pathlib import Path
    from cli_agent.workflows.flow import load_flow

    flow_file = tmp_path / "flow.toml"
    flow_file.write_text(
        'version = 1\nresult = "steps.group.output"\n'
        '[[steps]]\nid = "group"\noutput = "state/group.json"\n'
        '[steps.json_operation]\n'
        'kind = "group_items"\ninput = "assignments"\n'
        'canonicalize_route_ids = true\n'
        '[steps.json_inputs.assignments]\npath = "state/assign.json"\n'
        '[steps.json_inputs.routes]\npath = "state/routes.json"\n'
        '[steps.json_inputs.topics]\npath = "state/topics.json"\n'
        '[steps.json_inputs.sections]\npath = "state/sections.json"\n',
        encoding="utf-8",
    )
    loaded = load_flow(Path("flow.toml"), workspace=tmp_path)
    assert loaded.steps[0].json_operation["canonicalize_route_ids"] is True
