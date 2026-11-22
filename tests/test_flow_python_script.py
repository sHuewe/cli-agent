"""Tests for trusted JSON subprocess Flow steps."""
import asyncio
import json
from pathlib import Path
import sys

import pytest

from cli_agent.workflows.flow import load_flow, run_flow, _reserved_flow_input_paths, _filesystem_path_key
from cli_agent.workflows.flow_python_script import verify_script_config


def make_flow(root, script="print('{}')", *, overwrite=False, opts=""):
    (root / "script.py").write_text(script + "\n", encoding="utf-8")
    toml = (
        'version = 1\nresult = "steps.script.output"\n'
        '[[steps]]\nid = "script"\noutput = "state/result.json"\n'
        + ("overwrite_output = true\n" if overwrite else "")
        + '[steps.python_script]\npath = "script.py"\n' + opts
    )
    (root / "flow.toml").write_text(toml, encoding="utf-8")
    return load_flow(Path("flow.toml"), workspace=root)


@pytest.mark.asyncio
async def test_json_stdin_stdout_and_checkpoint_resume(tmp_path):
    flow = make_flow(tmp_path, (
        "import json, sys\n"
        "from pathlib import Path\n"
        "p = Path('count')\n"
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')\n"
        "x = json.load(sys.stdin)\n"
        "print(json.dumps({'inputs':x['inputs'], 'item': x['item']}))"
    ))
    await run_flow(flow, workspace=tmp_path)
    await run_flow(flow, workspace=tmp_path)
    assert (tmp_path / "count").read_text() == "x"
    assert json.loads((tmp_path / "state/result.json").read_text()) == {"inputs": {}, "item": None}


@pytest.mark.asyncio
async def test_overwrite_executes_again(tmp_path):
    flow = make_flow(tmp_path, (
        "from pathlib import Path\n"
        "p=Path('count')\n"
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')\n"
        "print('{}')"
    ), overwrite=True)
    await run_flow(flow, workspace=tmp_path)
    await run_flow(flow, workspace=tmp_path)
    assert (tmp_path / "count").read_text() == "xx"


@pytest.mark.asyncio
async def test_rejects_invalid_json_and_exit_code(tmp_path):
    flow = make_flow(tmp_path, "print('not json')")
    with pytest.raises(ValueError, match="JSON"):
        await run_flow(flow, workspace=tmp_path)
    assert not (tmp_path / "state/result.json").exists()
    flow = make_flow(tmp_path, "import sys\nsys.exit(7)")
    with pytest.raises(ValueError, match="code 7"):
        await run_flow(flow, workspace=tmp_path)


@pytest.mark.asyncio
async def test_timeout(tmp_path):
    flow = make_flow(tmp_path, "import time\ntime.sleep(30)", opts="timeout_seconds = 1\n")
    with pytest.raises(asyncio.TimeoutError):
        await run_flow(flow, workspace=tmp_path)


@pytest.mark.asyncio
async def test_custom_interpreter(tmp_path):
    flow = make_flow(tmp_path, "import json, sys\nprint(json.dumps({'exe':sys.executable}))",
                     opts="interpreter = " + json.dumps(sys.executable) + "\n")
    await run_flow(flow, workspace=tmp_path)
    result = json.loads((tmp_path / "state/result.json").read_text())
    assert Path(result["exe"]).resolve() == Path(sys.executable).resolve()


def test_source_reserved_and_tamper_detected(tmp_path):
    flow = make_flow(tmp_path)
    script = (tmp_path / "script.py").resolve()
    assert _filesystem_path_key(script) in _reserved_flow_input_paths(flow, workspace=tmp_path)
    (tmp_path / "script.py").write_text("print('changed')")
    with pytest.raises(ValueError, match="changed after Flow startup"):
        verify_script_config(tmp_path, flow.steps[0].python_script)


def test_rejects_dynamic_python_paths(tmp_path):
    (tmp_path / "script.py").write_text("print('{}')")
    for extras in ['path = "' + chr(36) + '{steps.x.output}"',
                   'path = "script.py"\ninterpreter = "' + chr(36) + '{input.python}"',
                   'path = "script.py"\nargs = ["' + chr(36) + '{item.script}"]']:
        (tmp_path / "flow.toml").write_text(
            'version = 1\n[[steps]]\nid = "script"\noutput = "result.json"\n'
            '[steps.python_script]\n' + extras + '\n'
        )
        with pytest.raises(ValueError):
            load_flow(Path("flow.toml"), workspace=tmp_path)


@pytest.mark.asyncio
async def test_schema_validates_before_checkpoint(tmp_path):
    (tmp_path / "schema.json").write_text(json.dumps({
        "type": "object", "properties": {"value": {"type": "integer"}},
        "required": ["value"]
    }))
    make_flow(tmp_path, 'import json\nprint(json.dumps({"value":"no"}))')
    config = (tmp_path / "flow.toml").read_text()
    config = config.replace("[steps.python_script]", 'output_schema = "schema.json"\n[steps.python_script]')
    (tmp_path / "flow.toml").write_text(config)
    flow = load_flow(Path("flow.toml"), workspace=tmp_path)
    with pytest.raises(ValueError, match="output_schema"):
        await run_flow(flow, workspace=tmp_path)
