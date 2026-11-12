from __future__ import annotations
import asyncio
from pathlib import Path
from types import SimpleNamespace
import pytest
import cli_agent.workflows.flow as flow_module
from cli_agent.workflows.flow import load_flow, run_flow

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
    with pytest.raises(ValueError, match="Output-Datei existiert bereits:" if created else "changed"):
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
    with pytest.raises(ValueError, match="Output-Datei existiert bereits:"):
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
    with pytest.raises(ValueError, match="Too many distinct dynamic JSON checkpoint"):
        flow_module._snapshot_dynamic_checkpoint_candidates(
            workspace=tmp_path,
            cache={"child": load_flow(tmp_path / "child.toml", workspace=tmp_path)},
            max_candidates=10,
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

    with pytest.raises(ValueError, match="Too many distinct dynamic JSON checkpoint"):
        flow_module._snapshot_dynamic_checkpoint_candidates(
            workspace=tmp_path,
            cache={"child": load_flow(tmp_path / "child.toml", workspace=tmp_path)},
            max_candidates=10,
        )



def test_dynamic_checkpoint_snapshot_budget_is_configurable_and_reports_size(
    tmp_path, monkeypatch,
):
    _flow(tmp_path)
    output = tmp_path / "work" / "one.json"
    output.write_text('{"payload":"' + "a" * 130 + '"}', encoding="utf-8")
    monkeypatch.setattr(
        flow_module, "MAX_DYNAMIC_CHECKPOINT_TOTAL_BYTES", 100,
    )
    flow = load_flow(tmp_path / "parent.toml", workspace=tmp_path)
    # The default is read from the Flow value, not the monkeypatched
    # constant; supply a deliberately tight test budget directly.
    with pytest.raises(ValueError, match=r"limit 100 bytes.*last file"):
        flow_module._snapshot_dynamic_checkpoint_candidates(
            workspace=tmp_path, cache={
                "child": load_flow(tmp_path / "child.toml", workspace=tmp_path),
            }, max_snapshot_bytes=100,
        )
    snapshots, paths = flow_module._snapshot_dynamic_checkpoint_candidates(
        workspace=tmp_path, cache={
            "child": load_flow(tmp_path / "child.toml", workspace=tmp_path),
        }, max_snapshot_bytes=500,
    )
    assert snapshots
    assert paths == (output,)


def test_root_flow_checkpoint_budget_uses_explicit_limit(tmp_path, monkeypatch):
    _flow(tmp_path)
    flow_file = tmp_path / "parent.toml"
    flow_file.write_text(
        flow_file.read_text(encoding="utf-8")
        .replace("version = 1\n", "version = 1\n"
                 "[checkpoint]\nmax_snapshot_bytes = 600000000\n"),
        encoding="utf-8",
    )
    flow = load_flow(flow_file, workspace=tmp_path)
    assert flow.checkpoint_snapshot_max_bytes == 600_000_000
    observed = []
    original = flow_module._snapshot_dynamic_checkpoint_candidates

    def record_budget(*, workspace, cache, known_checkpoint_fingerprints=None,
                      max_snapshot_bytes=0, max_candidates=0):
        observed.append(max_snapshot_bytes)
        return original(workspace=workspace, cache=cache,
                        known_checkpoint_fingerprints=known_checkpoint_fingerprints,
                        max_snapshot_bytes=max_snapshot_bytes,
                        max_candidates=max_candidates)

    async def fake_run_once(options, *, dependencies=None):
        if options.prompt == "discover":
            return SimpleNamespace(answer='{"items":[]}', web_context_statuses=())
        raise AssertionError("Unexpected model request")

    monkeypatch.setattr(flow_module, "_snapshot_dynamic_checkpoint_candidates", record_budget)
    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    asyncio.run(run_flow(flow, workspace=tmp_path))
    assert observed == [600_000_000]


@pytest.mark.parametrize("value", [
    "100000000", "1000000001", "true", '"600000000"',
])
def test_root_checkpoint_budget_rejects_invalid_values(tmp_path, value):
    _flow(tmp_path)
    flow_file = tmp_path / "parent.toml"
    flow_file.write_text(
        flow_file.read_text(encoding="utf-8")
        .replace("version = 1\n", "version = 1\n"
                 f"[checkpoint]\nmax_snapshot_bytes = {value}\n"),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"checkpoint.max_snapshot_bytes"):
        load_flow(flow_file, workspace=tmp_path)



def test_dynamic_checkpoint_candidate_budget_is_configurable(tmp_path):
    _flow(tmp_path)
    for i in range(24):
        (tmp_path / "work" / f"unrelated-{i}.txt").write_text(
            "not a checkpoint", encoding="utf-8",
        )
    child = load_flow(tmp_path / "child.toml", workspace=tmp_path)
    with pytest.raises(
        ValueError, match=r"25 examined entries \(limit 24, last entry ",
    ):
        flow_module._snapshot_dynamic_checkpoint_candidates(
            workspace=tmp_path, cache={"child": child}, max_candidates=24,
        )
    fingerprints, paths = flow_module._snapshot_dynamic_checkpoint_candidates(
        workspace=tmp_path, cache={"child": child}, max_candidates=40,
    )
    assert fingerprints == {}
    assert paths == ()


def test_root_checkpoint_candidate_budget_is_passed_to_discovery(tmp_path, monkeypatch):
    _flow(tmp_path)
    flow_file = tmp_path / "parent.toml"
    flow_file.write_text(
        flow_file.read_text(encoding="utf-8").replace(
            "version = 1\n",
            "version = 1\n[checkpoint]\n"
            "max_snapshot_bytes = 600000000\nmax_candidates = 100000\n",
        ),
        encoding="utf-8",
    )
    flow = load_flow(flow_file, workspace=tmp_path)
    assert flow.checkpoint_max_candidates == 100_000
    observed = []
    original = flow_module._snapshot_dynamic_checkpoint_candidates

    def capture(*, workspace, cache, known_checkpoint_fingerprints=None,
                max_snapshot_bytes=0, max_candidates=0):
        observed.append((max_snapshot_bytes, max_candidates))
        return original(
            workspace=workspace, cache=cache,
            known_checkpoint_fingerprints=known_checkpoint_fingerprints,
            max_snapshot_bytes=max_snapshot_bytes, max_candidates=max_candidates,
        )

    async def fake_run_once(options, *, dependencies=None):
        if options.prompt == "discover":
            return SimpleNamespace(answer='{"items":[]}', web_context_statuses=())
        raise AssertionError("Unexpected model request")

    monkeypatch.setattr(flow_module, "_snapshot_dynamic_checkpoint_candidates", capture)
    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    asyncio.run(run_flow(flow, workspace=tmp_path))
    assert observed == [(600_000_000, 100_000)]


@pytest.mark.parametrize("value", [
    "19999", "200001", "true", '"50000"',
])
def test_root_checkpoint_candidate_budget_rejects_invalid_values(tmp_path, value):
    _flow(tmp_path)
    flow_file = tmp_path / "parent.toml"
    flow_file.write_text(
        flow_file.read_text(encoding="utf-8").replace(
            "version = 1\n",
            f"version = 1\n[checkpoint]\nmax_candidates = {value}\n",
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="checkpoint.max_candidates"):
        load_flow(flow_file, workspace=tmp_path)



def _static_unknown_child_flow(tmp_path: Path) -> Path:
    """A static child output whose invocation cannot be planned at run start."""
    _flow(tmp_path)
    child = tmp_path / "child.toml"
    child.write_text(
        child.read_text(encoding="utf-8").replace(
            'output = "work/${input.item.id}.json"',
            'output = "work/fixed.json"',
        ),
        encoding="utf-8",
    )
    return tmp_path / "work" / "fixed.json"


def test_nested_static_checkpoint_can_resume_without_reinvoking_model(
    tmp_path, monkeypatch,
):
    output = _static_unknown_child_flow(tmp_path)
    output.write_text('{"status":"ok","items":[1,2,3]}', encoding="utf-8")
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options.prompt)
        if options.prompt == "discover":
            return SimpleNamespace(
                answer='{"items":[{"id":"one"}]}', web_context_statuses=(),
            )
        raise AssertionError("static child checkpoint should be reused")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    asyncio.run(run_flow(
        load_flow(tmp_path / "parent.toml", workspace=tmp_path),
        workspace=tmp_path,
    ))
    assert calls == ["discover"]
    assert output.read_text(encoding="utf-8") == '{"status":"ok","items":[1,2,3]}'


@pytest.mark.parametrize("exists_at_start", [False, True])
def test_nested_static_checkpoint_does_not_adopt_new_or_changed_file(
    tmp_path, monkeypatch, exists_at_start,
):
    output = _static_unknown_child_flow(tmp_path)
    if exists_at_start:
        output.write_text('{"status":"old"}', encoding="utf-8")

    async def fake_run_once(options, *, dependencies=None):
        if options.prompt == "discover":
            output.write_text('{"status":"new"}', encoding="utf-8")
            return SimpleNamespace(
                answer='{"items":[{"id":"one"}]}', web_context_statuses=(),
            )
        raise AssertionError("new/modified static output must not be adopted")

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    with pytest.raises(
        ValueError,
        match="changed" if exists_at_start else "Output-Datei existiert bereits:",
    ):
        asyncio.run(run_flow(
            load_flow(tmp_path / "parent.toml", workspace=tmp_path),
            workspace=tmp_path,
        ))


@pytest.mark.parametrize("as_absolute", [False, True])
def test_static_checkpoint_discovery_skips_workspace_directory(
    tmp_path, as_absolute,
):
    """A JSON output pointing to the workspace must never hang discovery.

    This can arise in conditional branches that will be skipped at runtime,
    because discovery examines their declarations before conditions resolve.
    """
    _flow(tmp_path)
    child = tmp_path / "child.toml"
    original = child.read_text(encoding="utf-8")
    replacement = tmp_path.resolve().as_posix() if as_absolute else "."
    child.write_text(
        original.replace(
            'output = "work/${input.item.id}.json"',
            f'output = "{replacement}"',
        ),
        encoding="utf-8",
    )
    fingerprints, paths = flow_module._snapshot_dynamic_checkpoint_candidates(
        workspace=tmp_path.resolve(),
        cache={"child": load_flow(child, workspace=tmp_path)},
    )
    assert fingerprints == {}
    assert paths == ()
