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


def test_documented_toml_examples_parse_and_configure_partition_validation():
    import tomllib
    from pathlib import Path
    import re

    docs = Path(__file__).resolve().parents[1] / "docs" / "flow-json-relations.md"
    source = docs.read_text(encoding="utf-8")
    examples = re.findall(r"```toml\n(.*?)\n```", source, flags=re.DOTALL)
    assert len(examples) == 3
    parsed = [tomllib.loads(example) for example in examples]
    partition_step = parsed[2]["steps"][0]
    operation = partition_step["json_operation"]
    assert operation["kind"] == "json_validate_partition"
    assert re.fullmatch(operation["result_id_pattern"], "example-safe-id")
    assert not re.fullmatch(operation["result_id_pattern"], "../unsafe")
    assert operation["result_path_field"] == "/file"
    assert operation["result_path_template"] == "output/{id}.json"


@pytest.mark.parametrize(("values", "expected_eligible"), [
    ([2024, 2025], True),
    ([2024, 2024], False),
    ([True, 1], True),
    ([False, 0], True),
    ([None, "null"], True),
    ([[1, 2], [1, 2]], False),
    ([[1, 2], [2, 1]], True),
    ([{"x": 1, "y": [True]}, {"y": [True], "x": 1}], False),
    ([{"x": 1}, {"x": 2}], True),
])
def test_partition_eligibility_accepts_all_json_value_types(values, expected_eligible):
    batch, result = _batch(), _result()
    for member, value in zip(batch["items"][0]["groups"][0]["members"], values):
        member["attribute"] = value
    batch["items"][0]["groups"][1]["members"][0]["attribute"] = None

    if expected_eligible:
        checked = validate_partition(
            source=batch, result=result, source_path="/items/*/groups/*",
            reference_template="i{0:04d}-g{1:04d}",
            distinct_path="/members/*/attribute", min_distinct=2,
            selected_path="/groups/*", references_path="/source_refs/*",
            excluded_path="/excluded/*", source_members_path="/members/*",
            result_members_path="/members/*", member_fields=["/category", "/reference"],
        )
        assert checked["validated_eligible_count"] == 1
    else:
        with pytest.raises(ValueError, match="ineligible source reference"):
            validate_partition(
                source=batch, result=result, source_path="/items/*/groups/*",
                reference_template="i{0:04d}-g{1:04d}",
                distinct_path="/members/*/attribute", min_distinct=2,
                selected_path="/groups/*", references_path="/source_refs/*",
                excluded_path="/excluded/*", source_members_path="/members/*",
                result_members_path="/members/*", member_fields=["/category", "/reference"],
            )


def test_partition_distinct_numeric_literal_values_are_lossless():
    from cli_agent.workflows.flow_json_operations import _JsonNumber
    batch, result = _batch(), _result()
    first, second = batch["items"][0]["groups"][0]["members"]
    first["value"] = _JsonNumber("1e100")
    second["value"] = _JsonNumber("1.00e100")
    batch["items"][0]["groups"][1]["members"][0]["value"] = 2
    with pytest.raises(ValueError, match="ineligible source reference"):
        validate_partition(
            source=batch, result=result, source_path="/items/*/groups/*",
            reference_template="i{0:04d}-g{1:04d}",
            distinct_path="/members/*/value", min_distinct=2,
            selected_path="/groups/*", references_path="/source_refs/*",
            excluded_path="/excluded/*", source_members_path="/members/*",
            result_members_path="/members/*", member_fields=["/category", "/reference"],
        )


def test_partition_distinct_values_reject_nonfinite_numbers():
    batch, result = _batch(), _result()
    batch["items"][0]["groups"][0]["members"][0]["attribute"] = float("nan")
    batch["items"][0]["groups"][0]["members"][1]["attribute"] = 2025
    batch["items"][0]["groups"][1]["members"][0]["attribute"] = None
    with pytest.raises(ValueError, match="Nonfinite JSON number"):
        validate_partition(
            source=batch, result=result, source_path="/items/*/groups/*",
            reference_template="i{0:04d}-g{1:04d}",
            distinct_path="/members/*/attribute", min_distinct=2,
            selected_path="/groups/*", references_path="/source_refs/*",
            excluded_path="/excluded/*", source_members_path="/members/*",
            result_members_path="/members/*", member_fields=["/category", "/reference"],
        )


@pytest.mark.parametrize("value", [
    1.5,
    1,
    True,
    None,
])
def test_reference_ids_require_real_json_strings(value):
    from cli_agent.workflows.flow_json_operations import _JsonNumber
    for raw in (value, _JsonNumber("1.5")):
        items = [{"entries": [{"id": "item", "join_key": raw}]}]
        mapping = {"groups": [{"id": "destination", "aliases": ["1.5"]}]}
        with pytest.raises(ValueError, match="nonempty JSON string"):
            join_by_alias(
                items=items, mapping=mapping,
                items_path="/*/entries/*", item_id_path="/id",
                item_key_path="/join_key", mapping_path="/groups/*",
                mapping_id_path="/id", mapping_alias_path="/aliases/*",
            )


def test_partition_rejects_numeric_member_masquerading_as_string():
    from cli_agent.workflows.flow_json_operations import _JsonNumber
    batch, result = _batch(), _result()
    # The parser keeps the number 1.5 in _JsonNumber (a str subclass).
    # It must NOT compare equal to a genuine JSON string "1.5".
    batch["items"][0]["groups"][0]["members"][0]["reference"] = "1.5"
    result["groups"][0]["members"][0]["reference"] = _JsonNumber("1.5")
    with pytest.raises(ValueError, match="nonempty JSON string"):
        _validate(batch, result)


def test_partition_rejects_numeric_reference_ids_and_aliases():
    from cli_agent.workflows.flow_json_operations import _JsonNumber
    batch, result = _batch(), _result()
    result["groups"][0]["source_refs"] = [_JsonNumber("1.5")]
    with pytest.raises(ValueError, match="invalid ID"):
        _validate(batch, result)
    items = [{"entries": [{"id": "item", "join_key": "tag"}]}]
    mapping = {"groups": [{"id": "destination", "aliases": [_JsonNumber("1.5")]}]}
    with pytest.raises(ValueError, match="invalid ID"):
        join_by_alias(
            items=items, mapping=mapping,
            items_path="/*/entries/*", item_id_path="/id",
            item_key_path="/join_key", mapping_path="/groups/*",
            mapping_id_path="/id", mapping_alias_path="/aliases/*",
        )


def test_partition_supports_root_source_status_pointer():
    batch, result = _batch(), _result()
    source = {
        "status": "ok", "warnings": [],
        "payload": {"groups": batch["items"][0]["groups"]},
    }
    kwargs = dict(
        source=source, result=result, source_path="/payload/groups/*",
        source_records_path="",
        reference_template="i0001-g{0:04d}",
        distinct_path="/members/*/category", min_distinct=2,
        selected_path="/groups/*", references_path="/source_refs/*",
        excluded_path="/excluded/*", source_members_path="/members/*",
        result_members_path="/members/*", result_id_path="/id",
        member_fields=["/category", "/reference"],
    )
    assert validate_partition(**kwargs)["status"] == "ok"
    source["status"] = "needs_attention"
    with pytest.raises(ValueError, match="preserve incomplete"):
        validate_partition(**kwargs)
    source["status"] = "ok"
    source["warnings"] = ["upstream incomplete"]
    with pytest.raises(ValueError, match="preserve incomplete"):
        validate_partition(**kwargs)


@pytest.mark.parametrize("extra", [
    {"result_id_pattern": r"^[a-z]+$"},
    {"result_path_field": "/location", "result_path_template": "out/{id}.json"},
    {"result_id_prefix": "required-"},
])
def test_partition_rejects_id_dependent_options_without_id_selector(extra):
    batch, result = _batch(), _result()
    with pytest.raises(ValueError, match="result_id_path is required"):
        validate_partition(
            source=batch, result=result,
            source_path="/items/*/groups/*",
            reference_template="i{0:04d}-g{1:04d}",
            distinct_path="/members/*/category", min_distinct=2,
            selected_path="/groups/*", references_path="/source_refs/*",
            excluded_path="/excluded/*", source_members_path="/members/*",
            result_members_path="/members/*", member_fields=["/category", "/reference"],
            **extra,
        )


def test_partition_without_id_selector_still_validates_memberships():
    batch, result = _batch(), _result()
    checked = validate_partition(
        source=batch, result=result, source_path="/items/*/groups/*",
        reference_template="i{0:04d}-g{1:04d}",
        distinct_path="/members/*/category", min_distinct=2,
        selected_path="/groups/*", references_path="/source_refs/*",
        excluded_path="/excluded/*", source_members_path="/members/*",
        result_members_path="/members/*", member_fields=["/category", "/reference"],
    )
    assert checked["validated_group_count"] == 2
