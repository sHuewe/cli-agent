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
async def test_python_script_utf8_stdin_even_with_non_utf8_inherited_stdio(
    tmp_path, monkeypatch,
):
    """A Windows cp1252 subprocess must not mangle the UTF-8 JSON pipe.

    Covers Unicode punctuation that CP1252 decodes into different characters,
    as in the 1,687-item OKF source repair task envelope.
    """
    from cli_agent.workflows.flow_python_script import run_script

    flow = make_flow(
        tmp_path,
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "print(json.dumps({"
        "'value': payload['inputs']['tasks']['concepts'][0]['name'],"
        "'stdin_encoding': sys.stdin.encoding}, ensure_ascii=True))",
    )
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    original = "Annex E—Optional Formats at E-1; Führung – Prüfung ✓"
    result = await run_script(
        workspace=tmp_path,
        config=flow.steps[0].python_script,
        envelope_json=json.dumps(
            {"inputs": {"tasks": {"concepts": [{"name": original}]}}},
            ensure_ascii=False,
        ),
    )
    parsed = json.loads(result)
    assert parsed["value"] == original
    assert parsed["stdin_encoding"].lower().replace("-", "") == "utf8"


@pytest.mark.asyncio
async def test_flow_json_inputs_round_trip_unicode_across_python_steps(
    tmp_path, monkeypatch,
):
    """The actual json_inputs handoff must preserve task metadata."""
    script = (
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "if sys.argv[1] == 'prepare':\n"
        "    print(json.dumps({'concepts': [{"
        "'name': 'Annex E—Optional Formats at E-1',"
        "'title': 'Führung – Umsetzung',"
        "'description': 'Prüfung ✓'}]}, ensure_ascii=True))\n"
        "else:\n"
        "    print(json.dumps(payload['inputs']['tasks'], ensure_ascii=True))\n"
    )
    (tmp_path / "script.py").write_text(script, encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        'version = 1\nresult = "steps.apply.output"\n'
        '[[steps]]\nid = "prepare"\noutput = "state/tasks.json"\n'
        'overwrite_output = true\n'
        '[steps.python_script]\npath = "script.py"\nargs = ["prepare"]\n'
        '[[steps]]\nid = "apply"\noutput = "state/apply.json"\n'
        '[steps.python_script]\npath = "script.py"\nargs = ["apply"]\n'
        '[steps.json_inputs.tasks]\npath = "state/tasks.json"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("PYTHONIOENCODING", "cp1252")
    flow = load_flow(Path("flow.toml"), workspace=tmp_path)
    await run_flow(flow, workspace=tmp_path)
    stored = json.loads(
        (tmp_path / "state/tasks.json").read_text(encoding="utf-8")
    )
    applied = json.loads(
        (tmp_path / "state/apply.json").read_text(encoding="utf-8")
    )
    assert applied == stored
    assert applied["concepts"][0]["name"] == "Annex E—Optional Formats at E-1"
    assert applied["concepts"][0]["title"] == "Führung – Umsetzung"
    assert applied["concepts"][0]["description"] == "Prüfung ✓"


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


@pytest.mark.asyncio
async def test_foreach_receives_earlier_json_data_and_item(tmp_path):
    (tmp_path / 'test_script.py').write_text("import json\nimport sys\ndata = json.load(sys.stdin)\nif sys.argv[1] == 'seed':\n    print(json.dumps({\"items\":[{\"id\":\"a\"},{\"id\":\"b\"}],\"v\":7}))\nelse:\n    print(json.dumps({\"key\":data[\"item\"][\"id\"],\"seed\":data[\"inputs\"][\"data\"][\"v\"],\"iteration\":data[\"iteration_id\"]}))", encoding='utf-8')
    (tmp_path / 'flow.toml').write_text("version = 1\nresult = \"steps.process.output\"\n[[steps]]\nid = \"seed\"\noutput = \"state/seed.json\"\n[steps.python_script]\npath = \"test_script.py\"\nargs = [\"seed\"]\n[[steps]]\nid = \"process\"\nforeach = \"steps.seed.output.items\"\niteration_id = \"${item.id}\"\noutput = \"state/item-${iteration.id}.json\"\n[steps.python_script]\npath = \"test_script.py\"\nargs = [\"process\"]\n[steps.json_inputs.data]\npath = \"state/seed.json\"", encoding='utf-8')
    flow = load_flow(Path('flow.toml'), workspace=tmp_path)
    await run_flow(flow, workspace=tmp_path)
    outputs = [json.loads((tmp_path / ('state/item-' + suffix + '.json')).read_text())
               for suffix in ('a', 'b')]
    assert outputs == [
        {'key': 'a', 'seed': 7, 'iteration': 'a'},
        {'key': 'b', 'seed': 7, 'iteration': 'b'},
    ]



@pytest.mark.asyncio
async def test_workspace_venv_interpreter_uses_its_own_prefix(tmp_path):
    import os
    import venv

    virtualenv = tmp_path / ".venv"
    venv.EnvBuilder(with_pip=False).create(virtualenv)
    executable = ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
    flow = make_flow(
        tmp_path,
        "import sys, json\nprint(json.dumps({'prefix':sys.prefix,'base':sys.base_prefix}))",
        opts="interpreter = " + json.dumps(executable) + "\n",
    )
    await run_flow(flow, workspace=tmp_path)
    output = json.loads((tmp_path / "state/result.json").read_text())
    assert Path(output["prefix"]).resolve() == virtualenv.resolve()
    assert output["prefix"] != output["base"]


@pytest.mark.asyncio
async def test_oversized_stderr_does_not_hang_process_cleanup(tmp_path):
    flow = make_flow(
        tmp_path,
        "import sys\nsys.stderr.write('E' * 10000000)\nsys.stderr.flush()\n",
        opts="timeout_seconds = 10\n",
    )
    with pytest.raises(ValueError, match="stderr exceeds"):
        await asyncio.wait_for(run_flow(flow, workspace=tmp_path), timeout=8)
    assert not (tmp_path / "state/result.json").exists()


@pytest.mark.asyncio
async def test_timeout_terminates_child_process_tree(tmp_path):
    child = (
        "import time; time.sleep(2); "
        "from pathlib import Path; Path('surviving-child.txt').write_text('bad')"
    )
    script = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {child!r}])\n"
        "time.sleep(30)\n"
    )
    flow = make_flow(tmp_path, script, opts="timeout_seconds = 1\n")
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(run_flow(flow, workspace=tmp_path), timeout=8)
    await asyncio.sleep(2.2)
    assert not (tmp_path / "surviving-child.txt").exists()



@pytest.mark.asyncio
async def test_large_envelope_with_early_closed_stdin_keeps_valid_result(tmp_path):
    # A script that never reads stdin must not fail the Flow just because
    # its input exceeds the OS pipe buffer.
    flow = make_flow(
        tmp_path,
        "import os\nos.close(0)\nprint('{}')",
        opts="timeout_seconds = 10\n",
    )
    from cli_agent.workflows.flow_python_script import run_script
    result = await asyncio.wait_for(
        run_script(
            workspace=tmp_path,
            config=flow.steps[0].python_script,
            envelope_json=json.dumps({"data": "x" * 1_000_000}),
        ),
        timeout=8,
    )
    assert json.loads(result) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["exit", "invalid_json"])
async def test_failed_result_terminates_descendants_with_detached_pipes(tmp_path, failure):
    # Child redirects all streams, so parent pipe tasks finish before the
    # descendant does. Process-tree termination must still run on errors.
    child = (
        "import time; time.sleep(2); "
        "from pathlib import Path; Path('surviving-child.txt').write_text('bad')"
    )
    output = "sys.exit(7)" if failure == "exit" else "print('not json')"
    script = (
        "import subprocess, sys\n"
        "subprocess.Popen([sys.executable, '-c', " + repr(child) +
        "], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        + output
    )
    flow = make_flow(tmp_path, script)
    with pytest.raises(ValueError, match="code 7|valid JSON"):
        await asyncio.wait_for(run_flow(flow, workspace=tmp_path), timeout=8)
    await asyncio.sleep(2.2)
    assert not (tmp_path / "surviving-child.txt").exists()
