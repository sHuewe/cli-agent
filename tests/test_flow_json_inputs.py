from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli_agent.flow as flow_module
from cli_agent.flow import load_flow, run_flow


def _flow(workspace: Path, text: str, name: str = "flow.toml"):
    path = workspace / name
    path.write_text(text.strip(), encoding="utf-8")
    return load_flow(path, workspace=workspace)


def _fake_model(monkeypatch, answers: list[str], prompts: list[str]):
    async def fake_run_once(options, *, dependencies=None):
        prompts.append(options.prompt)
        answer = answers.pop(0)
        if options.output is not None:
            options.output.parent.mkdir(parents=True, exist_ok=True)
            options.output.write_text(answer, encoding="utf-8")
        return SimpleNamespace(answer=answer)
    monkeypatch.setattr(flow_module, "run_once", fake_run_once)


def test_json_input_uses_prior_output_without_os_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("Read: {{var:scan}}", encoding="utf-8")
    prompts: list[str] = []
    answers = ['{"id":"c001","knowledge":["a"]}', '{"status":"ok"}']
    _fake_model(monkeypatch, answers, prompts)
    definition = _flow(tmp_path, """
version = 1
exclude_paths = ["state"]

[[steps]]
id = "scan"
prompt_file = "prompt.md"
response_format = "json"
output = "state/scan.json"

[steps.vars]
scan = "ignored"

[[steps]]
id = "route"
prompt_file = "prompt.md"
response_format = "json"
workspace_access = "none"
output = "state/route.json"

[steps.json_inputs.scan]
path = "state/scan.json"
""")
    # The scan prompt uses a normal variable; the route receives the JSON input.
    asyncio.run(run_flow(definition, workspace=tmp_path))
    assert prompts[1] == 'Read: {"id":"c001","knowledge":["a"]}'
    assert definition.steps[1].workspace_access == "none"
    assert json.loads((tmp_path / "state/route.json").read_text()) == {"status": "ok"}


def test_json_inputs_collection_from_foreach_across_subflow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "source.md").write_text("sources", encoding="utf-8")
    (tmp_path / "child.md").write_text("Routes: {{var:routes}}", encoding="utf-8")
    prompts: list[str] = []
    answers = [
        '{"chunks":[{"id":"a"},{"id":"b"}]}',
        '{"doc":"a"}',
        '{"doc":"b"}',
        '{"status":"routed"}',
    ]
    _fake_model(monkeypatch, answers, prompts)
    _flow(tmp_path, """
version = 1
inputs = ["scans"]
result = "steps.plan.output"

[[steps]]
id = "plan"
prompt_file = "child.md"
response_format = "json"
output = "state/plan.json"

[steps.json_inputs.routes]
iterate = "input.scans[]"
path = "state/scan-${each.id}.json"
""", "child.toml")
    definition = _flow(tmp_path, """
version = 1
exclude_paths = ["state"]

[[steps]]
id = "discover"
prompt_file = "source.md"
response_format = "json"
output = "state/discover.json"

[[steps]]
id = "scan"
prompt_file = "source.md"
response_format = "json"
foreach = "steps.discover.output.chunks"
iteration_id = "${item.id}"
output = "state/scan-${iteration.id}.json"

[[steps]]
id = "plan"
flow = "child.toml"

[steps.input]
scans = "${steps.scan.output.iterations}"
""")
    asyncio.run(run_flow(definition, workspace=tmp_path))
    assert prompts[-1] == 'Routes: [{"doc":"a"},{"doc":"b"}]'
    assert json.loads((tmp_path / "state/plan.json").read_text())["status"] == "routed"


def test_json_input_nested_collection_selector() -> None:
    scans = [
        {"output": {"chunks": [{"id": "a"}, {"id": "b"}]}},
        {"output": {"chunks": [{"id": "c"}]}},
    ]
    result = flow_module._json_input_items(
        "input.scans[].output.chunks[]",
        flow_input={"scans": scans}, item=None,
    )
    assert [x["id"] for x in result] == ["a", "b", "c"]


def test_resumed_json_inputs_never_read_for_skipped_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("Read: {{var:scan}}", encoding="utf-8")
    (tmp_path / "state").mkdir()
    (tmp_path / "state/scan.json").write_text('{"id":"old"}', encoding="utf-8")
    (tmp_path / "state/route.json").write_text('{"status":"old"}', encoding="utf-8")
    prompts: list[str] = []
    _fake_model(monkeypatch, [], prompts)
    definition = _flow(tmp_path, """
version = 1

[[steps]]
id = "scan"
prompt_file = "prompt.md"
response_format = "json"
output = "state/scan.json"

[steps.vars]
scan = "ignored"

[[steps]]
id = "route"
prompt_file = "prompt.md"
response_format = "json"
output = "state/route.json"

[steps.json_inputs.scan]
path = "state/scan.json"
""")
    asyncio.run(run_flow(definition, workspace=tmp_path))
    assert prompts == []
    (tmp_path / "state/route.json").unlink()
    answers = ['{"status":"new"}']
    _fake_model(monkeypatch, answers, prompts)
    asyncio.run(run_flow(definition, workspace=tmp_path))
    assert prompts == ['Read: {"id":"old"}']


def test_json_input_cannot_read_undeclared_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("Read: {{var:data}}", encoding="utf-8")
    (tmp_path / "some.json").write_text('{"secret":"value"}', encoding="utf-8")
    definition = _flow(tmp_path, """
version = 1

[[steps]]
id = "reader"
prompt_file = "prompt.md"
response_format = "json"

[steps.json_inputs.data]
path = "some.json"
""")
    _fake_model(monkeypatch, [], [])
    with pytest.raises(ValueError, match="bereits abgeschlossenen"):
        asyncio.run(run_flow(definition, workspace=tmp_path))


@pytest.mark.parametrize("field,value", [
    ("path", '"${steps.scan.output.file}"'),
    ("path", '"${unknown}"'),
    ("path", '"${each.id}"'),
    ("iterate", '"input.scans"'),
])
def test_json_input_validation_rejects_unsafe_templates(
    tmp_path: Path, field: str, value: str,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    snippet = (
        "[steps.json_inputs.data]\n"
        'path = "state/data.json"\n'
        if field == "iterate" else "[steps.json_inputs.data]\n"
    )
    snippet += field + " = " + value
    with pytest.raises(ValueError, match="json_inputs"):
        _flow(tmp_path, 'version = 1\n[[steps]]\nid = "one"\nprompt_file = "prompt.md"\n' + snippet)


def test_json_input_rejects_modified_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("{{var:data}}", encoding="utf-8")
    # Test the artifact boundary without an LLM call.
    path = tmp_path / "state.json"
    path.write_text('{"id":1}', encoding="utf-8")
    spec = flow_module.JsonInputSpec(path="state.json")
    from dataclasses import replace
    basic = _flow(tmp_path, 'version = 1\n[[steps]]\nid = "one"\nprompt_file = "prompt.md"')
    step = replace(basic.steps[0], json_inputs={"data": spec})
    fingerprint = flow_module._checkpoint_fingerprint('{"id":2}')
    with pytest.raises(ValueError, match="verändert"):
        flow_module._resolve_json_inputs(
            step,
            workspace=tmp_path,
            flow_input={},
            item=None,
            iteration_id=None,
            completed_json_outputs={flow_module._filesystem_path_key(path): fingerprint},
        )


def test_json_input_total_size_is_limited(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("{{var:data}}", encoding="utf-8")
    path = tmp_path / "state.json"
    raw = '{"list":[1,2,3]}'
    path.write_text(raw, encoding="utf-8")
    from dataclasses import replace
    step = replace(
        _flow(tmp_path, 'version = 1\n[[steps]]\nid = "one"\nprompt_file = "prompt.md"').steps[0],
        json_inputs={"data": flow_module.JsonInputSpec(path="state.json")},
    )
    monkeypatch.setattr(flow_module, "MAX_JSON_INPUT_TOTAL_BYTES", 4)
    with pytest.raises(ValueError, match="Gesamtlimit"):
        flow_module._resolve_json_inputs(
            step, workspace=tmp_path, flow_input={}, item=None,
            iteration_id=None,
            completed_json_outputs={
                flow_module._filesystem_path_key(path):
                flow_module._checkpoint_fingerprint(raw),
            },
        )


def test_structured_json_limit_is_100_mb() -> None:
    assert flow_module.MAX_STRUCTURED_OUTPUT_BYTES == 100_000_000


def test_json_input_accepts_persisted_crlf_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("Read: {{var:data}}", encoding="utf-8")
    prompts: list[str] = []

    async def fake_run_once(options, *, dependencies=None):
        prompts.append(options.prompt)
        if len(prompts) == 1:
            answer = '{\\r\\n  "id": "crlf"\\r\\n}'
        else:
            answer = '{"status":"ok"}'
        if options.output is not None:
            options.output.parent.mkdir(parents=True, exist_ok=True)
            options.output.write_bytes(answer.encode("utf-8"))
        return SimpleNamespace(answer=answer)

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    definition = _flow(tmp_path, """
version = 1

[[steps]]
id = "first"
prompt_file = "prompt.md"
response_format = "json"
output = "state/first.json"

[steps.vars]
data = "initial"

[[steps]]
id = "second"
prompt_file = "prompt.md"
response_format = "json"
output = "state/second.json"

[steps.json_inputs.data]
path = "state/first.json"
""")
    asyncio.run(run_flow(definition, workspace=tmp_path))
    assert len(prompts) == 2
    assert json.loads(prompts[1].removeprefix("Read: ")) == {"id": "crlf"}


def test_json_input_rejects_missing_persisted_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")

    async def fake_run_once(options, *, dependencies=None):
        return SimpleNamespace(answer='{"status":"ok"}')

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    definition = _flow(tmp_path, """
version = 1

[[steps]]
id = "first"
prompt_file = "prompt.md"
response_format = "json"
output = "state/first.json"
""")
    with pytest.raises(ValueError, match="nicht gespeichert"):
        asyncio.run(run_flow(definition, workspace=tmp_path))
