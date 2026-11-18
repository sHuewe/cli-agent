"""Generic allow-list validation via RFC 6901 JSON pointer selectors."""
import pytest

from cli_agent.workflows.flow_json_relations import validate_allowed_values


def check(allowed, data, **options):
    return validate_allowed_values(
        allowed=allowed, candidate=data,
        allowed_path="/folders/*/id", values_path="/groups/*/folder_id",
        null_policy="when_allowed_empty", **options,
    )


def test_valid_values_preserve_original_payload():
    source = {"status": "ok", "groups": [
        {"member_ids": ["a"], "folder_id": "rules"},
        {"member_ids": ["b"], "folder_id": "definitions"},
    ]}
    report = check({"folders": [{"id": "rules"}, {"id": "definitions"}]}, source)
    assert report["status"] == "ok"
    assert report["checked_count"] == 2
    assert report["errors"] == []
    assert report["value"] == source


def test_invalid_value_returns_precise_findings_without_truncation():
    source = {"groups": [{"folder_id": "rules"}, {"folder_id": "invented"}]}
    report = check({"folders": [{"id": "rules"}]}, source)
    assert report["status"] == "needs_attention"
    assert report["errors"] == [
        {"path": "/groups/1/folder_id", "reason": "not_allowed", "value": "invented"}
    ]
    with pytest.raises(ValueError, match="JSON value"):
        check({"folders": [{"id": "rules"}]}, source, on_invalid="raise")


def test_empty_allowed_list_allows_null_but_not_invented_identifier():
    allowed = {"folders": []}
    assert check(allowed, {"groups": [{"folder_id": None}]})["status"] == "ok"
    report = check(allowed, {"groups": [{"folder_id": "made-up"}]})
    assert report["errors"][0]["reason"] == "not_allowed"


def test_nonempty_allowed_list_rejects_null_and_missing_field():
    allowed = {"folders": [{"id": "rules"}]}
    report = check(allowed, {"groups": [{"folder_id": None}, {}]})
    assert [item["reason"] for item in report["errors"]] == [
        "null_not_allowed", "missing_field",
    ]


def test_missing_or_invalid_allowed_schema_fails_closed():
    with pytest.raises(ValueError, match="does not resolve"):
        check({"not_folders": []}, {"groups": [{"folder_id": "anything"}]})
    with pytest.raises(ValueError, match="Wildcard requires JSON array"):
        check({"folders": {}}, {"groups": [{"folder_id": "anything"}]})


def test_missing_candidate_group_array_is_reported_not_accepted():
    report = check({"folders": [{"id": "rules"}]}, {"groups": "not-array"})
    assert report["status"] == "needs_attention"
    assert report["errors"][0]["reason"] == "expected_array"


def test_json_type_distinctness_and_pointer_escaping():
    result = validate_allowed_values(
        allowed={"valid": [1, True, {"key": [2]}]},
        candidate={"items": [{"v/x": 1.0}, {"v/x": True}, {"v/x": {"key": [2]}}]},
        allowed_path="/valid/*", values_path="/items/*/v~1x",
    )
    assert result["status"] == "ok"
    bad = validate_allowed_values(
        allowed={"valid": [1]},
        candidate={"items": [{"v~x": True}]},
        allowed_path="/valid/*", values_path="/items/*/v~0x",
    )
    assert bad["status"] == "needs_attention"


@pytest.mark.parametrize("invalid", ["/folders/id", "folders/*/id"])
def test_rejects_selectors_without_valid_wildcard(invalid):
    with pytest.raises(ValueError):
        validate_allowed_values(
            allowed={"folders": []}, candidate={"groups": []},
            allowed_path=invalid, values_path="/groups/*/folder_id",
        )
