"""End-to-end nested foreach restart with a run-start checkpoint snapshot."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from cli_agent.workflows import flow as flow_module


def test_nested_foreach_restart_preserves_preexisting_checkpoint_on_failure(
    tmp_path, monkeypatch,
):
    (tmp_path / "discover.md").write_text("discover", encoding="utf-8")
    (tmp_path / "work.md").write_text("work", encoding="utf-8")
    (tmp_path / "work").mkdir()
    (tmp_path / "work" / "one.json").write_text('{"committed":true}', encoding="utf-8")
    (tmp_path / "child.toml").write_text(
        'version = 1\ninputs = ["item"]\nresult = "steps.work.output"\n'
        '[[steps]]\nid = "work"\nprompt_file = "work.md"\n'
        'response_format = "json"\noutput = "work/${input.item.id}.json"\n'
        'overwrite_output = false\n',
        encoding="utf-8",
    )
    (tmp_path / "parent.toml").write_text(
        'version = 1\n[[steps]]\nid = "discover"\nprompt_file = "discover.md"\n'
        'response_format = "json"\n[[steps]]\nid = "process"\n'
        'flow = "child.toml"\nforeach = "steps.discover.output.items"\n'
        'iteration_id = "${item.id}"\n[steps.input]\nitem = "${item}"\n',
        encoding="utf-8",
    )
    checkpoint = tmp_path / "work" / "one.json"
    original = checkpoint.read_bytes()
    calls = []
    fail = {"enabled": True}

    async def fake_run(options, *, dependencies=None):
        calls.append(options.prompt)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[{"id":"one"},{"id":"two"}]}',
                web_context_statuses=(),
            )
        if options.prompt == "work":
            if fail["enabled"]:
                raise RuntimeError("model unavailable")
            return SimpleNamespace(answer='{"ok":true}', web_context_statuses=())
        raise AssertionError("unexpected model call")

    monkeypatch.setattr(flow_module, "run_once", fake_run)
    definition = flow_module.load_flow(tmp_path / "parent.toml", workspace=tmp_path)
    with pytest.raises(RuntimeError, match="model unavailable"):
        asyncio.run(flow_module.run_flow(definition, workspace=tmp_path))
    assert calls == ["discover", "work"]
    assert checkpoint.read_bytes() == original

    fail["enabled"] = False
    calls.clear()
    asyncio.run(flow_module.run_flow(
        flow_module.load_flow(tmp_path / "parent.toml", workspace=tmp_path),
        workspace=tmp_path,
    ))
    # The first item must not be recomputed: only discovery and the missing
    # second item are sent to the model after recovery.
    assert calls == ["discover", "work"]
    assert checkpoint.read_bytes() == original
