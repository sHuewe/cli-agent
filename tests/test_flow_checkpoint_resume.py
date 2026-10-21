"""Regression tests for deterministic checkpoint reuse with write-on-miss."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli_agent.flow as flow_module
from cli_agent.flow import load_flow, run_flow


def _make_flow(root: Path) -> Path:
    (root / "prompt.md").write_text("{{var:value}}", encoding="utf-8")
    (root / "child.toml").write_text(
        """version = 1
inputs = ["source"]
result = "steps.resume.output.verification"
result_fallback = "steps.verify.output"

[[steps]]
id = "resume"
output = "resume.json"
overwrite_output = true
[steps.filesystem_operation]
kind = "checkpoint_status"
paths = ["old-v2.json", "old-v3.json"]
expected_status = "ok"
expected_source_path = "${input.source}"
release_source_on_miss = true

[[steps]]
id = "write"
prompt_file = "prompt.md"
workspace_access = "write"
when = { ref = "steps.resume.output.completed", eq = false }
[steps.vars]
value = "write"

[[steps]]
id = "verify"
prompt_file = "prompt.md"
response_format = "json"
when = { ref = "steps.resume.output.completed", eq = false }
[steps.vars]
value = "verify"
""",
        encoding="utf-8",
    )
    root_flow = root / "flow.toml"
    root_flow.write_text(
        'version = 1\n[[steps]]\nid = "child"\nflow = "child.toml"\n'
        '[steps.input]\nsource = "concept.md"\n',
        encoding="utf-8",
    )
    (root / "concept.md").write_text("# Concept", encoding="utf-8")
    return root_flow


@pytest.mark.parametrize("valid", [True, False])
def test_guarded_legacy_reuse_or_write_on_miss(tmp_path, monkeypatch, valid):
    root = _make_flow(tmp_path)
    if valid:
        (tmp_path / "old-v2.json").write_text(
            '{"status":"ok","source_path":"concept.md","checked_links":1,"findings":[]}',
            encoding="utf-8",
        )
    called = []

    async def fake_run(options, *, dependencies=None):
        called.append(options.prompt)
        if options.prompt == "write":
            assert (tmp_path / "concept.md") not in options.mutation_protected_paths
        return SimpleNamespace(
            answer='{"status":"ok","source_path":"concept.md","checked_links":0,"findings":[]}',
            web_context_statuses=(),
        )

    monkeypatch.setattr(flow_module, "run_once", fake_run)
    asyncio.run(run_flow(load_flow(root, workspace=tmp_path), workspace=tmp_path))
    assert called == ([] if valid else ["write", "verify"])
    status = json.loads((tmp_path / "resume.json").read_text(encoding="utf-8"))
    assert status["completed"] is valid
    assert status["verification"]["status"] == "ok" if valid else status["verification"] is None


def test_source_release_requires_first_unconditional_step(tmp_path):
    path = _make_flow(tmp_path)
    child = tmp_path / "child.toml"
    content = child.read_text(encoding="utf-8")
    child.write_text(content.replace('release_source_on_miss = true',
                                    'release_source_on_miss = false'), encoding="utf-8")
    with pytest.raises(ValueError, match="release_source_on_miss"):
        load_flow(child, workspace=tmp_path)


def test_fallback_result_requires_primary(tmp_path):
    path = tmp_path / "bad.toml"
    path.write_text(
        'version = 1\nresult_fallback = "steps.work.output"\n'
        '[[steps]]\nid = "work"\nprompt_file = "prompt.md"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="result_fallback"):
        load_flow(path, workspace=tmp_path)
