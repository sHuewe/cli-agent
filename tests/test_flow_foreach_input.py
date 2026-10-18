"""Regression tests for direct foreach traversal of flow input collections."""
from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent.flow import _foreach_items, load_flow


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
    with pytest.raises(ValueError, match="keine Liste"):
        _foreach_items(
            flow.steps[0], outputs={},
            flow_input={"structure": {"folders": {}}},
        )


def test_foreach_input_rejects_undeclared_root(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="structure"):
        _flow(tmp_path, selector="input.structure.folders")
        # The valid declared case is tested above.


@pytest.mark.parametrize("source", [
    "input", "input.structure", "input.structure.folders[]",
    "input.structure.folders[0]", "item.folders",
])
def test_foreach_input_rejects_bad_syntax(tmp_path: Path, source: str) -> None:
    with pytest.raises(ValueError, match="foreach"):
        _flow(tmp_path, selector=source)
