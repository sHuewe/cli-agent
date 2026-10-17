from __future__ import annotations
import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest
import cli_agent.flow as flow_module
from cli_agent.flow import load_flow, run_flow

def _flow(tmp_path: Path) -> None:
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "work.md").write_text("work", encoding="utf-8")
    (tmp_path / "work").mkdir()
    (tmp_path / "child.toml").write_text(
        'version = 1\ninputs = ["item"]\nresult = "steps.work.output"\n'
        '[[steps]]\nid = "work"\nprompt_file = "work.md"\n'
        'response_format = "json"\noutput = "work/$' + '{input.item.id}.json"\n'
        'overwrite_output = false\n', encoding="utf-8")
    (tmp_path / "parent.toml").write_text(
        'version = 1\n[[steps]]\nid = "discover"\nprompt_file = "discover.md"\n'
        'response_format = "json"\n[[steps]]\nid = "process"\n'
        'flow = "child.toml"\nforeach = "steps.discover.output.items"\n'
        'iteration_id = "$' + '{item.id}"\n[steps.input]\nitem = "$' + '{item}"\n',
        encoding="utf-8")

def test_dynamic_nested_resume_accepts_only_startup_files(tmp_path, monkeypatch):
    _flow(tmp_path)
    (tmp_path / "work" / "one.json").write_text('{"ok":1}', encoding="utf-8")
    called = []
    async def fake_run_once(options, *, dependencies=None):
        called.append(options.prompt)
        if options.prompt == "discover":
            return SimpleNamespace(answer='{"items":[{"id":"one"},{"id":"two"}]}', web_context_statuses=())
        if options.prompt == "work":
            return SimpleNamespace(answer='{"ok":2}', web_context_statuses=())
        raise AssertionError("unexpected request")
    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    asyncio.run(run_flow(load_flow(tmp_path / "parent.toml", workspace=tmp_path), workspace=tmp_path))
    assert called == ["discover", "work"]

@pytest.mark.parametrize("created", [False, True])
def test_dynamic_nested_resume_rejects_mutated_or_new_file(tmp_path, monkeypatch, created):
    _flow(tmp_path)
    output = tmp_path / "work" / "one.json"
    if not created:
        output.write_text('{"ok":1}', encoding="utf-8")
    async def fake_run_once(options, *, dependencies=None):
        if options.prompt == "discover":
            output.write_text('{"ok":2}', encoding="utf-8")
            return SimpleNamespace(answer='{"items":[{"id":"one"}]}', web_context_statuses=())
        raise AssertionError("must not execute child")
    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    with pytest.raises(ValueError, match="existiert bereits" if created else "verändert"):
        asyncio.run(run_flow(load_flow(tmp_path / "parent.toml", workspace=tmp_path), workspace=tmp_path))

def test_dynamic_nested_path_components_resume(tmp_path, monkeypatch):
    _flow(tmp_path)
    child = tmp_path / "child.toml"
    child.write_text(
        child.read_text(encoding="utf-8").replace(
            "work/${input.item.id}.json",
            "work/${input.item.path}.json",
        ),
        encoding="utf-8",
    )
    nested = tmp_path / "work" / "team" / "one.json"
    nested.parent.mkdir()
    nested.write_text('{"ok":1}', encoding="utf-8")
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options.prompt)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[{"id":"one","path":"team/one"}]}',
                web_context_statuses=(),
            )
        raise AssertionError("existing nested-path checkpoint must be resumed")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    asyncio.run(
        run_flow(
            load_flow(tmp_path / "parent.toml", workspace=tmp_path),
            workspace=tmp_path,
        )
    )
    assert calls == ["discover"]
