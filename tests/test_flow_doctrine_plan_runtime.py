from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from cli_agent.workflows.flow import load_flow, run_flow


def test_doctrine_batch_plan_runs_without_final_llm_merge(tmp_path, monkeypatch):
    from cli_agent.workflows import flow as module

    (tmp_path / "empty.md").write_text("No model run", encoding="utf-8")
    (tmp_path / "detail.md").write_text(
        "Only concepts: {{var:batch}}", encoding="utf-8",
    )
    outline = {
        "status": "ok",
        "source_path": "sources/doc.md",
        "folder": {"id": "doc-123", "path": "doctrines/doc-123"},
        "folders": [],
        "concepts": [{
            "id": "rules",
            "path": "doctrines/doc-123/rules.md",
            "title": "Rules",
            "description": "Rules",
            "type": "concept",
            "tags": [],
            "section_ids": [],
        }],
    }
    (tmp_path / "outline.json").write_text(json.dumps(outline), encoding="utf-8")
    (tmp_path / "analyses.json").write_text(
        '{"iterations":[]}', encoding="utf-8",
    )
    (tmp_path / "flow.toml").write_text("""
version = 1

[[steps]]
id = "outline"
prompt_file = "empty.md"
response_format = "json"
output = "outline.json"

[[steps]]
id = "analyses"
prompt_file = "empty.md"
response_format = "json"
output = "analyses.json"

[[steps]]
id = "batches"
output = "batches.json"
[steps.json_operation]
kind = "prepare_doctrine_batches"
input = "structure"
results_step = "analyses"
batch_size = 3
[steps.json_inputs.structure]
path = "outline.json"

[[steps]]
id = "details"
prompt_file = "detail.md"
foreach = "steps.batches.output.batches"
iteration_id = "${item.id}"
response_format = "json"
output = "detail-${iteration.id}.json"
[steps.vars]
batch = "${item}"

[[steps]]
id = "plan"
output = "plan.json"
[steps.json_operation]
kind = "assemble_doctrine_plan"
input = "structure"
results_step = "details"
batches_step = "batches"
[steps.json_inputs.structure]
path = "outline.json"
""".strip(), encoding="utf-8")
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options.prompt)
        answer = json.dumps({
            "status": "ok",
            "concepts": [{
                "id": "rules",
                "evidence_ranges": [{
                    "doctrine": "sources/doc.md",
                    "start_line": 2,
                    "end_line": 3,
                }],
            }],
        })
        if options.output is not None:
            options.output.write_text(answer, encoding="utf-8")
        return SimpleNamespace(answer=answer, web_context_statuses=())

    monkeypatch.setattr(module, "run_once", fake_run_once)
    flow = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    asyncio.run(run_flow(flow, workspace=tmp_path))
    plan = json.loads((tmp_path / "plan.json").read_text(encoding="utf-8"))
    assert len(calls) == 1
    assert len(plan["concepts"]) == 1
    assert plan["concepts"][0]["evidence_ranges"][0]["start_line"] == 2
    asyncio.run(run_flow(flow, workspace=tmp_path))
    assert len(calls) == 1
