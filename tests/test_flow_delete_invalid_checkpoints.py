import asyncio
import json
from types import SimpleNamespace

import pytest

import cli_agent.workflows.flow as runner


@pytest.mark.parametrize("flag", ["--delete-invalid-checkpoints", "--delete-invalide-checkpoints"])
def test_flag_aliases(flag):
    assert runner.build_parser().parse_args(["run", "flow.toml", flag]).delete_invalid_checkpoints
    assert not runner.build_parser().parse_args(["run", "flow.toml"]).delete_invalid_checkpoints
    with pytest.raises(SystemExit):
        runner.build_parser().parse_args(["validate", "flow.toml", flag])


def test_regenerate_only_schema_invalid(tmp_path, monkeypatch):
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "config.toml").write_text(
        '[model]\nprovider = "ollama"\nmodel = "test"\nbase_url = "http://localhost:11434"\n',
        encoding="utf-8",
    )
    (tmp_path / "schema.json").write_text(json.dumps({
        "type": "object", "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"], "additionalProperties": False,
    }), encoding="utf-8")
    valid = tmp_path / "valid.json"
    invalid = tmp_path / "invalid.json"
    valid.write_text('{ "ok": false }', encoding="utf-8")
    invalid.write_text('{"old": true}', encoding="utf-8")
    flow = tmp_path / "flow.toml"
    flow.write_text("""
version = 1
[[steps]]
id = "valid"
config = "config.toml"
prompt_file = "prompt.md"
response_format = "json"
output_schema = "schema.json"
output = "valid.json"
[[steps]]
id = "invalid"
config = "config.toml"
prompt_file = "prompt.md"
response_format = "json"
output_schema = "schema.json"
output = "invalid.json"
""", encoding="utf-8")
    definition = runner.load_flow(flow, workspace=tmp_path)
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options.output)
        assert not options.output.exists()
        answer = '{"ok":true}'
        options.output.write_text(answer, encoding="utf-8")
        return SimpleNamespace(answer=answer, web_context_statuses=())

    monkeypatch.setattr(runner, "run_once", fake_run_once)
    with pytest.raises(ValueError, match="does not match"):
        asyncio.run(runner.run_flow(definition, workspace=tmp_path))
    assert calls == []
    assert invalid.read_text(encoding="utf-8") == '{"old": true}'
    asyncio.run(runner.run_flow(
        definition, workspace=tmp_path, delete_invalid_checkpoints=True,
    ))
    assert calls == [invalid]
    assert valid.read_text(encoding="utf-8") == '{ "ok": false }'
    assert json.loads(invalid.read_text(encoding="utf-8")) == {"ok": True}
    asyncio.run(runner.run_flow(
        definition, workspace=tmp_path, delete_invalid_checkpoints=True,
    ))
    assert calls == [invalid]
