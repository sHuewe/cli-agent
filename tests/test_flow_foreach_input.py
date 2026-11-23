"""Regression tests for direct foreach traversal of flow input collections."""
from __future__ import annotations

from pathlib import Path
import json

import pytest

from cli_agent.workflows.flow import _foreach_items, load_flow


def _flow(tmp_path: Path, selector: str = "input.structure.folders"):
    (tmp_path / "prompt.md").write_text("Folder {{var:folder}}", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        'version = 1\ninputs = ["structure"]\n'
        '[[steps]]\nid = "route"\nprompt_file = "prompt.md"\n'
        f'foreach = "{selector}"\n'
        'iteration_id = "${item.id}"\n'
        'output = "route-${iteration.id}.json"\n'
        'response_format = "json"\n'
        '[steps.vars]\nfolder = "${item}"\n',
        encoding="utf-8",
    )
    return load_flow(Path("flow.toml"), workspace=tmp_path)


def test_foreach_reads_nested_input_list_without_step_output(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    folders = [{"id": "foundations"}, {"id": "publications"}]
    assert _foreach_items(
        flow.steps[0],
        outputs={},
        flow_input={"structure": {"folders": folders}},
    ) == folders


def test_foreach_input_list_empty(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    assert _foreach_items(
        flow.steps[0], outputs={}, flow_input={"structure": {"folders": []}},
    ) == []


def test_foreach_input_rejects_missing_or_nonlist(tmp_path: Path) -> None:
    flow = _flow(tmp_path)
    with pytest.raises(ValueError, match="folders"):
        _foreach_items(flow.steps[0], outputs={}, flow_input={"structure": {}})
    with pytest.raises(ValueError, match="foreach source 'input\\.structure\\.folders' is not a list\\."):
        _foreach_items(
            flow.steps[0], outputs={},
            flow_input={"structure": {"folders": {}}},
        )


def test_foreach_input_rejects_undeclared_root(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("Test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        'version = 1\n[[steps]]\nid = "route"\n'
        'prompt_file = "prompt.md"\n'
        'foreach = "input.structure.folders"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="structure"):
        load_flow(Path("flow.toml"), workspace=tmp_path)


@pytest.mark.parametrize("source", [
    "input", "input.structure.folders[]",
    "input.structure.folders[0]", "item.folders",
])
def test_foreach_input_rejects_bad_syntax(tmp_path: Path, source: str) -> None:
    with pytest.raises(ValueError, match="foreach"):
        _flow(tmp_path, selector=source)


@pytest.mark.parametrize("configured,expected", [
    (None, 1000), (1500, 1500), (2000, 2000), (10000, 10000),
])
def test_foreach_limit_configuration(tmp_path: Path, configured, expected) -> None:
    (tmp_path / "prompt.md").write_text("Test", encoding="utf-8")
    limits = "" if configured is None else f"[limits]\nmax_foreach_items = {configured}\n"
    (tmp_path / "flow.toml").write_text(
        f'version = 1\ninputs = ["items"]\n{limits}'
        '[[steps]]\nid = "route"\nprompt_file = "prompt.md"\n'
        'foreach = "input.items"\niteration_id = "${item.id}"\n'
        'output = "out-${iteration.id}.json"\nresponse_format = "json"\n',
        encoding="utf-8",
    )
    flow = load_flow(Path("flow.toml"), workspace=tmp_path)
    assert flow.max_foreach_items == expected
    assert flow.steps[0].max_foreach_items == expected
    items = [{"id": str(i)} for i in range(expected)]
    assert len(_foreach_items(flow.steps[0], outputs={}, flow_input={"items": items})) == expected
    with pytest.raises(ValueError, match=rf"at most {expected}"):
        _foreach_items(
            flow.steps[0], outputs={},
            flow_input={"items": [*items, {"id": "overflow"}]},
        )


def test_foreach_step_output_allows_large_configured_collection(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("Process", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        'version = 1\n[limits]\nmax_foreach_items = 2000\n'
        '[[steps]]\nid = "discover"\nprompt_file = "prompt.md"\n'
        'response_format = "json"\noutput = "items.json"\n'
        '[[steps]]\nid = "process"\nprompt_file = "prompt.md"\n'
        'foreach = "steps.discover.output.items"\n'
        'iteration_id = "${item.id}"\noutput = "out-${iteration.id}.json"\n',
        encoding="utf-8",
    )
    flow = load_flow(Path("flow.toml"), workspace=tmp_path)
    outputs = {"discover": json.dumps({
        "items": [{"id": str(i)} for i in range(1430)],
    })}
    assert len(_foreach_items(
        flow.steps[1], outputs=outputs, flow_input={},
    )) == 1430


@pytest.mark.parametrize("setting", [
    "0", "-1", "10001", "true", '"2000"', "2.5",
])
def test_foreach_limit_rejects_invalid_values(tmp_path: Path, setting: str) -> None:
    (tmp_path / "prompt.md").write_text("Test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        f'version = 1\n[limits]\nmax_foreach_items = {setting}\n'
        '[[steps]]\nid = "route"\nprompt_file = "prompt.md"\n'
        'foreach = "input.items"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"limits.max_foreach_items"):
        load_flow(Path("flow.toml"), workspace=tmp_path)


def test_foreach_limit_rejects_unknown_configuration(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("Test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        'version = 1\n[limits]\nunknown = 2000\n'
        '[[steps]]\nid = "route"\nprompt_file = "prompt.md"\n',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match=r"limits supports only"):
        load_flow(Path("flow.toml"), workspace=tmp_path)
