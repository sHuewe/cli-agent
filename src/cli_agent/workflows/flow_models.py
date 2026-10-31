"""Validated Flow definition value objects and run-start snapshot data."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from cli_agent.models.model import ModelRetryPolicy

@dataclass(frozen=True)
class JsonInputSpec:
    path: str
    iterate: str | None = None


@dataclass(frozen=True)
class FlowStep:
    step_id: str
    config: Path | None
    model: str | None
    prompt_file: Path | None
    flow: Path | None
    flow_input: dict[str, Any]
    context_files: tuple[Path, ...]
    add_web_context: tuple[str, ...]
    output: str | None
    overwrite_output: bool
    workspace_access: str
    with_data: bool
    with_mcp: tuple[str, ...]
    retry_policy: ModelRetryPolicy | None
    approve_tools: tuple[str, ...]
    variables: dict[str, str]
    global_variables: dict[str, str]
    foreach: str | None
    json_inputs: dict[str, JsonInputSpec] | None = None
    json_operation: dict[str, Any] | None = None
    filesystem_operation: dict[str, Any] | None = None
    conversation_items: tuple[Any, ...] | str | None = None
    conversation_final_prompt_file: Path | None = None
    iteration_id: str | None = None
    excluded_paths: tuple[Path, ...] = ()
    response_format: str = "text"
    # Explicit [steps.retry] keys, before load-time defaults are applied.
    retry_overrides: dict[str, int | float] | None = None
    when: dict[str, Any] | None = None


@dataclass(frozen=True)
class FlowDefinition:
    source: Path
    steps: tuple[FlowStep, ...]
    excluded_paths: tuple[Path, ...] = ()
    inputs: tuple[str, ...] = ()
    result: str | None = None
    result_fallback: str | None = None
    # Root-level [retry], separate from effective per-step defaults.
    retry_policy: ModelRetryPolicy | None = None


@dataclass(frozen=True)
class _FlowResultValue:
    text: str
    is_json: bool


@dataclass(frozen=True)
class _RunStartSnapshot:
    checkpoint_fingerprints: dict[tuple[str, str], bytes]
    checkpoint_paths: tuple[Path, ...]
    previous_output_fingerprints: dict[tuple[str, str], bytes | None]
    available_outputs: dict[str, str]
    output_json: dict[str, bool]


