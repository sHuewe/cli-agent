from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli_agent.workflows.flow as flow_module
from cli_agent.workflows.flow import load_flow, run_flow


def _write(tmp_path: Path, name: str, content: str) -> None:
    (tmp_path / name).write_text(content.strip() + "\n", encoding="utf-8")


def _capture_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls = []

    async def fake_run_once(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer="done", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_once", fake_run_once)
    flow = load_flow(tmp_path / "parent.toml", workspace=tmp_path)
    asyncio.run(run_flow(flow, workspace=tmp_path))
    return calls


@pytest.mark.parametrize(
    ("parent_retry", "child_retry", "grand_retry", "leaf_retry", "expected"),
    [
        ("", "", "", "", None),
        ("[retry]\nmax_attempts = 4\ninitial_delay_seconds = 0", "", "", "", (4, 0)),
        (
            "[retry]\nmax_attempts = 4",
            "[retry]\nmax_attempts = 2\ninitial_delay_seconds = 7",
            "",
            "",
            (2, 7),
        ),
        (
            "[retry]\nmax_attempts = 4",
            "",
            "[retry]\nmax_attempts = 3\ninitial_delay_seconds = 6",
            "",
            (3, 6),
        ),
        (
            "[retry]\nmax_attempts = 4",
            "[retry]\nmax_attempts = 3\ninitial_delay_seconds = 5",
            "",
            "[steps.retry]\nmax_attempts = 1",
            (1, 5),
        ),
        (
            "[retry]\nmax_attempts = 4",
            "",
            "",
            "[steps.retry]\nmax_attempts = 1",
            (1, 1),
        ),
    ],
)
def test_nested_subflow_retry_inheritance_and_overrides(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    parent_retry: str,
    child_retry: str,
    grand_retry: str,
    leaf_retry: str,
    expected: tuple[int, int] | None,
) -> None:
    _write(tmp_path, "work.md", "work")
    _write(
        tmp_path,
        "parent.toml",
        f"""
version = 1
{parent_retry}

[[steps]]
id = "child"
flow = "child.toml"
""",
    )
    _write(
        tmp_path,
        "child.toml",
        f"""
version = 1
result = "steps.grand.output"
{child_retry}

[[steps]]
id = "grand"
flow = "grand.toml"
""",
    )
    _write(
        tmp_path,
        "grand.toml",
        f"""
version = 1
result = "steps.work.output"
{grand_retry}

[[steps]]
id = "work"
prompt_file = "work.md"
{leaf_retry}
""",
    )
    calls = _capture_once(tmp_path, monkeypatch)

    assert len(calls) == 1
    policy = calls[0].retry_policy
    if expected is None:
        assert policy is None
    else:
        assert policy is not None
        assert policy.max_attempts == expected[0]
        assert policy.initial_delay_seconds == expected[1]


def test_shared_subflow_does_not_cache_parent_retry_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, "work.md", "work")
    _write(
        tmp_path,
        "parent.toml",
        """
version = 1

[[steps]]
id = "left"
flow = "left.toml"

[[steps]]
id = "right"
flow = "right.toml"
""",
    )
    for name, attempts in (("left", 2), ("right", 5)):
        _write(
            tmp_path,
            name + ".toml",
            f"""
version = 1
result = "steps.shared.output"

[retry]
max_attempts = {attempts}

[[steps]]
id = "shared"
flow = "shared.toml"
""",
        )
    _write(
        tmp_path,
        "shared.toml",
        """
version = 1
result = "steps.work.output"

[[steps]]
id = "work"
prompt_file = "work.md"
""",
    )

    calls = _capture_once(tmp_path, monkeypatch)

    assert len(calls) == 2
    assert [call.retry_policy.max_attempts for call in calls] == [2, 5]


def test_inherited_retry_reaches_conversation_steps(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, "work.md", "work")
    _write(
        tmp_path,
        "parent.toml",
        """
version = 1

[retry]
max_attempts = 3
initial_delay_seconds = 0

[[steps]]
id = "child"
flow = "child.toml"
""",
    )
    _write(
        tmp_path,
        "child.toml",
        """
version = 1
result = "steps.work.output"

[[steps]]
id = "work"
prompt_file = "work.md"
conversation_items = ["first", "second"]
""",
    )
    calls = []

    async def fake_run_conversation(options, *, dependencies=None):
        calls.append(options)
        return SimpleNamespace(answer="done", web_context_statuses=())

    monkeypatch.setattr(flow_module, "run_conversation", fake_run_conversation)
    definition = load_flow(tmp_path / "parent.toml", workspace=tmp_path)
    asyncio.run(run_flow(definition, workspace=tmp_path))

    assert len(calls) == 1
    assert calls[0].retry_policy is not None
    assert calls[0].retry_policy.max_attempts == 3
    assert calls[0].retry_policy.initial_delay_seconds == 0
    assert len(calls[0].prompts) == 2
