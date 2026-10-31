"""Typed, side-effect-free domain model for Flow definitions and executions."""
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from cli_agent.models.model import ModelRetryPolicy
from cli_agent.platform.execution import ExecutionDependencies, ApprovalCallback
from cli_agent.state.agent_types import UserMessageCallback

MAX_SUBFLOW_GRAPH_EXPANSIONS = 10_000

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


@dataclass
class _FlowExecutionContext:
    dependencies: ExecutionDependencies
    approval_callback: ApprovalCallback | None
    user_message_callback: UserMessageCallback | None
    executed_steps: int = 0
    claimed_outputs: dict[str, str] | None = None
    reserved_inputs: dict[str, Path] | None = None
    flow_cache: dict[str, FlowDefinition] | None = None
    run_start_snapshots: dict[str, "_RunStartSnapshot"] | None = None
    run_start_checkpoint_paths: tuple[Path, ...] = ()
    run_start_dynamic_checkpoints: dict[tuple[str, str, str], bytes] | None = None
    completed_json_outputs: dict[str, bytes] | None = None
    preflight_budget: "_SubflowTraversalBudget" | None = None

    def __post_init__(self) -> None:
        if self.claimed_outputs is None:
            self.claimed_outputs = {}
        if self.reserved_inputs is None:
            self.reserved_inputs = {}
        if self.flow_cache is None:
            self.flow_cache = {}
        if self.run_start_snapshots is None:
            self.run_start_snapshots = {}
        if self.preflight_budget is None:
            self.preflight_budget = _SubflowTraversalBudget()
        if self.completed_json_outputs is None:
            self.completed_json_outputs = {}


@dataclass
class _SubflowTraversalBudget:
    expanded_nodes: int = 0

    def consume(self, *, purpose: str) -> None:
        self.expanded_nodes += 1
        if self.expanded_nodes > MAX_SUBFLOW_GRAPH_EXPANSIONS:
            raise ValueError(
                f"Subflow-{purpose} überschreitet das Expansionslimit von "
                f"{MAX_SUBFLOW_GRAPH_EXPANSIONS} Flow-Knoten."
            )


@dataclass(frozen=True)
class _RunStartSnapshot:
    checkpoint_fingerprints: dict[tuple[str, str], bytes]
    checkpoint_paths: tuple[Path, ...]
    previous_output_fingerprints: dict[tuple[str, str], bytes | None]
    available_outputs: dict[str, str]
    output_json: dict[str, bool]


