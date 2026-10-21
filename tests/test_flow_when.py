"""Conditional Flow step contract and runtime tests."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli_agent.flow as flow_module
from cli_agent.flow import load_flow, run_flow


def _setup(tmp_path: Path, steps: str, *, header: str = "") -> Path:
    (tmp_path / "do.md").write_text("{{var:value}}", encoding="utf-8")
    flow = tmp_path / "flow.toml"
    flow.write_text("version = 1\n" + header + "\n" + steps, encoding="utf-8")
    return flow


def _run(tmp_path: Path, flow: Path, monkeypatch, answers: dict[str, str]):
    called = []

    async def fake_run_once(options, *, dependencies=None):
        called.append(options.prompt)
        return SimpleNamespace(answer=answers.get(options.prompt, '{"ok":true}'),
                               web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    asyncio.run(run_flow(load_flow(flow, workspace=tmp_path), workspace=tmp_path))
    return called


def test_when_eq_skips_step_without_skipping_next(tmp_path, monkeypatch):
    flow = _setup(tmp_path, """
[[steps]]
id = "assess"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "assess"
[[steps]]
id = "only_split"
prompt_file = "do.md"
response_format = "json"
output = "skipped.json"
when = { ref = "steps.assess.output.action", eq = "split" }
[steps.vars]
value = "split"
[[steps]]
id = "final"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "final"
""")
    calls = _run(tmp_path, flow, monkeypatch,
                 {"assess": '{"action":"keep"}'})
    assert calls == ["assess", "final"]
    assert not (tmp_path / "skipped.json").exists()


def test_when_eq_true_executes_and_resume_uses_checkpoint(tmp_path, monkeypatch):
    flow = _setup(tmp_path, """
[[steps]]
id = "assess"
prompt_file = "do.md"
response_format = "json"
output = "assess.json"
[steps.vars]
value = "assess"
[[steps]]
id = "split"
prompt_file = "do.md"
response_format = "json"
output = "split.json"
when = { ref = "steps.assess.output.action", eq = "split" }
[steps.vars]
value = "split"
""")
    (tmp_path / "assess.json").write_text('{"action":"split"}', encoding="utf-8")
    calls = _run(tmp_path, flow, monkeypatch, {})
    assert calls == ["split"]


def test_when_in_exists_and_skipped_status(tmp_path, monkeypatch):
    flow = _setup(tmp_path, """
[[steps]]
id = "first"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "first"
[[steps]]
id = "skipped"
prompt_file = "do.md"
when = { ref = "steps.first.output.action", in = ["keep", "skip"] }
[steps.vars]
value = "skipped"
[[steps]]
id = "after"
prompt_file = "do.md"
when = { ref = "steps.skipped.status", eq = "completed" }
[steps.vars]
value = "after"
[[steps]]
id = "last"
prompt_file = "do.md"
when = { ref = "steps.skipped.status", eq = "skipped" }
[steps.vars]
value = "last"
""")
    calls = _run(tmp_path, flow, monkeypatch,
                 {"first": '{"action":"split"}'})
    assert calls == ["first", "last"]


def test_when_on_foreach_evaluates_each_item(tmp_path, monkeypatch):
    flow = _setup(tmp_path, """
[[steps]]
id = "list"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "list"
[[steps]]
id = "process"
prompt_file = "do.md"
response_format = "json"
foreach = "steps.list.output.items"
output = "part-${item.id}.json"
when = { ref = "item.action", eq = "split" }
[steps.vars]
value = "${item.id}"
""")
    calls = _run(tmp_path, flow, monkeypatch, {
        "list": '{"items":[{"id":"a","action":"keep"},{"id":"b","action":"split"}]}',
    })
    assert calls == ["list", "b"]
    assert not (tmp_path / "part-a.json").exists()


@pytest.mark.parametrize("bad", [
    '{ ref = "steps.future.output.x", eq = "x" }',
    '{ ref = "steps.assess.output.x", eq = "x", neq = "y" }',
    '{ ref = "steps.assess.output.x", in = [] }',
    '{ ref = "item.action", eq = "split" }',
    '{ ref = "steps.assess.status.some", eq = "x" }',
    '{ ref = "steps.assess.output.x", exists = "true" }',
])
def test_when_validation_rejects_invalid_conditions(tmp_path, bad):
    flow = _setup(tmp_path, f"""
[[steps]]
id = "assess"
prompt_file = "do.md"
[[steps]]
id = "later"
prompt_file = "do.md"
when = {bad}
""")
    with pytest.raises(ValueError, match="when"):
        load_flow(flow, workspace=tmp_path)


def test_when_all_any_and_input(tmp_path, monkeypatch):
    flow = _setup(tmp_path, """
[[steps]]
id = "one"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "one"
[[steps]]
id = "two"
prompt_file = "do.md"
when = { all = [
    { ref = "steps.one.output.ready", eq = true },
    { any = [
        { ref = "steps.one.output.mode", eq = "live" },
        { ref = "steps.one.output.mode", eq = "test" }
    ] }
] }
[steps.vars]
value = "two"
""")
    calls = _run(tmp_path, flow, monkeypatch,
                 {"one": '{"ready":true,"mode":"test"}'})
    assert calls == ["one", "two"]


def test_when_decimal_compares_json_number_with_toml_float(tmp_path, monkeypatch):
    definition = _setup(tmp_path, """
[[steps]]
id = "score"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "score"
[[steps]]
id = "matched"
prompt_file = "do.md"
when = { ref = "steps.score.output.score", eq = 0.5 }
[steps.vars]
value = "matched"
""")
    calls = _run(tmp_path, definition, monkeypatch, {"score": '{"score":0.5}'})
    assert calls == ["score", "matched"]


def test_when_status_guard_skips_missing_foreach_source(tmp_path, monkeypatch):
    definition = _setup(tmp_path, """
[[steps]]
id = "gate"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "gate"
[[steps]]
id = "source"
prompt_file = "do.md"
response_format = "json"
when = { ref = "steps.gate.output.enabled", eq = true }
[steps.vars]
value = "source"
[[steps]]
id = "dependent"
prompt_file = "do.md"
response_format = "json"
foreach = "steps.source.output.items"
when = { all = [
    { ref = "steps.source.status", eq = "completed" },
    { ref = "item.enabled", eq = true }
] }
[steps.vars]
value = "dependent"
""")
    calls = _run(tmp_path, definition, monkeypatch, {"gate": '{ "enabled": false }'})
    assert calls == ["gate"]


def test_when_all_short_circuits_missing_guarded_output():
    condition = {"all": [
        {"ref": "steps.source.output", "exists": True},
        {"ref": "steps.source.output.kind", "eq": "x"},
    ]}
    assert flow_module._evaluate_when(
        condition, outputs={}, statuses={"source": "skipped"}, flow_input={},
    ) is False


def test_when_any_short_circuits_missing_guarded_output():
    condition = {"any": [
        {"ref": "steps.source.status", "eq": "skipped"},
        {"ref": "steps.source.output.kind", "eq": "x"},
    ]}
    assert flow_module._evaluate_when(
        condition, outputs={}, statuses={"source": "skipped"}, flow_input={},
    ) is True


def test_when_unknown_group_preserves_three_valued_logic():
    condition = {"all": [
        {"ref": "item.enabled", "eq": True},
        {"ref": "steps.source.status", "eq": "completed"},
    ]}
    assert flow_module._evaluate_when(
        condition, outputs={}, statuses={"source": "skipped"},
        flow_input={}, allow_unknown=True,
    ) is False


def test_when_iteration_id_deferred_in_preflight():
    condition = {"ref": "iteration.id", "eq": "2"}
    assert flow_module._evaluate_when(
        condition, outputs={}, statuses={}, flow_input={},
        allow_unknown=True,
    ) is None
    assert flow_module._evaluate_when(
        condition, outputs={}, statuses={}, flow_input={},
        iteration_id="2",
    ) is True
    assert flow_module._evaluate_when(
        condition, outputs={}, statuses={}, flow_input={},
        iteration_id="1",
    ) is False


def test_when_iteration_id_in_deferred_in_preflight():
    condition = {"ref": "iteration.id", "in": ["2", "3"]}
    assert flow_module._evaluate_when(
        condition, outputs={}, statuses={}, flow_input={},
        allow_unknown=True,
    ) is None
    assert flow_module._evaluate_when(
        condition, outputs={}, statuses={}, flow_input={},
        iteration_id="3",
    ) is True


def test_when_missing_ref_after_unknown_item_is_deferred():
    condition = {"all": [
        {"ref": "item.enabled", "eq": True},
        {"ref": "steps.meta.output.optional", "eq": "x"},
    ]}
    assert flow_module._evaluate_when(
        condition, outputs={"meta": '{"other":"value"}'},
        statuses={"meta": "completed"}, flow_input={}, allow_unknown=True,
    ) is None
    assert flow_module._evaluate_when(
        condition, outputs={"meta": '{"other":"value"}'},
        statuses={"meta": "completed"}, flow_input={},
        item={"enabled": False},
    ) is False
    with pytest.raises(ValueError, match="optional"):
        flow_module._evaluate_when(
            condition, outputs={"meta": '{"other":"value"}'},
            statuses={"meta": "completed"}, flow_input={},
            item={"enabled": True},
        )


def test_when_missing_ref_without_prior_unknown_still_raises():
    condition = {"all": [
        {"ref": "steps.meta.output.optional", "eq": "x"},
        {"ref": "item.enabled", "eq": False},
    ]}
    with pytest.raises(ValueError, match="optional"):
        flow_module._evaluate_when(
            condition, outputs={"meta": '{"other":"value"}'},
            statuses={"meta": "completed"}, flow_input={},
            allow_unknown=True,
        )


def test_when_empty_filtered_subflow_is_skipped_in_snapshot(tmp_path, monkeypatch):
    (tmp_path / "child.toml").write_text(
        'version = 1\ninputs = ["value"]\nresult = "steps.work.output"\n'
        '[[steps]]\nid = "work"\nprompt_file = "do.md"\n'
        '[steps.vars]\nvalue = "${input.value}"\n',
        encoding="utf-8",
    )
    definition = _setup(tmp_path, """
[[steps]]
id = "source"
prompt_file = "do.md"
response_format = "json"
output = "source.json"
[steps.vars]
value = "source"
[[steps]]
id = "child_batch"
flow = "child.toml"
foreach = "steps.source.output.items"
when = { ref = "item.enabled", eq = true }
[steps.input]
value = "${item.name}"
[[steps]]
id = "after"
prompt_file = "do.md"
response_format = "json"
when = { ref = "steps.child_batch.status", eq = "skipped" }
output = "after.json"
[steps.vars]
value = "after"
""")
    (tmp_path / "source.json").write_text(
        '{"items":[{"name":"a","enabled":false}]}', encoding="utf-8",
    )
    # Verify the run-start snapshot, not a file the mocked run_once never writes.
    parsed = load_flow(definition, workspace=tmp_path)
    snapshots = {}
    flow_module._capture_run_start_invocation(
        parsed,
        workspace=tmp_path,
        cache={flow_module._filesystem_path_key(parsed.source): parsed},
        flow_input={},
        namespace="",
        snapshots=snapshots,
        protected_paths={},
        budget=flow_module._SubflowTraversalBudget(),
    )
    assert "source" in snapshots[""].available_outputs
    assert "child_batch" not in snapshots[""].available_outputs

    calls = _run(tmp_path, definition, monkeypatch, {})
    assert calls == ["after"]


def test_when_null_foreach_item_is_concrete_in_preflight():
    condition = {"ref": "item", "eq": "expected"}
    assert flow_module._evaluate_when(
        condition, outputs={}, statuses={}, flow_input={},
        allow_unknown=True,
    ) is None
    assert flow_module._evaluate_when(
        condition, outputs={}, statuses={}, flow_input={},
        item=None, iteration_id="1", allow_unknown=True,
    ) is False


def test_when_null_foreach_item_skips_subflow_preflight(tmp_path, monkeypatch):
    (tmp_path / "child.toml").write_text(
        'version = 1\ninputs = ["value"]\nresult = "steps.work.output"\n'
        '[[steps]]\nid = "work"\nprompt_file = "do.md"\n'
        'output = "shared.json"\nresponse_format = "json"\n'
        '[steps.vars]\nvalue = "${input.value}"\n',
        encoding="utf-8",
    )
    definition = _setup(tmp_path, """
[[steps]]
id = "source"
prompt_file = "do.md"
output = "source.json"
response_format = "json"
[steps.vars]
value = "source"
[[steps]]
id = "children"
flow = "child.toml"
foreach = "steps.source.output.items"
when = { ref = "item", eq = "selected" }
[steps.input]
value = "${iteration.id}"
""")
    (tmp_path / "source.json").write_text(
        '{"items":[null,"selected"]}', encoding="utf-8",
    )
    assert _run(tmp_path, definition, monkeypatch, {}) == ["2"]


def test_when_model_foreach_filters_before_run_start_path_checks(tmp_path, monkeypatch):
    definition = _setup(tmp_path, """
[[steps]]
id = "source"
prompt_file = "do.md"
output = "source.json"
response_format = "json"
[steps.vars]
value = "source"
[[steps]]
id = "filtered"
prompt_file = "do.md"
foreach = "steps.source.output.items"
output = "shared.json"
response_format = "json"
when = { ref = "item.keep", eq = true }
[steps.vars]
value = "${item.name}"
""")
    (tmp_path / "source.json").write_text(
        '{"items":[{"name":"a","keep":false},{"name":"b","keep":true}]}',
        encoding="utf-8",
    )
    assert _run(tmp_path, definition, monkeypatch, {}) == ["b"]


def test_checkpoint_status_skips_completed_link_and_rechecks(tmp_path, monkeypatch):
    flow = _setup(tmp_path, """
[[steps]]
id = "resume"
output = "resume.json"
overwrite_output = true
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["verify-v2.json", "verify-v3.json"]
expected_status = "ok"
expected_source_path = "concept.md"
[[steps]]
id = "link"
prompt_file = "do.md"
when = { ref = "steps.resume.output.completed", eq = false }
[steps.vars]
value = "link"
""")
    (tmp_path / "verify-v2.json").write_text(
        '{"status":"ok","source_path":"concept.md"}', encoding="utf-8",
    )
    (tmp_path / "concept.md").write_text("# Verified concept", encoding="utf-8")
    assert _run(tmp_path, flow, monkeypatch, {}) == []
    import json
    assert json.loads((tmp_path / "resume.json").read_text())["completed"] is True
    (tmp_path / "verify-v2.json").unlink()
    assert _run(tmp_path, flow, monkeypatch, {}) == ["link"]
    assert json.loads((tmp_path / "resume.json").read_text())["completed"] is False


def test_checkpoint_status_rejects_invalid_or_unrelated_results(tmp_path, monkeypatch):
    flow = _setup(tmp_path, """
[[steps]]
id = "resume"
output = "resume.json"
overwrite_output = true
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["broken.json", "wrong.json"]
expected_status = "ok"
expected_source_path = "concept.md"
""")
    (tmp_path / "broken.json").write_text("not json", encoding="utf-8")
    (tmp_path / "wrong.json").write_text(
        '{"status":"ok","source_path":"other.md"}', encoding="utf-8",
    )
    assert _run(tmp_path, flow, monkeypatch, {}) == []
    import json
    assert json.loads((tmp_path / "resume.json").read_text())["completed"] is False


def test_checkpoint_status_requires_fresh_output(tmp_path):
    flow = _setup(tmp_path, """
[[steps]]
id = "resume"
output = "resume.json"
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["verify.json"]
expected_status = "ok"
expected_source_path = "concept.md"
""")
    with pytest.raises(ValueError, match="overwrite_output"):
        load_flow(flow, workspace=tmp_path)


@pytest.mark.parametrize("output", ["verify.json", "concept.md"])
def test_checkpoint_status_never_overwrites_inputs(tmp_path, monkeypatch, output):
    flow = _setup(tmp_path, f"""
[[steps]]
id = "resume"
output = "{output}"
overwrite_output = true
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["verify.json"]
expected_status = "ok"
expected_source_path = "concept.md"
""")
    original_checkpoint = '{"status":"ok","source_path":"concept.md"}'
    original_source = "# Important concept"
    (tmp_path / "verify.json").write_text(original_checkpoint, encoding="utf-8")
    (tmp_path / "concept.md").write_text(original_source, encoding="utf-8")
    with pytest.raises(ValueError, match="überschreiben|reserviert|kollidiert"):
        _run(tmp_path, flow, monkeypatch, {})
    assert (tmp_path / "verify.json").read_text(encoding="utf-8") == original_checkpoint
    assert (tmp_path / "concept.md").read_text(encoding="utf-8") == original_source


@pytest.mark.parametrize("checkpoint_first", [True, False])
@pytest.mark.parametrize("protected_name", ["verify.json", "concept.md"])
def test_checkpoint_status_reserves_inputs_against_other_steps(
    tmp_path, monkeypatch, checkpoint_first, protected_name,
):
    check = """
[[steps]]
id = "resume"
output = "resume.json"
overwrite_output = true
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["verify.json"]
expected_status = "ok"
expected_source_path = "concept.md"
"""
    writer = f"""
[[steps]]
id = "writer"
prompt_file = "do.md"
output = "{protected_name}"
overwrite_output = true
[steps.vars]
value = "writer"
"""
    flow = _setup(tmp_path, check + writer if checkpoint_first else writer + check)
    original_checkpoint = '{"status":"ok","source_path":"concept.md"}'
    original_source = "# Original concept"
    (tmp_path / "verify.json").write_text(original_checkpoint, encoding="utf-8")
    (tmp_path / "concept.md").write_text(original_source, encoding="utf-8")
    called = []

    async def should_not_run(*args, **kwargs):
        called.append(True)
        raise AssertionError("No model step may execute before collision detection")

    monkeypatch.setattr(flow_module, "run_once", should_not_run)
    with pytest.raises(ValueError, match="reserviert|kollidiert"):
        asyncio.run(run_flow(load_flow(flow, workspace=tmp_path), workspace=tmp_path))
    assert called == []
    assert (tmp_path / "verify.json").read_text(encoding="utf-8") == original_checkpoint
    assert (tmp_path / "concept.md").read_text(encoding="utf-8") == original_source


def test_checkpoint_status_reserves_child_input_before_parent_writer(
    tmp_path, monkeypatch,
):
    (tmp_path / "child.toml").write_text("""
version = 1
inputs = ["concept"]
result = "steps.resume.output"
[[steps]]
id = "resume"
output = "resume.json"
overwrite_output = true
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["verify.json"]
expected_status = "${input.concept}"
expected_source_path = "${input.concept}"
""", encoding="utf-8")
    flow = _setup(tmp_path, """
[[steps]]
id = "writer"
prompt_file = "do.md"
output = "concept.md"
overwrite_output = true
[steps.vars]
value = "writer"
[[steps]]
id = "child"
flow = "child.toml"
[steps.input]
concept = "concept.md"
""")
    (tmp_path / "concept.md").write_text("# Original", encoding="utf-8")
    with pytest.raises(ValueError, match="reserviert|kollidiert"):
        _run(tmp_path, flow, monkeypatch, {})
    assert (tmp_path / "concept.md").read_text(encoding="utf-8") == "# Original"


def test_checkpoint_status_inputs_are_protected_from_model_tool_writes(
    tmp_path, monkeypatch,
):
    flow = _setup(tmp_path, """
[[steps]]
id = "writer"
prompt_file = "do.md"
workspace_access = "write"
[steps.vars]
value = "writer"
[[steps]]
id = "resume"
output = "resume.json"
overwrite_output = true
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["verify.json"]
expected_status = "ok"
expected_source_path = "concept.md"
""")
    protected = []

    async def inspect_protection(options, *, dependencies=None):
        protected.append({
            path.resolve() for path in options.mutation_protected_paths
        })
        return SimpleNamespace(answer='{"ok":true}', web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", inspect_protection)
    asyncio.run(run_flow(load_flow(flow, workspace=tmp_path), workspace=tmp_path))
    assert len(protected) == 1
    assert (tmp_path / "verify.json") in protected[0]
    assert (tmp_path / "concept.md") in protected[0]


def test_checkpoint_status_dynamic_child_reserves_before_intervening_writer(
    tmp_path, monkeypatch,
):
    (tmp_path / "child.toml").write_text(
        'version = 1\ninputs = ["concept"]\nresult = "steps.resume.output"\n'
        '[[steps]]\nid = "resume"\noutput = "status.json"\n'
        'overwrite_output = true\n[steps.filesystem_operation]\n'
        'kind = "checkpoint_status"\npaths = ["verify.json"]\n'
        'expected_status = "ok"\n'
        'expected_source_path = "${input.concept}"\n',
        encoding="utf-8",
    )
    definition = _setup(tmp_path, """
[[steps]]
id = "discover"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "discover"
[[steps]]
id = "writer"
prompt_file = "do.md"
output = "concept.md"
overwrite_output = true
[steps.vars]
value = "writer"
[[steps]]
id = "child"
flow = "child.toml"
[steps.input]
concept = "${steps.discover.output.path}"
""")
    (tmp_path / "concept.md").write_text("# Original", encoding="utf-8")
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options.prompt)
        return SimpleNamespace(answer='{"path":"concept.md"}',
                               web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    with pytest.raises(ValueError, match="reserviert|kollidiert"):
        asyncio.run(run_flow(load_flow(definition, workspace=tmp_path), workspace=tmp_path))
    assert calls == ["discover"]
    assert (tmp_path / "concept.md").read_text(encoding="utf-8") == "# Original"


def test_known_true_subflow_does_not_trigger_skipped_foreach_preflight(
    tmp_path, monkeypatch,
):
    (tmp_path / "child.toml").write_text(
        'version = 1\nresult = "steps.work.output"\n'
        '[[steps]]\nid = "work"\nprompt_file = "do.md"\n'
        'response_format = "json"\noutput = "work.json"\n'
        '[steps.vars]\nvalue = "work"\n',
        encoding="utf-8",
    )
    definition = _setup(tmp_path, """
[[steps]]
id = "source"
prompt_file = "do.md"
response_format = "json"
output = "source.json"
[steps.vars]
value = "source"
[[steps]]
id = "child"
flow = "child.toml"
[[steps]]
id = "skipped_batch"
prompt_file = "do.md"
response_format = "json"
foreach = "steps.source.output.items"
output = "shared.json"
when = { ref = "steps.child.status", eq = "skipped" }
[steps.vars]
value = "${item}"
""")
    (tmp_path / "source.json").write_text('{"items":[1,2]}', encoding="utf-8")
    assert _run(tmp_path, definition, monkeypatch, {}) == ["work"]


def test_future_checkpoint_reservation_uses_fresh_budget_per_scan(
    tmp_path, monkeypatch,
):
    (tmp_path / "child.toml").write_text(
        'version = 1\ninputs = ["concept"]\nresult = "steps.resume.output"\n'
        '[[steps]]\nid = "resume"\noutput = "resume.json"\n'
        'overwrite_output = true\n[steps.filesystem_operation]\n'
        'kind = "checkpoint_status"\npaths = ["verify.json"]\n'
        'expected_status = "ok"\n'
        'expected_source_path = "${input.concept}"\n',
        encoding="utf-8",
    )
    definition = _setup(tmp_path, """
[[steps]]
id = "child"
flow = "child.toml"
[steps.input]
concept = "concept.md"
""")
    root = load_flow(definition, workspace=tmp_path)
    # Root and concrete child each consume one expansion per scan. A shared
    # budget would exceed this limit after the first scan.
    monkeypatch.setattr(flow_module, "MAX_SUBFLOW_GRAPH_EXPANSIONS", 2)
    reserved = {}
    cache = {flow_module._filesystem_path_key(root.source): root}
    for _ in range(12):
        flow_module._reserve_future_child_checkpoint_inputs(
            root, workspace=tmp_path, cache=cache,
            flow_input={}, outputs={}, output_json={}, statuses={},
            reserved_inputs=reserved, claimed_outputs={},
        )
    assert flow_module._filesystem_path_key(tmp_path / "verify.json") in reserved
    assert flow_module._filesystem_path_key(tmp_path / "concept.md") in reserved

    # Each individual scan remains bounded, including nested descendants.
    monkeypatch.setattr(flow_module, "MAX_SUBFLOW_GRAPH_EXPANSIONS", 1)
    with pytest.raises(ValueError, match="Expansionslimit"):
        flow_module._reserve_future_child_checkpoint_inputs(
            root, workspace=tmp_path, cache=cache,
            flow_input={}, outputs={}, output_json={}, statuses={},
            reserved_inputs={}, claimed_outputs={},
        )


@pytest.mark.parametrize(
    ("items", "condition", "expected"),
    [
        ('["a","b"]', None, "completed"),
        ('[]', None, "completed"),
        ('[{"keep":false}]', {"ref": "item.keep", "eq": True}, "skipped"),
        ('[{"keep":true},{"keep":false}]', {"ref": "item.keep", "eq": True}, "completed"),
    ],
)
def test_known_foreach_status_from_checkpoint(items, condition, expected, tmp_path):
    import json
    definition = _setup(tmp_path, """
[[steps]]
id = "source"
prompt_file = "do.md"
response_format = "json"
output = "source.json"
[steps.vars]
value = "source"
[[steps]]
id = "work"
prompt_file = "do.md"
foreach = "steps.source.output.items"
[steps.vars]
value = "${item}"
""")
    flow = load_flow(definition, workspace=tmp_path)
    step = flow.steps[1]
    if condition is not None:
        from dataclasses import replace
        step = replace(step, when=condition)
    statuses = {"source": "completed"}
    result = flow_module._known_foreach_execution_status(
        step, outputs={"source": json.dumps({"items": json.loads(items)})},
        statuses=statuses, flow_input={}, condition=True if condition is None else None,
    )
    assert result == expected


def test_checkpoint_backed_foreach_completed_skips_later_colliding_batch(
    tmp_path, monkeypatch,
):
    definition = _setup(tmp_path, """
[[steps]]
id = "source"
prompt_file = "do.md"
response_format = "json"
output = "source.json"
[steps.vars]
value = "source"
[[steps]]
id = "gate"
prompt_file = "do.md"
foreach = "steps.source.output.items"
[steps.vars]
value = "gate"
[[steps]]
id = "never"
prompt_file = "do.md"
response_format = "json"
foreach = "steps.source.output.items"
output = "duplicate.json"
when = { ref = "steps.gate.status", eq = "skipped" }
[steps.vars]
value = "never"
""")
    (tmp_path / "source.json").write_text('{"items":["a","b"]}', encoding="utf-8")
    assert _run(tmp_path, definition, monkeypatch, {}) == ["gate", "gate"]


def test_checkpoint_backed_foreach_unknown_source_stays_unresolved(
    tmp_path,
):
    definition = _setup(tmp_path, """
[[steps]]
id = "source"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "source"
[[steps]]
id = "gate"
prompt_file = "do.md"
foreach = "steps.source.output.items"
[steps.vars]
value = "gate"
""")
    gate = load_flow(definition, workspace=tmp_path).steps[1]
    assert flow_module._known_foreach_execution_status(
        gate, outputs={}, statuses={}, flow_input={}, condition=True,
    ) is None


def test_completed_without_checkpoint_proves_root_output_presence_only():
    outputs = {}
    statuses = {"gate": "completed"}
    kwargs = dict(outputs=outputs, statuses=statuses, flow_input={}, allow_unknown=True)
    assert flow_module._evaluate_when(
        {"ref": "steps.gate.output", "exists": True}, **kwargs,
    ) is True
    assert flow_module._evaluate_when(
        {"ref": "steps.gate.output", "exists": False}, **kwargs,
    ) is False
    assert flow_module._evaluate_when(
        {"ref": "steps.gate.output", "eq": "anything"}, **kwargs,
    ) is None
    assert flow_module._evaluate_when(
        {"ref": "steps.gate.output.value", "exists": False}, **kwargs,
    ) is None
    assert flow_module._evaluate_when(
        {"ref": "steps.gate.output.value", "eq": "anything"}, **kwargs,
    ) is None
    assert flow_module._evaluate_when(
        {"ref": "steps.gate.output", "exists": False},
        outputs={}, statuses={}, flow_input={}, allow_unknown=True,
    ) is None
    assert flow_module._evaluate_when(
        {"ref": "steps.gate.output", "exists": False},
        outputs={}, statuses={"gate": "skipped"},
        flow_input={}, allow_unknown=True,
    ) is True


def test_known_completed_root_output_avoids_false_foreach_collision(tmp_path, monkeypatch):
    definition = _setup(tmp_path, """
[[steps]]
id = "source"
prompt_file = "do.md"
response_format = "json"
output = "source.json"
[steps.vars]
value = "source"
[[steps]]
id = "gate"
prompt_file = "do.md"
[steps.vars]
value = "gate"
[[steps]]
id = "never"
prompt_file = "do.md"
response_format = "json"
foreach = "steps.source.output.items"
output = "duplicate.json"
when = { ref = "steps.gate.output", exists = false }
[steps.vars]
value = "never"
""")
    (tmp_path / "source.json").write_text('{"items":[1,2]}', encoding="utf-8")
    assert _run(tmp_path, definition, monkeypatch, {}) == ["gate"]


@pytest.mark.parametrize(
    ("raw", "expected", "json_mode"),
    [
        ("true", True, True),
        ("1", 1, True),
        ('"ready"', "ready", True),
        ("ready", "ready", False),
    ],
)
def test_when_root_output_typed_json_scalars(raw, expected, json_mode):
    assert flow_module._evaluate_when(
        {"ref": "steps.source.output", "eq": expected},
        outputs={"source": raw}, output_json={"source": json_mode},
        statuses={"source": "completed"}, flow_input={},
    ) is True
    if json_mode and raw == '"ready"':
        assert flow_module._evaluate_when(
            {"ref": "steps.source.output", "eq": raw},
            outputs={"source": raw}, output_json={"source": True},
            statuses={"source": "completed"}, flow_input={},
        ) is False


def test_conditional_static_outputs_can_share_a_path(tmp_path, monkeypatch):
    definition = _setup(tmp_path, """
[[steps]]
id = "source"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "source"
[[steps]]
id = "yes"
prompt_file = "do.md"
output = "shared.txt"
when = { ref = "steps.source.output.enabled", eq = true }
[steps.vars]
value = "yes"
[[steps]]
id = "no"
prompt_file = "do.md"
output = "shared.txt"
when = { ref = "steps.source.output.enabled", eq = false }
[steps.vars]
value = "no"
""")
    assert _run(
        tmp_path, definition, monkeypatch,
        {"source": '{"enabled":true}'},
    ) == ["source", "yes"]


def test_when_typed_root_json_scalar_selects_branch(tmp_path, monkeypatch):
    definition = _setup(tmp_path, """
[[steps]]
id = "source"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "source"
[[steps]]
id = "selected"
prompt_file = "do.md"
when = { ref = "steps.source.output", eq = true }
[steps.vars]
value = "selected"
""")
    assert _run(tmp_path, definition, monkeypatch, {"source": "true"}) == [
        "source", "selected",
    ]


@pytest.mark.parametrize("both_active", [False, True])
def test_nested_unknown_conditional_writers_wait_for_runtime_choice(
    tmp_path, monkeypatch, both_active,
):
    """Unknown child conditions must not reserve mutually exclusive outputs.

    Conversely, if both branches execute, the runtime must still reject the
    second writer instead of allowing two non-overwriting output claims.
    """
    (tmp_path / "child.toml").write_text("""
version = 1
result = "steps.final.output"
[[steps]]
id = "choice"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "choice"
[[steps]]
id = "first"
prompt_file = "do.md"
output = "shared.txt"
when = { ref = "steps.choice.output.enabled", eq = true }
[steps.vars]
value = "first"
[[steps]]
id = "second"
prompt_file = "do.md"
output = "shared.txt"
when = { ref = "steps.choice.output.enabled", eq = """ + ("true" if both_active else "false") + """ }
[steps.vars]
value = "second"
[[steps]]
id = "final"
prompt_file = "do.md"
[steps.vars]
value = "final"
""", encoding="utf-8")
    definition = _setup(tmp_path, """
[[steps]]
id = "child"
flow = "child.toml"
""")
    if both_active:
        with pytest.raises(ValueError, match="bereits|kollidiert|existiert"):
            _run(tmp_path, definition, monkeypatch, {"choice": '{"enabled":true}'})
    else:
        assert _run(
            tmp_path, definition, monkeypatch, {"choice": '{"enabled":true}'},
        ) == ["choice", "first", "final"]


@pytest.mark.parametrize("enabled", [False, True])
def test_conditional_checkpoint_inputs_only_block_active_branch(
    tmp_path, monkeypatch, enabled,
):
    definition = _setup(tmp_path, """
[[steps]]
id = "selector"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "selector"
[[steps]]
id = "verify"
output = "status.json"
overwrite_output = true
when = { ref = "steps.selector.output.enabled", eq = true }
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["verify.json"]
expected_status = "ok"
expected_source_path = "concept.md"
[[steps]]
id = "rewrite"
prompt_file = "do.md"
output = "verify.json"
overwrite_output = true
when = { ref = "steps.selector.output.enabled", eq = false }
[steps.vars]
value = "rewrite"
""")
    original = '{"status":"ok","source_path":"concept.md"}'
    (tmp_path / "verify.json").write_text(original, encoding="utf-8")
    (tmp_path / "concept.md").write_text("# Source", encoding="utf-8")
    calls = _run(
        tmp_path, definition, monkeypatch,
        {"selector": '{"enabled":' + str(enabled).lower() + '}'},
    )
    assert calls == (["selector"] if enabled else ["selector", "rewrite"])
    if enabled:
        assert (tmp_path / "verify.json").read_text(encoding="utf-8") == original
        assert (tmp_path / "status.json").exists()
    else:
        assert not (tmp_path / "status.json").exists()


def test_unresolved_conditional_checkpoint_protects_earlier_writer(
    tmp_path, monkeypatch,
):
    definition = _setup(tmp_path, """
[[steps]]
id = "rewrite"
prompt_file = "do.md"
output = "verify.json"
overwrite_output = true
[steps.vars]
value = "rewrite"
[[steps]]
id = "selector"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "selector"
[[steps]]
id = "verify"
output = "status.json"
overwrite_output = true
when = { ref = "steps.selector.output.enabled", eq = true }
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["verify.json"]
expected_status = "ok"
expected_source_path = "concept.md"
""")
    (tmp_path / "verify.json").write_text(
        '{"status":"ok","source_path":"concept.md"}', encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reserviert|kollidiert"):
        _run(tmp_path, definition, monkeypatch, {"selector": '{"enabled":true}'})


def test_unknown_parent_checkpoint_branch_protects_against_child_write(
    tmp_path, monkeypatch,
):
    (tmp_path / "child.toml").write_text(
        'version = 1\nresult = "steps.write.output"\n'
        '[[steps]]\nid = "write"\nprompt_file = "do.md"\n'
        'output = "verify.json"\noverwrite_output = true\n'
        '[steps.vars]\nvalue = "write"\n',
        encoding="utf-8",
    )
    definition = _setup(tmp_path, """
[[steps]]
id = "child"
flow = "child.toml"
[[steps]]
id = "selector"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "selector"
[[steps]]
id = "verify"
output = "status.json"
overwrite_output = true
when = { ref = "steps.selector.output.enabled", eq = true }
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["verify.json"]
expected_status = "ok"
expected_source_path = "concept.md"
""")
    original = '{"status":"ok","source_path":"concept.md"}'
    (tmp_path / "verify.json").write_text(original, encoding="utf-8")
    (tmp_path / "concept.md").write_text("# Concept", encoding="utf-8")
    with pytest.raises(ValueError, match="reserviert|kollidiert"):
        _run(tmp_path, definition, monkeypatch, {})
    assert (tmp_path / "verify.json").read_text(encoding="utf-8") == original


@pytest.mark.parametrize("source_kind", ["missing", "directory", "file"])
def test_checkpoint_status_requires_real_regular_source(
    tmp_path, monkeypatch, source_kind,
):
    definition = _setup(tmp_path, """
[[steps]]
id = "verify"
output = "resume.json"
overwrite_output = true
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["verification.json"]
expected_status = "ok"
expected_source_path = "concept.md"
""")
    (tmp_path / "verification.json").write_text(
        '{"status":"ok","source_path":"concept.md"}', encoding="utf-8",
    )
    if source_kind == "directory":
        (tmp_path / "concept.md").mkdir()
    elif source_kind == "file":
        (tmp_path / "concept.md").write_text("# Concept", encoding="utf-8")

    _run(tmp_path, definition, monkeypatch, {})
    import json
    result = json.loads((tmp_path / "resume.json").read_text(encoding="utf-8"))
    assert result["completed"] is (source_kind == "file")
    assert result["checkpoint"] == (
        "verification.json" if source_kind == "file" else None
    )


@pytest.mark.parametrize("enabled", [False, True])
def test_child_recomputes_own_conditional_checkpoint_reservations(
    tmp_path, monkeypatch, enabled,
):
    """A child's false conditional reader must not remain inherited from its parent."""
    (tmp_path / "child.toml").write_text("""
version = 1
result = "steps.final.output"
[[steps]]
id = "selector"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "selector"
[[steps]]
id = "verify"
output = "status.json"
overwrite_output = true
when = { ref = "steps.selector.output.enabled", eq = true }
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["verify.json"]
expected_status = "ok"
expected_source_path = "concept.md"
[[steps]]
id = "rewrite"
prompt_file = "do.md"
output = "verify.json"
overwrite_output = true
when = { ref = "steps.selector.output.enabled", eq = false }
[steps.vars]
value = "rewrite"
[[steps]]
id = "final"
prompt_file = "do.md"
[steps.vars]
value = "final"
""", encoding="utf-8")
    flow = _setup(tmp_path, """
[[steps]]
id = "child"
flow = "child.toml"
""")
    original = '{"status":"ok","source_path":"concept.md"}'
    (tmp_path / "verify.json").write_text(original, encoding="utf-8")
    (tmp_path / "concept.md").write_text("# Concept", encoding="utf-8")
    calls = _run(tmp_path, flow, monkeypatch, {
        "selector": '{"enabled":' + str(enabled).lower() + '}',
    })
    assert calls == (["selector", "final"] if enabled
                     else ["selector", "rewrite", "final"])
    if enabled:
        assert (tmp_path / "verify.json").read_text(encoding="utf-8") == original
    else:
        assert not (tmp_path / "status.json").exists()


def test_run_start_reserves_deep_checkpoint_source_from_child_checkpoint(
    tmp_path, monkeypatch,
):
    """A parent writer must not destroy a deep child's checkpoint source."""
    (tmp_path / "deep.toml").write_text("""
version = 1
inputs = ["concept"]
result = "steps.verify.output"
[[steps]]
id = "verify"
output = "status.json"
overwrite_output = true
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["verify.json"]
expected_status = "ok"
expected_source_path = "${input.concept}"
""", encoding="utf-8")
    (tmp_path / "child.toml").write_text("""
version = 1
result = "steps.deep.output"
[[steps]]
id = "selector"
prompt_file = "do.md"
response_format = "json"
output = "selector.json"
[steps.vars]
value = "selector"
[[steps]]
id = "deep"
flow = "deep.toml"
[steps.input]
concept = "${steps.selector.output.path}"
""", encoding="utf-8")
    definition = _setup(tmp_path, """
[[steps]]
id = "writer"
prompt_file = "do.md"
output = "concept.md"
overwrite_output = true
[steps.vars]
value = "writer"
[[steps]]
id = "child"
flow = "child.toml"
""")
    (tmp_path / "selector.json").write_text(
        '{"path":"concept.md"}', encoding="utf-8",
    )
    (tmp_path / "concept.md").write_text("# Original", encoding="utf-8")
    (tmp_path / "verify.json").write_text(
        '{"status":"ok","source_path":"concept.md"}', encoding="utf-8",
    )
    with pytest.raises(ValueError, match="reserviert|kollidiert"):
        _run(tmp_path, definition, monkeypatch, {})
    assert (tmp_path / "concept.md").read_text(encoding="utf-8") == "# Original"


def test_unknown_gated_checkpoint_not_published_to_sibling_preflight(
    tmp_path, monkeypatch,
):
    """Speculative checkpoints must not make guarded sibling writers active."""
    (tmp_path / "child.toml").write_text("""
version = 1
inputs = ["name"]
result = "steps.final.output"
[[steps]]
id = "selector"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "selector-${input.name}"
[[steps]]
id = "gated"
prompt_file = "do.md"
response_format = "json"
output = "gated.json"
when = { ref = "steps.selector.output.enabled", eq = true }
[steps.vars]
value = "gated"
[[steps]]
id = "after"
prompt_file = "do.md"
output = "shared.txt"
when = { ref = "steps.gated.status", eq = "completed" }
[steps.vars]
value = "after"
[[steps]]
id = "final"
prompt_file = "do.md"
[steps.vars]
value = "final-${input.name}"
""", encoding="utf-8")
    definition = _setup(tmp_path, """
[[steps]]
id = "source"
prompt_file = "do.md"
response_format = "json"
output = "source.json"
[steps.vars]
value = "source"
[[steps]]
id = "children"
flow = "child.toml"
foreach = "steps.source.output.items"
[steps.input]
name = "${item}"
""")
    (tmp_path / "source.json").write_text(
        '{"items":["a","b"]}', encoding="utf-8",
    )
    (tmp_path / "gated.json").write_text('{"old":true}', encoding="utf-8")
    calls = _run(tmp_path, definition, monkeypatch, {
        "selector-a": '{"enabled":false}',
        "selector-b": '{"enabled":false}',
    })
    assert calls == ["selector-a", "final-a", "selector-b", "final-b"]
    assert not (tmp_path / "shared.txt").exists()
    assert (tmp_path / "gated.json").read_text(encoding="utf-8") == '{"old":true}'


def test_unknown_foreach_child_gate_does_not_permanently_reserve_checkpoint(
    tmp_path, monkeypatch,
):
    (tmp_path / "child.toml").write_text("""
version = 1
inputs = ["source"]
result = "steps.verify.output"
[[steps]]
id = "verify"
output = "status.json"
overwrite_output = true
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["verify.json"]
expected_status = "ok"
expected_source_path = "${input.source}"
""", encoding="utf-8")
    definition = _setup(tmp_path, """
[[steps]]
id = "source"
prompt_file = "do.md"
response_format = "json"
output = "source.json"
[steps.vars]
value = "source"
[[steps]]
id = "selector"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "selector"
[[steps]]
id = "writer"
prompt_file = "do.md"
output = "concept.md"
overwrite_output = true
when = { ref = "steps.selector.output.allow", eq = false }
[steps.vars]
value = "writer"
[[steps]]
id = "child"
flow = "child.toml"
foreach = "steps.source.output.items"
when = { all = [
    { ref = "steps.selector.output.allow", eq = true },
    { ref = "item.keep", eq = true }
] }
[steps.input]
source = "concept.md"
""")
    (tmp_path / "source.json").write_text(
        '{"items":[{"keep":true}]}', encoding="utf-8",
    )
    (tmp_path / "concept.md").write_text("# Original", encoding="utf-8")
    assert _run(
        tmp_path, definition, monkeypatch,
        {"selector": '{"allow":false}'},
    ) == ["selector", "writer"]


def test_snapshot_publishes_resolved_item_filtered_checkpoints(tmp_path):
    definition = _setup(tmp_path, """
[[steps]]
id = "source"
prompt_file = "do.md"
response_format = "json"
output = "source.json"
[steps.vars]
value = "source"
[[steps]]
id = "filtered"
prompt_file = "do.md"
response_format = "json"
foreach = "steps.source.output.items"
output = "filtered-${item.id}.json"
when = { ref = "item.keep", eq = true }
[steps.vars]
value = "filtered"
""")
    (tmp_path / "source.json").write_text(
        '{"items":[{"id":"a","keep":false},{"id":"b","keep":true}]}',
        encoding="utf-8",
    )
    (tmp_path / "filtered-b.json").write_text('{"ok":true}', encoding="utf-8")
    parsed = load_flow(definition, workspace=tmp_path)
    snapshots = {}
    flow_module._capture_run_start_invocation(
        parsed, workspace=tmp_path,
        cache={flow_module._filesystem_path_key(parsed.source): parsed},
        flow_input={}, namespace="", snapshots=snapshots,
        protected_paths={}, budget=flow_module._SubflowTraversalBudget(),
    )
    import json
    value = json.loads(snapshots[""].available_outputs["filtered"])
    assert value["iterations"] == [{"id":"b","output":{"ok":True}}]


def test_snapshot_does_not_publish_partially_unknown_item_gate(tmp_path):
    definition = _setup(tmp_path, """
[[steps]]
id = "source"
prompt_file = "do.md"
response_format = "json"
output = "source.json"
[steps.vars]
value = "source"
[[steps]]
id = "gate"
prompt_file = "do.md"
response_format = "json"
[steps.vars]
value = "gate"
[[steps]]
id = "filtered"
prompt_file = "do.md"
response_format = "json"
foreach = "steps.source.output.items"
output = "filtered-${item.id}.json"
when = { all = [
    { ref = "item.keep", eq = true },
    { ref = "steps.gate.output.allow", eq = true }
] }
[steps.vars]
value = "filtered"
""")
    (tmp_path / "source.json").write_text(
        '{"items":[{"id":"a","keep":true}]}', encoding="utf-8",
    )
    (tmp_path / "filtered-a.json").write_text('{"ok":true}', encoding="utf-8")
    parsed = load_flow(definition, workspace=tmp_path)
    snapshots = {}
    flow_module._capture_run_start_invocation(
        parsed, workspace=tmp_path,
        cache={flow_module._filesystem_path_key(parsed.source): parsed},
        flow_input={}, namespace="", snapshots=snapshots,
        protected_paths={}, budget=flow_module._SubflowTraversalBudget(),
    )
    assert "filtered" not in snapshots[""].available_outputs
