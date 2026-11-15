"""Generic JSON relationship operations, without domain-specific semantics."""
import pytest

from cli_agent.workflows.flow_json_relations import (
    join_by_alias, project_fields, validate_partition,
)
from cli_agent.workflows.flow_json_operations import transform_json


def test_projection_renames_unrelated_json_fields_without_mutation():
    source = {"a": {"label": "Alpha"}, "b": [1, 2], "c": True}
    result = project_fields(source, fields={"name": "/a/label", "values": "/b"})
    assert result == {"name": "Alpha", "values": [1, 2]}
    result["values"].append(3)
    assert source["b"] == [1, 2]


def test_join_preserves_every_item_and_resolves_aliases():
    items = [
        {"knowledge_items": [{"id": "r1", "topic_key": "a"}]},
        {"knowledge_items": [{"id": "r2", "topic_key": "b"}]},
    ]
    mapping = {"concepts": [{"id": "merged", "merged_from": ["a", "b"]}]}
    result = join_by_alias(
        items=items, mapping=mapping, items_path="/*/knowledge_items/*",
        item_id_path="/id", item_key_path="/topic_key",
        mapping_path="/concepts/*", mapping_id_path="/id",
        mapping_alias_path="/merged_from/*", id_prefix="doc:",
    )
    assert result == [{"assignments": [
        {"item_id": "doc:r1", "topic_id": "merged"},
        {"item_id": "doc:r2", "topic_id": "merged"},
    ]}]


@pytest.mark.parametrize("broken_items,broken_mapping,match", [
    ([{"knowledge_items": [{"id": "r1", "topic_key": "missing"}]}],
     {"concepts": [{"id": "merged", "merged_from": ["a"]}]}, "No mapping"),
    ([{"knowledge_items": [{"id": "r1", "topic_key": "a"},
                           {"id": "r1", "topic_key": "a"}]}],
     {"concepts": [{"id": "merged", "merged_from": ["a"]}]}, "Duplicate source ID"),
    ([{"knowledge_items": [{"id": "r1", "topic_key": "a"}]}],
     {"concepts": [{"id": "one", "merged_from": ["a"]},
                   {"id": "two", "merged_from": ["a"]}]}, "Alias appears more than once"),
    ([{"knowledge_items": [{"id": "r1", "topic_key": "a"}]}],
     {"concepts": [{"id": "one", "merged_from": ["a", "unused"]}]}, "Unused mapping aliases"),
])
def test_join_rejects_broken_relation_contracts(broken_items, broken_mapping, match):
    with pytest.raises(ValueError, match=match):
        join_by_alias(
            items=broken_items, mapping=broken_mapping,
            items_path="/*/knowledge_items/*",
            item_id_path="/id", item_key_path="/topic_key",
            mapping_path="/concepts/*", mapping_id_path="/id",
            mapping_alias_path="/merged_from/*",
        )


def _batch():
    return {"id": "b0001", "items": [{"status": "ok", "warnings": [], "groups": [
        {"members": [
            {"category": "alpha", "reference": "a/one"},
            {"category": "beta", "reference": "b/two"},
        ]},
        {"members": [{"category": "alpha", "reference": "a/three"}]},
    ]}]}


def _result():
    return {"status": "ok", "warnings": [],
            "groups": [{"id": "b0001-example", "location": "out/b0001-example.json",
                        "source_refs": ["i0001-g0001"],
                        "members": [
                            {"category": "alpha", "reference": "a/one"},
                            {"category": "beta", "reference": "b/two"},
                        ]}],
            "excluded": ["i0001-g0002"]}


def _validate(batch, result):
    return validate_partition(
        source=batch, result=result,
        source_path="/items/*/groups/*",
        reference_template="i{0:04d}-g{1:04d}",
        distinct_path="/members/*/category", min_distinct=2,
        selected_path="/groups/*", references_path="/source_refs/*",
        excluded_path="/excluded/*", source_members_path="/members/*",
        result_members_path="/members/*", result_id_path="/id",
        result_id_prefix="b0001-", result_path_field="/location",
        result_path_template="out/{id}.json",
        member_fields=["/category", "/reference"],
    )


def test_partition_generic_coverage_and_member_union():
    obj = _validate(_batch(), _result())
    assert obj["status"] == "ok"
    assert obj["validated_group_count"] == 2
    assert obj["validated_eligible_count"] == 1


def test_partition_fails_closed_on_missing_or_mutated_output():
    b, r = _batch(), _result()
    r["groups"].clear()
    with pytest.raises(ValueError, match="Missing source"):
        _validate(b, r)
    b, r = _batch(), _result()
    r["groups"][0]["members"].pop()
    with pytest.raises(ValueError, match="do not equal"):
        _validate(b, r)
    b, r = _batch(), _result()
    r["excluded"].append("i0001-g0001")
    with pytest.raises(ValueError, match="eligible excluded"):
        _validate(b, r)


def test_partition_does_not_hide_input_failure():
    b, r = _batch(), _result()
    b["items"][0]["status"] = "needs_attention"
    with pytest.raises(ValueError, match="preserve incomplete"):
        _validate(b, r)
    r["status"] = "needs_attention"
    assert _validate(b, r)["status"] == "needs_attention"


def test_array_batching_caps_complete_items_generically():
    items = [{"x": str(i)} for i in range(21)]
    batches = transform_json(
        kind="batch_arrays", value=[{"entries": items}],
        array_paths=["/entries"], max_fragment_items=4,
        batch_size=2, max_batch_bytes=8000, target_fragment_bytes=2400,
    )["batches"]
    fragments = [f for batch in batches for f in batch["items"]]
    assert all(len(f["entries"]) <= 4 for f in fragments)
    assert [x for f in fragments for x in f["entries"]] == items

def test_generic_regex_constraints_reject_unsafe_output_ids_and_members():
    batch, out = _batch(), _result()
    out["groups"][0]["id"] = "b0001-../../outside"
    with pytest.raises(ValueError, match="Invalid or duplicate output ID"):
        validate_partition(
            source=batch, result=out,
            source_path="/items/*/groups/*",
            reference_template="i{0:04d}-g{1:04d}",
            distinct_path="/members/*/category", min_distinct=2,
            selected_path="/groups/*", references_path="/source_refs/*",
            excluded_path="/excluded/*", source_members_path="/members/*",
            result_members_path="/members/*", result_id_path="/id",
            result_id_pattern=r"^[a-z][a-z0-9-]*$",
            member_fields=["/category", "/reference"],
        )
    batch, out = _batch(), _result()
    out["groups"][0]["members"][0]["reference"] = "../escape"
    with pytest.raises(ValueError, match="Member field does not match"):
        validate_partition(
            source=batch, result=out,
            source_path="/items/*/groups/*",
            reference_template="i{0:04d}-g{1:04d}",
            distinct_path="/members/*/category", min_distinct=2,
            selected_path="/groups/*", references_path="/source_refs/*",
            excluded_path="/excluded/*", source_members_path="/members/*",
            result_members_path="/members/*",
            member_fields=["/category", "/reference"],
            member_patterns={"/reference": r"^[a-z]+/[a-z]+$"},
        )


def test_partition_checks_status_with_renamed_source_records():
    batch, out = _batch(), _result()
    batch["records"] = batch.pop("items")
    batch["records"][0]["status"] = "needs_attention"
    with pytest.raises(ValueError, match="preserve incomplete"):
        validate_partition(
            source=batch, result=out,
            source_path="/records/*/groups/*",
            reference_template="i{0:04d}-g{1:04d}",
            distinct_path="/members/*/category", min_distinct=2,
            selected_path="/groups/*", references_path="/source_refs/*",
            excluded_path="/excluded/*", source_members_path="/members/*",
            result_members_path="/members/*", result_id_path="/id",
            member_fields=["/category", "/reference"],
        )


def test_partition_checks_nested_and_explicit_source_status_records():
    batch, out = _batch(), _result()
    batch = {"payload": {"blocks": batch["items"]}}
    batch["payload"]["blocks"][0]["warnings"] = ["source incomplete"]
    kwargs = dict(
        source=batch, result=out, source_path="/payload/blocks/*/groups/*",
        reference_template="i{0:04d}-g{1:04d}",
        distinct_path="/members/*/category", min_distinct=2,
        selected_path="/groups/*", references_path="/source_refs/*",
        excluded_path="/excluded/*", source_members_path="/members/*",
        result_members_path="/members/*", result_id_path="/id",
        member_fields=["/category", "/reference"],
    )
    with pytest.raises(ValueError, match="preserve incomplete"):
        validate_partition(**kwargs)
    # Explicit record location is an alternative to the inferred parent.
    with pytest.raises(ValueError, match="preserve incomplete"):
        validate_partition(
            **{**kwargs, "source_records_path": "/payload/blocks/*"}
        )


@pytest.mark.parametrize("status_field,status_pointer", [
    ("state/status", "/state~1status"),
    ("state~status", "/state~0status"),
])
def test_partition_writes_decoded_json_pointer_status_field(status_field, status_pointer):
    batch, out = _batch(), _result()
    out[status_field] = "ok"
    out["warnings"] = ["Review needed"]
    verified = validate_partition(
        source=batch, result=out, source_path="/items/*/groups/*",
        reference_template="i{0:04d}-g{1:04d}",
        distinct_path="/members/*/category", min_distinct=2,
        selected_path="/groups/*", references_path="/source_refs/*",
        excluded_path="/excluded/*", source_members_path="/members/*",
        result_members_path="/members/*", result_id_path="/id",
        result_status_path=status_pointer,
        member_fields=["/category", "/reference"],
    )
    assert verified[status_field] == "needs_attention"
    assert out[status_field] == "ok"
    assert status_pointer[1:] not in verified or status_pointer[1:] == status_field


def test_partition_rejects_invalid_status_pointer_and_missing_source_records():
    batch, out = _batch(), _result()
    with pytest.raises(ValueError, match="top-level field"):
        validate_partition(
            source=batch, result=out, source_path="/items/*/groups/*",
            reference_template="i{0:04d}-g{1:04d}",
            distinct_path="/members/*/category", min_distinct=2,
            selected_path="/groups/*", references_path="/source_refs/*",
            excluded_path="/excluded/*", source_members_path="/members/*",
            result_members_path="/members/*", result_id_path="/id",
            result_status_path="/status/extra",
            member_fields=["/category", "/reference"],
        )
    with pytest.raises(ValueError, match="does not resolve"):
        validate_partition(
            source=batch, result=out, source_path="/items/*/groups/*",
            source_records_path="/unavailable/*",
            reference_template="i{0:04d}-g{1:04d}",
            distinct_path="/members/*/category", min_distinct=2,
            selected_path="/groups/*", references_path="/source_refs/*",
            excluded_path="/excluded/*", source_members_path="/members/*",
            result_members_path="/members/*", result_id_path="/id",
            member_fields=["/category", "/reference"],
        )
