from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent.flow import load_flow


def _write_prompt(tmp_path: Path) -> None:
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")


def test_global_retry_applies_to_all_steps(tmp_path: Path) -> None:
    _write_prompt(tmp_path)
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[retry]
max_attempts = 4
initial_delay_seconds = 2
backoff_multiplier = 3
max_delay_seconds = 20

[[steps]]
id = "one"
prompt_file = "prompt.md"

[[steps]]
id = "two"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )

    flow = load_flow(tmp_path / "flow.toml", workspace=tmp_path)

    for step in flow.steps:
        assert step.retry_policy is not None
        assert step.retry_policy.max_attempts == 4
        assert step.retry_policy.initial_delay_seconds == 2
        assert step.retry_policy.backoff_multiplier == 3
        assert step.retry_policy.max_delay_seconds == 20


def test_step_retry_partially_overrides_global_retry(tmp_path: Path) -> None:
    _write_prompt(tmp_path)
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[retry]
max_attempts = 3
initial_delay_seconds = 2
backoff_multiplier = 2.5
max_delay_seconds = 30

[[steps]]
id = "one"
prompt_file = "prompt.md"

[steps.retry]
max_attempts = 5
max_delay_seconds = 60
""".strip(),
        encoding="utf-8",
    )

    flow = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    retry = flow.steps[0].retry_policy

    assert retry is not None
    assert retry.max_attempts == 5
    assert retry.initial_delay_seconds == 2
    assert retry.backoff_multiplier == 2.5
    assert retry.max_delay_seconds == 60


def test_step_retry_without_global_keeps_existing_defaults(tmp_path: Path) -> None:
    _write_prompt(tmp_path)
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
prompt_file = "prompt.md"

[steps.retry]
max_attempts = 3
""".strip(),
        encoding="utf-8",
    )

    flow = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    retry = flow.steps[0].retry_policy

    assert retry is not None
    assert retry.max_attempts == 3
    assert retry.initial_delay_seconds == 1.0
    assert retry.backoff_multiplier == 2.0
    assert retry.max_delay_seconds == 10.0



def test_partial_global_retry_uses_existing_defaults(tmp_path: Path) -> None:
    _write_prompt(tmp_path)
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[retry]
max_attempts = 2

[[steps]]
id = "one"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )

    flow = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    retry = flow.steps[0].retry_policy

    assert retry is not None
    assert retry.max_attempts == 2
    assert retry.initial_delay_seconds == 1.0
    assert retry.backoff_multiplier == 2.0
    assert retry.max_delay_seconds == 10.0


def test_no_retry_configuration_stays_disabled(tmp_path: Path) -> None:
    _write_prompt(tmp_path)
    (tmp_path / "flow.toml").write_text(
        """
version = 1

[[steps]]
id = "one"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )

    flow = load_flow(tmp_path / "flow.toml", workspace=tmp_path)

    assert flow.steps[0].retry_policy is None


@pytest.mark.parametrize(
    "retry_toml,match",
    [
        ('retry = "invalid"', "retry muss eine Tabelle"),
        ("[retry]\nunknown = 1", "retry enthält unbekannte Schlüssel"),
        ("[retry]\nmax_attempts = 0", "max_attempts"),
    ],
)
def test_global_retry_is_validated(
    tmp_path: Path,
    retry_toml: str,
    match: str,
) -> None:
    _write_prompt(tmp_path)
    (tmp_path / "flow.toml").write_text(
        f"""
version = 1
{retry_toml}

[[steps]]
id = "one"
prompt_file = "prompt.md"
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match=match):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)
