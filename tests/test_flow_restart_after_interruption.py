"""End-to-end nested foreach checkpoint restart and failure recovery."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from cli_agent.workflows import flow as flow_module


def _make_flow(root):
    (root / "discover.md").write_text("discover", encoding="utf-8")
    (root / "work.md").write_text("work", encoding="utf-8")
    (root / "work").mkdir()
    (root / "child.toml").write_text(
        'version = 1\ninputs = ["item"]\nresult = "steps.work.output"\n'
        '[[steps]]\nid = "work"\nprompt_file = "work.md"\n'
        'response_format = "json"\n'
        'output = "work/${input.item.id}.json"\n'
        'overwrite_output = false\n',
        encoding="utf-8",
    )
    (root / "parent.toml").write_text(
        'version = 1\n'
        '[[steps]]\nid = "discover"\nprompt_file = "discover.md"\n'
        'response_format = "json"\n'
        '[[steps]]\nid = "process"\n'
        'flow = "child.toml"\n'
        'foreach = "steps.discover.output.items"\n'
        'iteration_id = "${item.id}"\n'
        '[steps.input]\nitem = "${item}"\n',
        encoding="utf-8",
    )
    return root / "parent.toml"


def test_nested_foreach_restart_reuses_committed_output_after_model_failure(tmp_path, monkeypatch):
    path = _make_flow(tmp_path)
    calls = []
    phase = {"name": "initial"}

    async def run_once(options, *, dependencies=None):
        calls.append(options.prompt)
        if options.prompt == "discover":
            ids = ["one"] if phase["name"] == "initial" else ["one", "two"]
            return SimpleNamespace(
                answer=json.dumps({"items": [{"id": name} for name in ids]}),
                web_context_statuses=(),
            )
        if options.prompt == "work":
            if phase["name"] == "interrupted":
                raise RuntimeError("network failure before second checkpoint")
            return SimpleNamespace(
                answer='{"status":"ok"}', web_context_statuses=(),
            )
        raise AssertionError("unexpected model request")

    monkeypatch.setattr(flow_module, "run_once", run_once)
    asyncio.run(flow_module.run_flow(
        flow_module.load_flow(path, workspace=tmp_path), workspace=tmp_path,
    ))
    first = tmp_path / "work" / "one.json"
    second = tmp_path / "work" / "two.json"
    assert calls == ["discover", "work"]
    assert first.is_file() and not second.exists()
    first_content = first.read_bytes()

    phase["name"] = "interrupted"
    calls.clear()
    with pytest.raises(RuntimeError, match="network failure"):
        asyncio.run(flow_module.run_flow(
            flow_module.load_flow(path, workspace=tmp_path), workspace=tmp_path,
        ))
    assert calls == ["discover", "work"]
    assert first.read_bytes() == first_content
    assert not second.exists()

    phase["name"] = "restarted"
    calls.clear()
    asyncio.run(flow_module.run_flow(
        flow_module.load_flow(path, workspace=tmp_path), workspace=tmp_path,
    ))
    assert calls == ["discover", "work"]
    assert first.read_bytes() == first_content
    assert second.is_file()
    assert json.loads(second.read_text(encoding="utf-8")) == {"status": "ok"}
