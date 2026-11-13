"""Compact subflow checkpoint regression tests."""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
import cli_agent.workflows.flow as flow_module
from cli_agent.workflows.flow import load_flow, run_flow

def _setup(root: Path, *, preserve: bool = False) -> None:
    (root / "prompts").mkdir()
    (root / "prompts/discover.md").write_text("discover {{var:id}}", encoding="utf-8")
    (root / "prompts/assemble.md").write_text("assemble {{var:id}}", encoding="utf-8")
    (root / "child.toml").write_text(
        'version = 1\ninputs = ["id"]\nresult = "steps.assemble.output"\n'
        '[checkpoint]\nretain_intermediate = false\n'
        + ('preserve_steps = ["discover"]\n' if preserve else '')
        + '[[steps]]\nid = "discover"\n'
        'prompt_file = "prompts/discover.md"\n'
        'output = "state/$' + '{input.id}-discover.json"\n'
        'response_format = "json"\noverwrite_output = false\n'
        '[steps.vars]\nid = "$' + '{input.id}"\n'
        '[[steps]]\nid = "assemble"\n'
        'prompt_file = "prompts/assemble.md"\n'
        'output = "state/$' + '{input.id}-assemble.json"\n'
        'response_format = "json"\noverwrite_output = false\n'
        '[steps.vars]\nid = "$' + '{input.id}"\n', encoding="utf-8",
    )
    (root / "parent.toml").write_text(
        'version = 1\n[[steps]]\nid = "child"\nflow = "child.toml"\n'
        '[steps.input]\nid = "first"\n', encoding="utf-8",
    )
    (root / "state").mkdir()

def _run(root: Path, monkeypatch, expected: list[str], fail: str | None = None) -> None:
    calls: list[str] = []
    async def fake(options, *, dependencies=None):
        calls.append(options.prompt)
        if options.prompt == fail:
            raise RuntimeError("interrupted")
        if options.output is not None:
            options.output.write_text(json.dumps({"prompt": options.prompt}), encoding="utf-8")
        return SimpleNamespace(
            answer=json.dumps({"prompt": options.prompt}), web_context_statuses=(),
        )
    monkeypatch.setattr(flow_module, "run_once", fake)
    runner = lambda: asyncio.run(
        run_flow(load_flow(root / "parent.toml", workspace=root), workspace=root),
    )
    if fail is None:
        runner()
    else:
        with pytest.raises(RuntimeError, match="interrupted"):
            runner()
    assert calls == expected

def test_compact_child_resume_skips_all_internal_steps(tmp_path, monkeypatch):
    _setup(tmp_path)
    _run(tmp_path, monkeypatch, ["discover first", "assemble first"])
    assert not (tmp_path / "state/first-discover.json").exists()
    assert (tmp_path / "state/first-assemble.json").exists()
    assert len(list((tmp_path / ".cli-agent/flow-checkpoints").glob("*.json"))) == 1
    _run(tmp_path, monkeypatch, [])

def test_incomplete_child_keeps_checkpoints_then_compacts(tmp_path, monkeypatch):
    _setup(tmp_path)
    _run(tmp_path, monkeypatch, ["discover first", "assemble first"], "assemble first")
    assert (tmp_path / "state/first-discover.json").exists()
    assert not list((tmp_path / ".cli-agent").glob("flow-checkpoints/*.json"))
    _run(tmp_path, monkeypatch, ["assemble first"])
    assert not (tmp_path / "state/first-discover.json").exists()

def test_explicit_preserved_step_survives_compaction(tmp_path, monkeypatch):
    _setup(tmp_path, preserve=True)
    _run(tmp_path, monkeypatch, ["discover first", "assemble first"])
    assert (tmp_path / "state/first-discover.json").exists()
    _run(tmp_path, monkeypatch, [])

def test_changed_preserved_output_rejected(tmp_path, monkeypatch):
    _setup(tmp_path)
    _run(tmp_path, monkeypatch, ["discover first", "assemble first"])
    (tmp_path / "state/first-assemble.json").write_text('{"changed":true}', encoding="utf-8")
    with pytest.raises(ValueError, match="changed"):
        _run(tmp_path, monkeypatch, [])

def test_compact_validation(tmp_path):
    _setup(tmp_path)
    child = tmp_path / "child.toml"
    child.write_text(child.read_text().replace(
        'result = "steps.assemble.output"', '',
    ), encoding="utf-8")
    with pytest.raises(ValueError, match="requires a flow result"):
        load_flow(child, workspace=tmp_path)
    child.write_text(child.read_text().replace(
        'retain_intermediate = false',
        'retain_intermediate = false\npreserve_steps = ["missing"]',
    ).replace('inputs = ["id"]', 'inputs = ["id"]\nresult = "steps.assemble.output"'), encoding="utf-8")
    with pytest.raises(ValueError, match="unknown steps"):
        load_flow(child, workspace=tmp_path)
