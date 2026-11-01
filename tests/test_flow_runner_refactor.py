"""Behavioral guardrails for extracted flow iteration snapshot handling."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import cli_agent.workflows.flow as flow


def test_dynamic_subflow_iteration_does_not_read_new_previous_output(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    step = SimpleNamespace(foreach=None)
    monkeypatch.setattr(flow, "_uses_previous_output", lambda _: True)

    def should_not_read(*args, **kwargs):
        raise AssertionError("late output must not be read as a run-start snapshot")

    monkeypatch.setattr(flow, "_previous_output_from_snapshot", should_not_read)
    assert flow._resolve_iteration_previous_output(
        step, workspace=tmp_path, output=tmp_path / "late.json",
        invocation_known_at_run_start=False,
        initial_previous_output_fingerprints={},
    ) is None


def test_known_iteration_uses_original_previous_output_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    step = SimpleNamespace(foreach=None)
    original = {"checkpoint": b"fingerprint"}
    seen = {}

    def read_snapshot(received_step, *, workspace, output, fingerprints):
        seen.update(step=received_step, workspace=workspace, output=output,
                    fingerprints=fingerprints)
        return '{"old":true}'

    monkeypatch.setattr(flow, "_previous_output_from_snapshot", read_snapshot)
    output = tmp_path / "state.json"
    assert flow._resolve_iteration_previous_output(
        step, workspace=tmp_path, output=output,
        invocation_known_at_run_start=True,
        initial_previous_output_fingerprints=original,
    ) == '{"old":true}'
    assert seen == {"step": step, "workspace": tmp_path,
                    "output": output, "fingerprints": original}


def test_foreach_previous_output_snapshot_error_keeps_context(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    step = SimpleNamespace(foreach="input.items")
    monkeypatch.setattr(flow, "_uses_previous_output", lambda _: True)

    def missing_snapshot(*args, **kwargs):
        raise ValueError("Snapshot fehlt")

    monkeypatch.setattr(flow, "_previous_output_from_snapshot", missing_snapshot)
    with pytest.raises(ValueError, match="Bei foreach muss die Quelle vollständig") as exc:
        flow._resolve_iteration_previous_output(
            step, workspace=tmp_path, output=tmp_path / "state.json",
            invocation_known_at_run_start=True,
            initial_previous_output_fingerprints={},
        )
    assert isinstance(exc.value.__cause__, ValueError)
    assert "Snapshot fehlt" in str(exc.value)
