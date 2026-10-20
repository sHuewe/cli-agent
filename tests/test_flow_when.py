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
