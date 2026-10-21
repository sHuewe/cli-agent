"""Regression tests for deterministic checkpoint reuse with write-on-miss."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli_agent.flow as flow_module
from cli_agent.flow import load_flow, run_flow


def _make_flow(root: Path) -> Path:
    (root / "prompt.md").write_text("{{var:value}}", encoding="utf-8")
    (root / "child.toml").write_text(
        """version = 1
inputs = ["source"]
result = "steps.resume.output.verification"
result_fallback = "steps.verify.output"

[[steps]]
id = "resume"
output = "resume.json"
overwrite_output = true
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["old-v2.json", "old-v3.json"]
expected_status = "ok"
expected_source_path = "${input.source}"
release_source_on_miss = true

[[steps]]
id = "write"
prompt_file = "prompt.md"
workspace_access = "write"
when = { ref = "steps.resume.output.completed", eq = false }
[steps.vars]
value = "write"

[[steps]]
id = "verify"
prompt_file = "prompt.md"
response_format = "json"
when = { ref = "steps.resume.output.completed", eq = false }
[steps.vars]
value = "verify"
""",
        encoding="utf-8",
    )
    root_flow = root / "flow.toml"
    root_flow.write_text(
        'version = 1\n[[steps]]\nid = "child"\nflow = "child.toml"\n'
        '[steps.input]\nsource = "concept.md"\n',
        encoding="utf-8",
    )
    (root / "concept.md").write_text("# Concept", encoding="utf-8")
    return root_flow


@pytest.mark.parametrize("valid", [True, False])
def test_guarded_legacy_reuse_or_write_on_miss(tmp_path, monkeypatch, valid):
    root = _make_flow(tmp_path)
    if valid:
        (tmp_path / "old-v2.json").write_text(
            '{"status":"ok","source_path":"concept.md","checked_links":1,"findings":[]}',
            encoding="utf-8",
        )
    called = []

    async def fake_run(options, *, dependencies=None):
        called.append(options.prompt)
        if options.prompt == "write":
            assert (tmp_path / "concept.md") not in options.mutation_protected_paths
        return SimpleNamespace(
            answer='{"status":"ok","source_path":"concept.md","checked_links":0,"findings":[]}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run)
    asyncio.run(run_flow(load_flow(root, workspace=tmp_path), workspace=tmp_path))
    assert called == ([] if valid else ["write", "verify"])
    status = json.loads((tmp_path / "resume.json").read_text(encoding="utf-8"))
    assert status["completed"] is valid
    assert status["verification"]["status"] == "ok" if valid else status["verification"] is None


def test_source_release_requires_first_unconditional_step(tmp_path):
    path = _make_flow(tmp_path)
    child = tmp_path / "child.toml"
    content = child.read_text(encoding="utf-8")
    child.write_text(content.replace('release_source_on_miss = true',
                                    'release_source_on_miss = false'), encoding="utf-8")
    with pytest.raises(ValueError, match="release_source_on_miss"):
        load_flow(child, workspace=tmp_path)


def test_fallback_result_requires_primary(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text(
        'version = 1\nresult_fallback = "steps.work.output"\n'
        '[[steps]]\nid = "work"\nprompt_file = "prompt.md"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="result_fallback"):
        load_flow(path, workspace=tmp_path)


def test_released_source_alias_stays_protected_when_also_checkpoint_candidate(
    tmp_path,
):
    path = tmp_path / "alias.toml"
    path.write_text(
        'version = 1\n[[steps]]\nid = "resume"\n'
        'output = "status.json"\noverwrite_output = true\n'
        '[steps.filesystem_operation]\nkind = "checkpoint_status"\n'
        'paths = ["same.json"]\nexpected_status = "ok"\n'
        'expected_source_path = "same.json"\nrelease_source_on_miss = true\n',
        encoding="utf-8",
    )
    flow = load_flow(path, workspace=tmp_path)
    reserved = {}
    flow_module._reserve_checkpoint_status_inputs(
        flow, workspace=tmp_path, flow_input={},
        reserved_inputs=reserved,
    )
    key = flow_module._filesystem_path_key(tmp_path / "same.json")
    assert key in reserved

    front = {}
    flow_module._reserve_conditional_checkpoint_status_inputs(
        flow, workspace=tmp_path, flow_input={},
        outputs={"resume": '{"completed":false}'},
        output_json={"resume": True}, statuses={"resume": "completed"},
        reserved_inputs=front, claimed_outputs={},
    )
    assert key in front


@pytest.mark.parametrize(
    ("primary_text", "primary_json", "expected_text", "expected_json"),
    [
        ("null", True, '{"status":"ok"}', True),
        ("null", False, "null", False),
        ('"null"', True, '"null"', True),
        ("false", True, "false", True),
        ("0", True, "0", True),
    ],
)
def test_whole_step_result_fallback_only_on_json_null(
    tmp_path, primary_text, primary_json, expected_text, expected_json,
):
    source = tmp_path / "fallback.toml"
    source.write_text(
        'version = 1\nresult = "steps.primary.output"\n'
        'result_fallback = "steps.secondary.output"\n'
        '[[steps]]\nid = "primary"\nprompt_file = "prompt.md"\n'
        '[[steps]]\nid = "secondary"\nprompt_file = "prompt.md"\n',
        encoding="utf-8",
    )
    flow = load_flow(source, workspace=tmp_path)
    result = flow_module._flow_result_value(
        flow,
        outputs={"primary": primary_text, "secondary": '{"status":"ok"}'},
        output_json={"primary": primary_json, "secondary": True},
    )
    assert result is not None
    assert result.text == expected_text
    assert result.is_json is expected_json


def test_null_primary_without_available_fallback_is_an_error(tmp_path):
    source = tmp_path / "fallback.toml"
    source.write_text(
        'version = 1\nresult = "steps.primary.output"\n'
        'result_fallback = "steps.secondary.output"\n'
        '[[steps]]\nid = "primary"\nprompt_file = "prompt.md"\n'
        '[[steps]]\nid = "secondary"\nprompt_file = "prompt.md"\n',
        encoding="utf-8",
    )
    flow = load_flow(source, workspace=tmp_path)
    with pytest.raises(ValueError, match="Kein verfügbarer Flow-Result-Selector"):
        flow_module._flow_result_value(
            flow, outputs={"primary": "null"}, output_json={"primary": True},
        )


def test_run_start_does_not_publish_fallback_before_primary_is_known(tmp_path):
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    path = tmp_path / "choice.toml"
    path.write_text(
        'version = 1\nresult = "steps.primary.output"\n'
        'result_fallback = "steps.fallback.output"\n'
        '[[steps]]\nid = "primary"\nprompt_file = "prompt.md"\n'
        'output = "primary.json"\nresponse_format = "json"\n'
        '[[steps]]\nid = "fallback"\nprompt_file = "prompt.md"\n'
        'output = "fallback.json"\nresponse_format = "json"\n',
        encoding="utf-8",
    )
    (tmp_path / "fallback.json").write_text('{"choice":"old"}', encoding="utf-8")
    flow = load_flow(path, workspace=tmp_path)
    snapshots = {}
    result = flow_module._capture_run_start_invocation(
        flow, workspace=tmp_path,
        cache={flow_module._filesystem_path_key(flow.source): flow},
        flow_input={}, namespace="", snapshots=snapshots,
        protected_paths={}, budget=flow_module._SubflowTraversalBudget(),
    )
    assert result is None
    assert "primary" not in snapshots[""].available_outputs
    assert "fallback" in snapshots[""].available_outputs


def test_oversize_embedded_verification_rejected_before_status_write(
    tmp_path, monkeypatch,
):
    root = _make_flow(tmp_path)
    payload = {
        "status": "ok",
        "source_path": "concept.md",
        "checked_links": 0,
        "findings": [],
        "notes": "x" * 110,
    }
    raw = json.dumps(payload)
    wrapper = json.dumps({
        "completed": True, "checkpoint": "old-v2.json",
        "source_path": "concept.md", "verification": payload,
    })
    assert len(raw.encode("utf-8")) < len(wrapper.encode("utf-8"))
    monkeypatch.setattr(
        flow_module, "MAX_STRUCTURED_OUTPUT_BYTES",
        (len(raw.encode("utf-8")) + len(wrapper.encode("utf-8"))) // 2,
    )
    (tmp_path / "old-v2.json").write_text(raw, encoding="utf-8")
    with pytest.raises(ValueError, match="checkpoint_status überschreitet"):
        asyncio.run(run_flow(load_flow(root, workspace=tmp_path), workspace=tmp_path))
    assert not (tmp_path / "resume.json").exists()
    assert (tmp_path / "old-v2.json").read_text(encoding="utf-8") == raw
