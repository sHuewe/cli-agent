"""Regression coverage for structured flow and delegated-agent contracts."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_agent.models.json_contract import check_json_contract, json_contract_error
from cli_agent.state.sub_runs import SUB_RUN_RESULT_SCHEMA, validate_sub_result
from cli_agent.workflows.flow import load_flow


def test_sub_run_schema_matches_existing_contract():
    valid = {
        "status": "completed", "summary": "Done",
        "evidence_ids": ["new-1"], "confidence": "high",
        "uncertainties": [],
    }
    assert validate_sub_result(json.dumps(valid), {"new-1"}) == valid
    assert json_contract_error(json.dumps(valid), SUB_RUN_RESULT_SCHEMA) is None


@pytest.mark.parametrize("change", [
    {"status": "unknown"},
    {"confidence": "certain"},
    {"summary": 123},
    {"extra": True},
    {"evidence_ids": ["new-1", "new-1"]},
    {"uncertainties": [123]},
])
def test_sub_run_schema_rejects_invalid_shapes(change):
    result = {
        "status": "completed", "summary": "Done",
        "evidence_ids": ["new-1"], "confidence": "high",
        "uncertainties": [],
    }
    result.update(change)
    with pytest.raises(ValueError):
        validate_sub_result(json.dumps(result), {"new-1"})


def test_sub_run_schema_rejects_unsaved_evidence():
    result = {
        "status": "completed", "summary": "Done",
        "evidence_ids": ["invented"], "confidence": "high",
        "uncertainties": [],
    }
    with pytest.raises(ValueError, match="unknown or not newly saved"):
        validate_sub_result(json.dumps(result), {"existing"})


def test_json_contract_rejects_remote_references():
    with pytest.raises(ValueError, match="local"):
        check_json_contract({"$ref": "https://example.com/schema.json"})


def test_json_contract_checks_types():
    schema = check_json_contract({
        "type": "object", "required": ["count"],
        "additionalProperties": False,
        "properties": {"count": {"type": "integer", "minimum": 1}},
    })
    assert json_contract_error('{"count": 3}', schema) is None
    assert "count" in json_contract_error('{"count": "3"}', schema)
    assert json_contract_error('{"count": 0}', schema) is not None


def test_flow_loads_optional_output_schema(tmp_path: Path):
    (tmp_path / "prompt.md").write_text("Return JSON", encoding="utf-8")
    (tmp_path / "result.schema.json").write_text(json.dumps({
        "type": "object", "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
    }), encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        'version = 1\n[[steps]]\nid = "check"\n'
        'prompt_file = "prompt.md"\nresponse_format = "json"\n'
        'output_schema = "result.schema.json"\n', encoding="utf-8",
    )
    flow = load_flow(Path("flow.toml"), workspace=tmp_path)
    assert flow.steps[0].output_schema["required"] == ["ok"]


def test_flow_rejects_schema_for_text_step(tmp_path: Path):
    (tmp_path / "prompt.md").write_text("Return text", encoding="utf-8")
    (tmp_path / "result.schema.json").write_text('{"type":"object"}', encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        'version = 1\n[[steps]]\nid = "check"\n'
        'prompt_file = "prompt.md"\noutput_schema = "result.schema.json"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="requires a JSON model step"):
        load_flow(Path("flow.toml"), workspace=tmp_path)

@pytest.mark.asyncio
async def test_schema_mismatch_is_corrected_within_same_model_run():
    from types import SimpleNamespace
    from cli_agent.runtime.agent_conversation import ConversationMixin

    calls = []

    async def fake_loop(**kwargs):
        calls.append(kwargs)
        return '{"ok": true}'

    agent = SimpleNamespace(
        response_format="json",
        output_schema={
            "type": "object", "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
        _agui_pending_json_repairs=0,
        _run_model_loop=fake_loop,
        max_tool_calls=10,
    )
    messages = []
    result = await ConversationMixin._ensure_json_response(
        agent, '{"ok": "yes"}', messages=messages, tools=[], routes={},
        enabled_server_names=set(), run_state=SimpleNamespace(),
    )
    assert json.loads(result) == {"ok": True}
    assert len(calls) == 1
    assert "schema" in messages[-1]["content"].lower()
    assert agent._agui_pending_json_repairs == 0


@pytest.mark.asyncio
async def test_unfixable_schema_mismatch_raises():
    from types import SimpleNamespace
    from cli_agent.runtime.agent_conversation import ConversationMixin

    async def fake_loop(**kwargs):
        return '{"ok": "still invalid"}'

    agent = SimpleNamespace(
        response_format="json",
        output_schema={
            "type": "object", "required": ["ok"],
            "properties": {"ok": {"type": "boolean"}},
        },
        _agui_pending_json_repairs=0,
        _run_model_loop=fake_loop,
        max_tool_calls=10,
    )
    with pytest.raises(ValueError, match="kein valid JSON"):
        await ConversationMixin._ensure_json_response(
            agent, '{"ok": "invalid"}', messages=[], tools=[], routes={},
            enabled_server_names=set(), run_state=SimpleNamespace(),
        )


def test_json_contract_accepts_keyword_like_property_names_and_const_data():
    schema = check_json_contract({
        "type": "object",
        "properties": {
            "$ref": {"type": "string"},
            "record": {"const": {"$id": "record-id"}},
        },
    })
    assert json_contract_error(
        '{"$ref": "ordinary-value", "record": {"$id": "record-id"}}',
        schema,
    ) is None


def test_json_contract_rejects_remote_ref_in_nested_subschema():
    with pytest.raises(ValueError, match="local"):
        check_json_contract({
            "type": "object",
            "properties": {
                "value": {"$ref": "https://example.com/remote.json"},
            },
        })


def test_existing_json_checkpoint_rejects_schema_mismatch(tmp_path: Path):
    from cli_agent.workflows.flow import _existing_json_checkpoint
    (tmp_path / "prompt.md").write_text("Return JSON", encoding="utf-8")
    (tmp_path / "result.schema.json").write_text(json.dumps({
        "type": "object", "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
    }), encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        'version = 1\n[[steps]]\nid = "check"\n'
        'prompt_file = "prompt.md"\nresponse_format = "json"\n'
        'output_schema = "result.schema.json"\noutput = "result.json"\n',
        encoding="utf-8",
    )
    step = load_flow(Path("flow.toml"), workspace=tmp_path).steps[0]
    output = tmp_path / "result.json"
    output.write_text('{"ok": "bad"}', encoding="utf-8")
    with pytest.raises(ValueError, match="does not match output_schema"):
        _existing_json_checkpoint(step, workspace=tmp_path, output=output)
    output.write_text('{"ok": true}', encoding="utf-8")
    assert json.loads(_existing_json_checkpoint(
        step, workspace=tmp_path, output=output,
    )) == {"ok": True}


def test_local_ref_cannot_hide_external_ref():
    with pytest.raises(ValueError, match="local"):
        check_json_contract({
            "$ref": "#/hidden",
            "hidden": {"$ref": "https://example.invalid/remote.json"},
        })


def test_local_ref_to_literal_const_with_keyword_names():
    schema = check_json_contract({
        "$ref": "#/$defs/entry",
        "$defs": {"entry": {"const": {"$id": "ordinary-data", "$ref": "not-a-schema"}}},
    })
    assert json_contract_error(
        '{"$id":"ordinary-data","$ref":"not-a-schema"}', schema,
    ) is None


def test_flow_reserves_schema_as_protected_input(tmp_path: Path):
    from cli_agent.workflows.flow import _reserved_flow_input_paths, _filesystem_path_key
    (tmp_path / "prompt.md").write_text("Return JSON", encoding="utf-8")
    schema_path = tmp_path / "contract.json"
    schema_path.write_text('{"type":"object"}', encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        'version = 1\n[[steps]]\nid = "check"\n'
        'prompt_file = "prompt.md"\nresponse_format = "json"\n'
        'output_schema = "contract.json"\noutput = "contract.json"\n'
        'overwrite_output = true\n', encoding="utf-8",
    )
    flow = load_flow(Path("flow.toml"), workspace=tmp_path)
    assert flow.steps[0].output_schema_path == schema_path
    reserved = _reserved_flow_input_paths(flow, workspace=tmp_path)
    assert _filesystem_path_key(schema_path) in reserved


def test_decimal_multiple_of_uses_exact_arithmetic():
    schema = check_json_contract({"type": "number", "multipleOf": 0.1})
    assert json_contract_error("0.3", schema) is None
    assert json_contract_error("0.31", schema) is not None


def test_compact_checkpoint_identity_changes_when_schema_changes(tmp_path: Path):
    from cli_agent.workflows.flow_compact_checkpoints import checkpoint_path
    (tmp_path / "prompt.md").write_text("JSON", encoding="utf-8")
    schema = tmp_path / "contract.json"
    schema.write_text('{"type":"object","properties":{"ok":{"type":"boolean"}}}', encoding="utf-8")
    flow = tmp_path / "flow.toml"
    flow.write_text(
        'version = 1\nretain_intermediate = false\n[[steps]]\n'
        'id = "check"\nprompt_file = "prompt.md"\nresponse_format = "json"\n'
        'output_schema = "contract.json"\noutput = "result.json"\n',
        encoding="utf-8",
    )
    original = checkpoint_path(tmp_path, flow, "", {})
    schema.write_text('{"type":"object","properties":{"ok":{"type":"string"}}}', encoding="utf-8")
    changed = checkpoint_path(tmp_path, flow, "", {})
    assert original != changed


def test_decimal_integer_type_accepts_integral_json_numbers():
    schema = check_json_contract({"type": "integer"})
    for value in ("1", "1.0", "1e2", "-2.000"):
        assert json_contract_error(value, schema) is None
    for value in ("1.01", "true", "false", '"1"'):
        assert json_contract_error(value, schema) is not None


def test_local_json_pointer_decodes_percent_encoded_fragment():
    schema = check_json_contract({
        "$defs": {"a b": {"type": "string"}},
        "$ref": "#/$defs/a%20b",
    })
    assert json_contract_error('"hello"', schema) is None
    assert json_contract_error("12", schema) is not None


def test_local_json_pointer_decodes_encoded_slash_before_pointer_tokens():
    schema = check_json_contract({
        "$defs": {"a/b": {"type": "integer"}},
        "$ref": "#/$defs/a~1b",
    })
    assert json_contract_error("12", schema) is None


def test_local_json_pointer_preserves_empty_property_name():
    schema = check_json_contract({
        "$ref": "#//x",
        "": {"x": {"type": "integer"}},
    })
    assert json_contract_error("42", schema) is None
    assert json_contract_error('"wrong"', schema) is not None


@pytest.mark.parametrize("target", [{"type": "typo"}, 1, [], {"minimum": "not-a-number"}])
def test_local_reference_target_is_checked_against_metaschema(target):
    with pytest.raises(ValueError, match="Invalid JSON Schema reference target"):
        check_json_contract({"$ref": "#/hidden", "hidden": target})


def test_valid_local_reference_target_under_unknown_keyword():
    schema = check_json_contract({
        "$ref": "#/hidden", "hidden": {"type": "integer"},
    })
    assert json_contract_error("5", schema) is None
    assert json_contract_error('"wrong"', schema) is not None


@pytest.mark.parametrize(("multiple_of", "valid", "invalid"), [
    (1e-30, "1", "1.5e-30"),
    (0.1, "0.3", "0.31"),
    (1e-40, "1e-10", "1.5e-40"),
])
def test_multiple_of_is_independent_of_decimal_context_precision(
    multiple_of, valid, invalid,
):
    from decimal import getcontext, localcontext

    schema = check_json_contract({"type": "number", "multipleOf": multiple_of})
    with localcontext() as ctx:
        ctx.prec = 6
        assert json_contract_error(valid, schema) is None
        assert json_contract_error(invalid, schema) is not None


def test_multiple_of_in_nested_schema_and_local_reference():
    schema = check_json_contract({
        "type": "object",
        "$defs": {"tiny": {"type": "number", "multipleOf": 1e-30}},
        "properties": {"value": {"$ref": "#/$defs/tiny"}},
    })
    assert json_contract_error('{"value": 1}', schema) is None
    assert json_contract_error('{"value": 1.5e-30}', schema) is not None


def _dynamic_enum_flow(tmp_path: Path, *, reference: str, foreach: str | None = None):
    (tmp_path / "prompt.md").write_text("Return JSON", encoding="utf-8")
    (tmp_path / "schema.json").write_text(json.dumps({
        "type": "object",
        "required": ["choice"],
        "properties": {"choice": {"type": "string"}},
    }), encoding="utf-8")
    source = 'version = 1\n[[steps]]\nid = "discover"\nprompt_file = "prompt.md"\nresponse_format = "json"\noutput = "choices.json"\n'
    target = (
        '\n[[steps]]\nid = "select"\nprompt_file = "prompt.md"\n'
        'response_format = "json"\noutput_schema = "schema.json"\n'
        'output = "selection.json"\n'
    )
    if foreach is not None:
        target += f'foreach = "{foreach}"\n'
        target = target.replace('output = "selection.json"', 'output = "selection-${iteration.id}.json"')
    target += f'\n[steps.output_schema_enums]\n"/properties/choice" = "{reference}"\n'
    (tmp_path / "flow.toml").write_text(source + target, encoding="utf-8")
    return load_flow(Path("flow.toml"), workspace=tmp_path)


def test_dynamic_schema_enums_from_prior_step_output(tmp_path: Path):
    from cli_agent.workflows.flow import _resolve_step_schema_enums
    flow = _dynamic_enum_flow(
        tmp_path, reference="steps.discover.output.allowed",
    )
    step = flow.steps[1]
    concrete = _resolve_step_schema_enums(
        step, outputs={"discover": '{"allowed":["red","blue"]}'}, flow_input={},
    )
    assert concrete.output_schema["properties"]["choice"]["enum"] == ["red", "blue"]
    assert json_contract_error('{"choice":"blue"}', concrete.output_schema) is None
    assert json_contract_error('{"choice":"green"}', concrete.output_schema) is not None
    assert "enum" not in step.output_schema["properties"]["choice"]


def test_dynamic_schema_enum_rejects_unavailable_sources_and_bad_lists(tmp_path: Path):
    from cli_agent.workflows.flow import _resolve_step_schema_enums
    step = _dynamic_enum_flow(
        tmp_path, reference="steps.discover.output.allowed",
    ).steps[1]
    assert _resolve_step_schema_enums(
        step, outputs={}, flow_input={}, allow_unavailable=True,
    ) is None
    with pytest.raises(ValueError, match="not available"):
        _resolve_step_schema_enums(step, outputs={}, flow_input={})
    for value in ("[]", '"wrong"', '[{"nested":true}]'):
        with pytest.raises(ValueError, match="output_schema_enums"):
            _resolve_step_schema_enums(
                step, outputs={"discover": '{"allowed":' + value + "}"}, flow_input={},
            )


def test_dynamic_schema_enum_rejects_future_reference(tmp_path: Path):
    with pytest.raises(ValueError, match="non-prior step"):
        _dynamic_enum_flow(
            tmp_path, reference="steps.select.output.allowed",
        )


def test_dynamic_schema_enum_requires_matching_schema_property(tmp_path: Path):
    (tmp_path / "prompt.md").write_text("Return JSON", encoding="utf-8")
    (tmp_path / "schema.json").write_text('{"type":"object"}', encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        'version = 1\n[[steps]]\nid = "select"\nprompt_file = "prompt.md"\n'
        'response_format = "json"\noutput_schema = "schema.json"\n'
        '[steps.output_schema_enums]\n"/properties/missing" = "input.allowed"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="schema pointer"):
        load_flow(Path("flow.toml"), workspace=tmp_path)


def test_dynamic_schema_enums_validate_reused_checkpoint(tmp_path: Path):
    from cli_agent.workflows.flow import (
        _resolve_step_schema_enums, _existing_json_checkpoint,
    )
    step = _dynamic_enum_flow(
        tmp_path, reference="steps.discover.output.allowed",
    ).steps[1]
    concrete = _resolve_step_schema_enums(
        step, outputs={"discover": '{"allowed":["red"]}'}, flow_input={},
    )
    output = tmp_path / "selection.json"
    output.write_text('{"choice":"blue"}', encoding="utf-8")
    with pytest.raises(ValueError, match="does not match output_schema"):
        _existing_json_checkpoint(concrete, workspace=tmp_path, output=output)
    output.write_text('{"choice":"red"}', encoding="utf-8")
    assert _existing_json_checkpoint(concrete, workspace=tmp_path, output=output)


def test_dynamic_schema_enums_use_foreach_item(tmp_path: Path):
    from cli_agent.workflows.flow import _resolve_step_schema_enums
    step = _dynamic_enum_flow(
        tmp_path, reference="item.allowed",
        foreach="steps.discover.output.groups",
    ).steps[1]
    resolved = _resolve_step_schema_enums(
        step, outputs={}, flow_input={},
        item={"allowed": ["a", "b"]},
    )
    assert resolved.output_schema["properties"]["choice"]["enum"] == ["a", "b"]


def test_run_start_snapshot_checks_dynamic_enum_against_prior_checkpoint(tmp_path: Path):
    from cli_agent.workflows.flow import (
        _capture_run_start_invocation, _SubflowTraversalBudget,
    )
    flow = _dynamic_enum_flow(
        tmp_path, reference="steps.discover.output.allowed",
    )
    (tmp_path / "choices.json").write_text(
        '{"allowed":["red"]}', encoding="utf-8",
    )
    (tmp_path / "selection.json").write_text(
        '{"choice":"blue"}', encoding="utf-8",
    )
    with pytest.raises(ValueError, match="does not match output_schema"):
        _capture_run_start_invocation(
            flow, workspace=tmp_path, cache={},
            flow_input={}, namespace="", snapshots={},
            protected_paths={}, budget=_SubflowTraversalBudget(),
        )


def test_dynamic_schema_enums_from_declared_flow_input(tmp_path: Path):
    from cli_agent.workflows.flow import _resolve_step_schema_enums
    _dynamic_enum_flow(tmp_path, reference="steps.discover.output.allowed")
    source = tmp_path / "flow.toml"
    content = source.read_text(encoding="utf-8")
    content = content.replace(
        "version = 1\\n", 'version = 1\\ninputs = ["choices"]\\n', 1,
    )
    content = content.replace(
        "steps.discover.output.allowed", "input.choices",
    )
    source.write_text(content, encoding="utf-8")
    step = load_flow(Path("flow.toml"), workspace=tmp_path).steps[1]
    resolved = _resolve_step_schema_enums(
        step, outputs={}, flow_input={"choices": ["red", "blue"]},
    )
    assert resolved.output_schema["properties"]["choice"]["enum"] == ["red", "blue"]
