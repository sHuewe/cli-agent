"""Regression tests for side-effect-free Flow iteration eligibility."""
from types import SimpleNamespace

from cli_agent.workflows.flow import _eligible_step_iterations


def test_no_foreach_invocation_has_single_iteration():
    step = SimpleNamespace(foreach=None, iteration_id=None, when=None)
    assert _eligible_step_iterations(
        step, outputs={}, statuses={}, flow_input={}, output_json={},
    ) == ([None], [None])


def test_empty_foreach_remains_empty_not_a_skipped_step():
    step = SimpleNamespace(foreach="input.items", iteration_id=None, when=None)
    assert _eligible_step_iterations(
        step, outputs={}, statuses={}, flow_input={"items": []}, output_json={},
    ) == ([], [])


def test_foreach_filter_preserves_original_iteration_identifiers():
    step = SimpleNamespace(
        foreach="input.items", iteration_id=None,
        when={"ref": "item.enabled", "eq": True},
    )
    items, ids = _eligible_step_iterations(
        step,
        outputs={}, statuses={}, output_json={},
        flow_input={"items": [
            {"enabled": True}, {"enabled": False}, {"enabled": True},
        ]},
    )
    assert items == [{"enabled": True}, {"enabled": True}]
    assert ids == ["1", "3"]
