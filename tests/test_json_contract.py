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
