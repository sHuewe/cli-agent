"""Regression tests for deterministic checkpoint reuse with write-on-miss."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli_agent.workflows.flow as flow_module
from cli_agent.workflows.flow import load_flow, run_flow


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


@pytest.mark.parametrize("checkpoint_success", [False, True])
@pytest.mark.parametrize("depth", [1, 2])
def test_parent_retains_only_live_checkpoint_source_guards(
    tmp_path, monkeypatch, checkpoint_success, depth,
):
    """A completed miss releases the source across nested boundaries;
    a successful read stays protected even after the child has returned.
    """
    root = _make_flow(tmp_path)
    if checkpoint_success:
        (tmp_path / "old-v2.json").write_text(
            '{"status":"ok","source_path":"concept.md","findings":[]}',
            encoding="utf-8",
        )
    if depth == 2:
        (tmp_path / "middle.toml").write_text(
            'version = 1\nresult = "steps.child.output"\n'
            '[[steps]]\nid = "child"\nflow = "child.toml"\n'
            '[steps.input]\nsource = "concept.md"\n',
            encoding="utf-8",
        )
        middle = 'flow = "middle.toml"'
        inputs = ''
    else:
        middle = 'flow = "child.toml"'
        inputs = '[steps.input]\nsource = "concept.md"\n'
    root.write_text(
        'version = 1\n[[steps]]\nid = "nested"\n'
        + middle + '\n' + inputs
        + '[[steps]]\nid = "rewrite"\nprompt_file = "prompt.md"\n'
        'output = "concept.md"\noverwrite_output = true\n'
        'workspace_access = "write"\n[steps.vars]\nvalue = "rewrite"\n',
        encoding="utf-8",
    )
    called = []

    async def fake_run(options, *, dependencies=None):
        called.append(options.prompt)
        if options.prompt == "rewrite":
            assert (tmp_path / "concept.md") not in options.mutation_protected_paths
        return SimpleNamespace(
            answer='{"status":"ok","source_path":"concept.md","findings":[]}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run)
    flow = load_flow(root, workspace=tmp_path)
    if checkpoint_success:
        with pytest.raises(ValueError, match="reserviert|kollidiert"):
            asyncio.run(run_flow(flow, workspace=tmp_path))
        assert called == []
    else:
        asyncio.run(run_flow(flow, workspace=tmp_path))
        assert called == ["write", "verify", "rewrite"]


def test_parent_protects_unknown_future_checkpoint_before_writer(
    tmp_path, monkeypatch,
):
    """Before the child checks its source even a future miss must fail closed."""
    root = _make_flow(tmp_path)
    root.write_text(
        'version = 1\n[[steps]]\nid = "rewrite"\n'
        'prompt_file = "prompt.md"\noutput = "concept.md"\n'
        'overwrite_output = true\n[steps.vars]\nvalue = "rewrite"\n'
        '[[steps]]\nid = "child"\nflow = "child.toml"\n'
        '[steps.input]\nsource = "concept.md"\n',
        encoding="utf-8",
    )
    called = []

    async def fake_run(options, *, dependencies=None):
        called.append(options.prompt)
        raise AssertionError("Earlier writer must not run")

    monkeypatch.setattr(flow_module, "run_once", fake_run)
    with pytest.raises(ValueError, match="reserviert|kollidiert"):
        asyncio.run(run_flow(load_flow(root, workspace=tmp_path), workspace=tmp_path))
    assert called == []


def test_skipped_child_does_not_protect_source_from_later_writer(
    tmp_path, monkeypatch,
):
    root = _make_flow(tmp_path)
    root.write_text(
        'version = 1\n'
        '[[steps]]\nid = "switch"\nprompt_file = "prompt.md"\n'
        'response_format = "json"\n[steps.vars]\nvalue = "switch"\n'
        '[[steps]]\nid = "child"\nflow = "child.toml"\n'
        'when = { ref = "steps.switch.output.enabled", eq = true }\n'
        '[steps.input]\nsource = "concept.md"\n'
        '[[steps]]\nid = "rewrite"\nprompt_file = "prompt.md"\n'
        'output = "concept.md"\noverwrite_output = true\n'
        '[steps.vars]\nvalue = "rewrite"\n',
        encoding="utf-8",
    )
    called = []

    async def fake_run(options, *, dependencies=None):
        called.append(options.prompt)
        return SimpleNamespace(
            answer='{"enabled":false}' if options.prompt == "switch" else "edited",
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run)
    asyncio.run(run_flow(load_flow(root, workspace=tmp_path), workspace=tmp_path))
    assert called == ["switch", "rewrite"]


def test_completed_child_still_protects_verifier_checkpoint_file(
    tmp_path, monkeypatch,
):
    root = _make_flow(tmp_path)
    root.write_text(
        'version = 1\n[[steps]]\nid = "child"\nflow = "child.toml"\n'
        '[steps.input]\nsource = "concept.md"\n'
        '[[steps]]\nid = "clobber"\nprompt_file = "prompt.md"\n'
        'output = "old-v2.json"\noverwrite_output = true\n'
        '[steps.vars]\nvalue = "clobber"\n',
        encoding="utf-8",
    )
    async def fake_run(options, *, dependencies=None):
        return SimpleNamespace(
            answer='{"status":"ok","source_path":"concept.md"}',
            web_context_statuses=(),
        )
    monkeypatch.setattr(flow_module, "run_once", fake_run)
    with pytest.raises(ValueError, match="reserviert|kollidiert"):
        asyncio.run(run_flow(load_flow(root, workspace=tmp_path), workspace=tmp_path))


def test_completed_foreach_child_does_not_reapply_released_source_guard(
    tmp_path, monkeypatch,
):
    """The second sibling may write a source whose earlier reader finished."""
    (tmp_path / "prompt.md").write_text("{{var:value}}", encoding="utf-8")
    (tmp_path / "a.md").write_text("A", encoding="utf-8")
    (tmp_path / "b.md").write_text("B", encoding="utf-8")
    (tmp_path / "child.toml").write_text(
        'version = 1\ninputs = ["source", "key", "target"]\n'
        'result = "steps.resume.output.verification"\n'
        'result_fallback = "steps.verify.output"\n'
        '[[steps]]\nid = "resume"\n'
        'output = "state/${input.key}/status.json"\n'
        'overwrite_output = true\n[steps.filesystem_operation]\n'
        'kind = "checkpoint_status"\npaths = ["state/${input.key}/old.json"]\n'
        'expected_status = "ok"\nexpected_source_path = "${input.source}"\n'
        'release_source_on_miss = true\n'
        '[[steps]]\nid = "write"\nprompt_file = "prompt.md"\n'
        'workspace_access = "write"\n'
        'output = "${input.target}"\noverwrite_output = true\n'
        'when = { ref = "steps.resume.output.completed", eq = false }\n'
        '[steps.vars]\nvalue = "write"\n'
        '[[steps]]\nid = "verify"\nprompt_file = "prompt.md"\n'
        'response_format = "json"\n'
        'when = { ref = "steps.resume.output.completed", eq = false }\n'
        '[steps.vars]\nvalue = "verify"\n',
        encoding="utf-8",
    )
    (tmp_path / "flow.toml").write_text(
        'version = 1\n'
        '[[steps]]\nid = "discover"\nprompt_file = "prompt.md"\n'
        'response_format = "json"\n[steps.vars]\nvalue = "discover"\n'
        '[[steps]]\nid = "children"\nflow = "child.toml"\n'
        'foreach = "steps.discover.output.items"\n'
        '[steps.input]\nsource = "${item.source}"\n'
        'key = "${item.key}"\ntarget = "${item.target}"\n',
        encoding="utf-8",
    )
    called = []

    async def fake_run(options, *, dependencies=None):
        called.append(options.prompt)
        if options.prompt == "discover":
            answer = json.dumps({"items":[
                {"source":"a.md", "key":"first", "target":"a.md"},
                {"source":"b.md", "key":"second", "target":"a.md"},
            ]})
        else:
            if options.prompt == "write":
                assert (tmp_path / "a.md") not in options.mutation_protected_paths
            answer = '{"status":"ok","source_path":"concept.md"}'
        return SimpleNamespace(answer=answer, web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run)
    asyncio.run(run_flow(
        load_flow(tmp_path / "flow.toml", workspace=tmp_path),
        workspace=tmp_path,
    ))
    assert called == ["discover", "write", "verify", "write", "verify"]
