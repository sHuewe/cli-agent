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

def test_unrelated_dynamic_template_cannot_authorize_static_child(
    tmp_path, monkeypatch,
):
    _flow(tmp_path)
    (tmp_path / "work" / "fixed.json").write_text(
        '{"stale":true}', encoding="utf-8"
    )
    (tmp_path / "unrelated.md").write_text("unrelated", encoding="utf-8")
    parent = tmp_path / "parent.toml"
    parent.write_text(
        parent.read_text(encoding="utf-8")
        + '\n[[steps]]\nid = "unrelated"\nprompt_file = "unrelated.md"\n'
          'response_format = "json"\n'
          'foreach = "steps.discover.output.items"\n'
          'output = "work/${item.id}.json"\n',
        encoding="utf-8",
    )
    child = tmp_path / "child.toml"
    child.write_text(
        child.read_text(encoding="utf-8").replace(
            'output = "work/${input.item.id}.json"',
            'output = "work/fixed.json"',
        ),
        encoding="utf-8",
    )
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options.prompt)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[{"id":"fixed"}]}',
                web_context_statuses=(),
            )
        raise AssertionError("static child must not adopt another step's checkpoint")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    with pytest.raises(ValueError, match="existiert bereits"):
        asyncio.run(
            run_flow(
                load_flow(parent, workspace=tmp_path),
                workspace=tmp_path,
            )
        )
    assert calls == ["discover"]


def test_known_previous_output_is_available_to_dynamic_owner(tmp_path, monkeypatch):
    _flow(tmp_path)
    path = tmp_path / "work" / "one.json"
    path.write_text('{"old":1}', encoding="utf-8")
    (tmp_path / "later.md").write_text("later {{var:old}}", encoding="utf-8")
    parent = tmp_path / "parent.toml"
    parent.write_text(
        parent.read_text(encoding="utf-8")
        + '\n[[steps]]\nid = "later"\nprompt_file = "later.md"\n'
          'response_format = "json"\noutput = "work/one.json"\n'
          'overwrite_output = true\n'
          '[steps.vars]\nold = "${previous_output.old}"\n',
        encoding="utf-8",
    )
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options.prompt)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[{"id":"one"}]}',
                web_context_statuses=(),
            )
        if options.prompt == "later 1":
            return SimpleNamespace(answer='{"updated":2}', web_context_statuses=())
        raise AssertionError("dynamic child must resume, not call model")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    asyncio.run(run_flow(load_flow(parent, workspace=tmp_path), workspace=tmp_path))
    assert calls == ["discover", "later 1"]


@pytest.mark.skipif(__import__("os").name != "nt", reason="Windows filesystem")
def test_dynamic_checkpoint_case_insensitive_suffix(tmp_path, monkeypatch):
    _flow(tmp_path)
    child = tmp_path / "child.toml"
    child.write_text(
        child.read_text(encoding="utf-8").replace(
            'output = "work/${input.item.id}.json"',
            'output = "work/${input.item.id}.JSON"',
        ),
        encoding="utf-8",
    )
    (tmp_path / "work" / "one.json").write_text('{"ok":1}', encoding="utf-8")
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options.prompt)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[{"id":"one"}]}',
                web_context_statuses=(),
            )
        raise AssertionError("matching checkpoint should skip model")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    asyncio.run(run_flow(load_flow(tmp_path / "parent.toml", workspace=tmp_path), workspace=tmp_path))
    assert calls == ["discover"]


def test_dynamic_discovery_does_not_recount_same_directory_per_template(
    tmp_path, monkeypatch,
):
    _flow(tmp_path)
    child = tmp_path / "child.toml"
    child.write_text(
        child.read_text(encoding="utf-8") +
        '\n[[steps]]\nid = "verify"\nprompt_file = "work.md"\n'
        'response_format = "json"\n'
        'output = "work/verify-${input.item.id}.json"\n'
        'overwrite_output = false\n',
        encoding="utf-8",
    )
    for i in range(24):
        (tmp_path / "work" / f"unrelated-{i}.txt").write_text("not JSON", encoding="utf-8")
    monkeypatch.setattr(flow_module, "MAX_DYNAMIC_CHECKPOINT_CANDIDATES", 35)
    # Two dynamic output templates share the same directory. There are
    # fewer than 35 distinct entries, although two walks used to exceed 35.
    fingerprints, paths = flow_module._snapshot_dynamic_checkpoint_candidates(
        workspace=tmp_path,
        cache={"child": load_flow(child, workspace=tmp_path)},
    )
    assert fingerprints == {}
    assert paths == ()


def test_dynamic_discovery_distinct_entry_limit_still_enforced(
    tmp_path, monkeypatch,
):
    _flow(tmp_path)
    for i in range(24):
        (tmp_path / "work" / f"unrelated-{i}.txt").write_text("not JSON", encoding="utf-8")
    monkeypatch.setattr(flow_module, "MAX_DYNAMIC_CHECKPOINT_CANDIDATES", 10)
    with pytest.raises(ValueError, match="Zu viele eindeutige"):
        flow_module._snapshot_dynamic_checkpoint_candidates(
            workspace=tmp_path,
            cache={"child": load_flow(tmp_path / "child.toml", workspace=tmp_path)},
        )


def test_dynamic_discovery_overlapping_roots_count_paths_once(
    tmp_path, monkeypatch,
):
    _flow(tmp_path)
    (tmp_path / "work" / "nested").mkdir()
    for i in range(15):
        (tmp_path / "work" / "nested" / f"unrelated-{i}.txt").write_text(
            "x", encoding="utf-8"
        )
    child = tmp_path / "child.toml"
    child.write_text(
        child.read_text(encoding="utf-8") +
        '\n[[steps]]\nid = "nested"\nprompt_file = "work.md"\n'
        'response_format = "json"\n'
        'output = "work/nested/check-${input.item.id}.json"\n'
        'overwrite_output = false\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(flow_module, "MAX_DYNAMIC_CHECKPOINT_CANDIDATES", 20)
    fingerprints, paths = flow_module._snapshot_dynamic_checkpoint_candidates(
        workspace=tmp_path,
        cache={"child": load_flow(child, workspace=tmp_path)},
    )
    assert fingerprints == {}
    assert paths == ()


def test_dynamic_discovery_counts_symlink_entries_not_targets(
    tmp_path, monkeypatch,
):
    _flow(tmp_path)
    target = tmp_path / "work" / "target.txt"
    target.write_text("data", encoding="utf-8")
    try:
        for i in range(12):
            (tmp_path / "work" / f"link-{i}.txt").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Creating file symlinks is not permitted on this platform")

    monkeypatch.setattr(flow_module, "MAX_DYNAMIC_CHECKPOINT_CANDIDATES", 10)
    with pytest.raises(ValueError, match="Zu viele eindeutige"):
        flow_module._snapshot_dynamic_checkpoint_candidates(
            workspace=tmp_path,
            cache={"child": load_flow(tmp_path / "child.toml", workspace=tmp_path)},
        )
