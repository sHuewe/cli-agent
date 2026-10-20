from __future__ import annotations

import argparse
import asyncio
import contextvars
import logging
import hashlib
import json
import os
import re
import sys
import tomllib
import traceback
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any, Callable

from . import __version__
from .cli import _show_mcp_user_message, approve_tool_call
from .admin_config import AdminConfig, load_admin_config
from .config import AppConfig, default_config_file, load_config
from .execution import (
    ApprovalCallback,
    ExecutionDependencies,
    ConversationRunOptions,
    OneShotRunOptions,
    apply_named_mcp_overrides,
    build_preapproval_callback,
    resolve_excluded_paths,
    run_conversation,
    run_once,
)
from .file_context import (
    is_local_agent_command,
    prepare_file_options,
    prepare_output_target,
    prepare_prompt_file,
    read_existing_output_text,
)
from .flow_json_operations import transform_json
from .flow_filesystem_operations import inventory_directory, MAX_INVENTORY_ENTRIES, _workspace_directory
from .flow_json_grouping import group_assigned_items, verify_assigned_coverage
from .filesystem_security import path_entry_is_symlink_or_reparse
from .logging_setup import configure_logging
from .model import ModelRetryPolicy
from .prompt_template import PromptTemplate
from .terminal_output import sanitize_terminal_text
from .agent_types import UserMessageCallback

@dataclass
class _FlowDiagnosticState:
    """Mutable state shared with tasks spawned from the current CLI context."""

    details: str = ""


_flow_diagnostic_context: contextvars.ContextVar[
    _FlowDiagnosticState | None
] = contextvars.ContextVar("flow_diagnostic_context", default=None)
_flow_logger = logging.getLogger(__name__)


def _set_flow_diagnostic(
    *, phase: str, flow: Path, namespace: str = "",
    step: str | None = None, iteration_id: str | None = None,
    output: Path | None = None,
) -> None:
    parts = [f"phase={phase}", f"flow={flow}"]
    if step is not None:
        parts.append(f"step={namespace}{step}")
    if iteration_id is not None:
        parts.append(f"iteration={iteration_id}")
    if output is not None:
        parts.append(f"output={output}")
    state = _flow_diagnostic_context.get()
    if state is None:
        state = _FlowDiagnosticState()
        _flow_diagnostic_context.set(state)
    # asyncio copies the context but preserves references to mutable values.
    state.details = " | ".join(parts)


MAX_FLOW_STEPS = 100
MAX_FOREACH_ITEMS = 1000
MAX_FLOW_FILE_BYTES = 1_000_000
MAX_STRUCTURED_OUTPUT_BYTES = 100_000_000
MAX_JSON_INPUT_FILES = 1000
MAX_JSON_INPUT_TOTAL_BYTES = 100_000_000
MAX_SUBFLOW_DEPTH = 8
MAX_FLOW_EXECUTIONS = 10_000
MAX_SUBFLOW_GRAPH_EXPANSIONS = 10_000
MAX_DYNAMIC_CHECKPOINT_CANDIDATES = 20_000
MAX_DYNAMIC_CHECKPOINT_TOTAL_BYTES = 200_000_000

_STEP_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
_ITEM_EXPR = re.compile(
    r"\$\{item(?:\.([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?\}"
)
_ITERATION_ID_EXPR = re.compile(r"\$\{iteration\.id\}")
_CONVERSATION_ITEM_EXPR = re.compile(
    r"\$\{conversation\.item(?:\.([A-Za-z_][A-Za-z0-9_-]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?\}"
)
_PREVIOUS_OUTPUT_EXPR = re.compile(
    r"\$\{previous_output(?:\.([A-Za-z_][A-Za-z0-9_-]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?\}"
)
_INPUT_EXPR = re.compile(
    r"\$\{input(?:\.([A-Za-z_][A-Za-z0-9_-]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?\}"
)
_DYNAMIC_VALUE_EXPR = re.compile(
    r"\$\{(?:(?P<iteration>iteration\.id)"
    r"|item(?:\.(?P<item_path>[A-Za-z_][A-Za-z0-9_-]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?"
    r"|conversation\.item(?:\.(?P<conversation_path>[A-Za-z_][A-Za-z0-9_-]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?"
    r"|previous_output(?:\.(?P<previous_output_path>[A-Za-z_][A-Za-z0-9_-]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?"
    r"|input(?:\.(?P<input_path>[A-Za-z_][A-Za-z0-9_-]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?"
    r"|steps\.(?P<step_output_id>[A-Za-z_][A-Za-z0-9_-]*)\.output"
    r"(?:\.(?P<step_output_path>[A-Za-z_][A-Za-z0-9_-]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?)\}"
)
_JSON_INPUT_ITERATE = re.compile(
    r"(?:input|item)(?:\.[A-Za-z_][A-Za-z0-9_-]*)+"
    r"(?:\[\](?:\.[A-Za-z_][A-Za-z0-9_-]*)*)*\[\]\Z"
)
_JSON_INPUT_TEMPLATE = re.compile(
    r"\$\{(?:input(?:\.[A-Za-z_][A-Za-z0-9_-]*)+"
    r"|item(?:\.[A-Za-z_][A-Za-z0-9_-]*)*"
    r"|iteration\.id|each(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)\}"
)
_ITERATION_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_FOREACH = re.compile(
    r"steps\.([A-Za-z_][A-Za-z0-9_-]*)\.output"
    r"(?:\.([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?\Z"
)


# Foreach may also consume an existing list from the current flow invocation.
_INPUT_FOREACH = re.compile(
    r"input\.([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*)\Z"
)


def _foreach_output_source(source: str) -> str | None:
    """Return the producing step ID, or None for a flow-input list."""
    match = _FOREACH.fullmatch(source)
    if match is not None:
        return match.group(1)
    if _INPUT_FOREACH.fullmatch(source):
        return None
    raise ValueError(f"Ungültige foreach-Quelle: {source!r}.")


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


@dataclass(frozen=True)
class FlowDefinition:
    source: Path
    steps: tuple[FlowStep, ...]
    excluded_paths: tuple[Path, ...] = ()
    inputs: tuple[str, ...] = ()
    result: str | None = None
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


def _reject_parent_reference(path: Path, *, purpose: str) -> None:
    text = str(path)
    if ".." in PurePath(text).parts or ".." in PureWindowsPath(text).parts:
        raise ValueError(f"{purpose} darf '..' nicht enthalten.")


def _workspace_path(
    workspace: Path,
    value: str | Path,
    *,
    purpose: str,
    must_exist: bool,
) -> Path:
    raw = Path(value).expanduser()
    _reject_parent_reference(raw, purpose=purpose)
    windows = PureWindowsPath(str(raw))
    if windows.drive and not raw.is_absolute():
        raise ValueError(
            f"{purpose} verwendet ein nicht unterstütztes Windows-Laufwerk."
        )

    lexical = raw if raw.is_absolute() else workspace / raw
    try:
        if path_entry_is_symlink_or_reparse(lexical):
            raise ValueError(
                f"{purpose} darf kein Symlink oder Reparse Point sein: {lexical}"
            )
    except OSError as exc:
        raise ValueError(
            f"{purpose} konnte nicht sicher auf Dateisystem-Indirection geprüft werden: "
            f"{lexical}"
        ) from exc

    try:
        resolved = lexical.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise ValueError(
            f"{purpose} konnte nicht aufgelöst werden: {value}: {exc}"
        ) from exc
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(
            f"{purpose} muss innerhalb des festen Workspace liegen: {value}"
        ) from exc
    return resolved


def _ensure_output_parent_directory(workspace: Path, output: Path) -> None:
    workspace = workspace.expanduser().resolve()
    try:
        relative_parent = output.parent.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(
            f"Output-Ordner muss innerhalb des festen Workspace liegen: {output.parent}"
        ) from exc

    current = workspace
    for part in relative_parent.parts:
        candidate = current / part
        if path_entry_is_symlink_or_reparse(candidate):
            raise ValueError(
                f"Output-Ordner darf keinen Symlink oder Reparse Point enthalten: "
                f"{candidate}"
            )
        try:
            candidate.mkdir()
        except FileExistsError:
            pass
        except OSError as exc:
            raise ValueError(
                f"Output-Ordner konnte nicht erstellt werden: {candidate}: {exc}"
            ) from exc

        if path_entry_is_symlink_or_reparse(candidate) or not candidate.is_dir():
            raise ValueError(
                f"Output-Ordner ist kein sicherer regulärer Ordner: {candidate}"
            )
        resolved = candidate.resolve()
        try:
            resolved.relative_to(workspace)
        except ValueError as exc:
            raise ValueError(
                f"Output-Ordner muss innerhalb des festen Workspace liegen: "
                f"{candidate}"
            ) from exc
        current = resolved


def _flow_source(workspace: Path, path: Path) -> Path:
    resolved = _workspace_path(
        workspace,
        path,
        purpose="Flow-Datei",
        must_exist=True,
    )
    if not resolved.is_file():
        raise ValueError(f"Flow-Datei ist keine reguläre Datei: {resolved}")
    return resolved


def _alternate_case_component(value: str) -> str | None:
    for index, char in enumerate(value):
        swapped = char.swapcase()
        if swapped != char:
            return value[:index] + swapped + value[index + 1 :]
    return None


def _filesystem_is_case_insensitive(path: Path) -> bool:
    """Detect case-insensitive identity using an existing path on that filesystem."""

    current = path
    while not current.exists() and current != current.parent:
        current = current.parent
    try:
        resolved = current.resolve(strict=True)
    except OSError:
        return False

    parts = list(resolved.parts)
    for index in range(len(parts) - 1, 0, -1):
        alternate = _alternate_case_component(parts[index])
        if alternate is None:
            continue
        candidate = Path(
            *parts[:index],
            alternate,
            *parts[index + 1 :],
        )
        try:
            if candidate.exists() and os.path.samefile(resolved, candidate):
                return True
        except OSError:
            continue
    return False


def _filesystem_path_key(path: Path) -> str:
    resolved = path.resolve(strict=False)
    value = str(resolved)
    if _filesystem_is_case_insensitive(
        resolved if resolved.exists() else resolved.parent
    ):
        return value.casefold()
    return value


def _case_colliding_step_ids(
    flow: FlowDefinition,
    *,
    workspace: Path,
) -> frozenset[str]:
    if not _filesystem_is_case_insensitive(workspace):
        return frozenset()

    groups: dict[str, list[str]] = {}
    for step in flow.steps:
        groups.setdefault(step.step_id.casefold(), []).append(step.step_id)
    return frozenset(
        step_id
        for group in groups.values()
        if len(group) > 1
        for step_id in group
    )


def _dump_prefix_for_iteration(
    step: FlowStep,
    *,
    index: int,
    case_colliding_step_ids: frozenset[str],
    iteration_id: str | None = None,
) -> str:
    prefix = (
        f"{step.step_id}.{iteration_id}"
        if step.foreach is not None and iteration_id is not None
        else (
            f"{step.step_id}.foreach-{index}"
            if step.foreach is not None
            else step.step_id
        )
    )
    if step.step_id not in case_colliding_step_ids:
        return prefix

    digest = hashlib.sha256(
        step.step_id.encode("utf-8")
    ).hexdigest()[:16]
    return f"{prefix}.case-{digest}"


def _resolve_subflow_path(
    step: FlowStep,
    *,
    workspace: Path,
) -> Path:
    if step.flow is None:
        raise ValueError(f"Schritt {step.step_id!r} ist kein Subflow-Schritt.")
    return _workspace_path(
        workspace,
        step.flow,
        purpose=f"Subflow-Datei von Schritt {step.step_id!r}",
        must_exist=True,
    )


def _workspace_local_config_path(
    step: FlowStep,
    *,
    workspace: Path,
    flow_dir: Path,
) -> Path | None:
    config = _resolve_config(step, flow_dir=flow_dir).expanduser()
    try:
        resolved = config.resolve(strict=False)
    except OSError:
        return None
    try:
        resolved.relative_to(workspace)
    except ValueError:
        return None
    return resolved


def _reserved_flow_input_paths(
    flow: FlowDefinition,
    *,
    workspace: Path,
) -> dict[str, Path]:
    flow_dir = flow.source.parent
    reserved: dict[str, Path] = {
        _filesystem_path_key(flow.source): flow.source,
    }
    for step in flow.steps:
        _set_flow_diagnostic(
            phase="validate-reserved-input", flow=flow.source,
            step=step.step_id,
        )
        if step.flow is not None:
            subflow_path = _resolve_subflow_path(
                step,
                workspace=workspace,
            )
            reserved[_filesystem_path_key(subflow_path)] = subflow_path
            continue

        if step.workspace_access in {"read", "write"}:
            resolve_excluded_paths(
                workspace,
                tuple(
                    dict.fromkeys(
                        (
                            *flow.excluded_paths,
                            *step.excluded_paths,
                        )
                    )
                ),
            )

        if step.json_operation is not None or step.filesystem_operation is not None:
            continue
        assert step.prompt_file is not None
        prompt_path = _workspace_path(
            workspace,
            step.prompt_file,
            purpose=f"Prompt-Datei von Schritt {step.step_id!r}",
            must_exist=True,
        )
        reserved[_filesystem_path_key(prompt_path)] = prompt_path
        if step.conversation_final_prompt_file is not None:
            final_prompt_path = _workspace_path(
                workspace,
                step.conversation_final_prompt_file,
                purpose=(
                    f"Finale Conversation-Prompt-Datei von Schritt "
                    f"{step.step_id!r}"
                ),
                must_exist=True,
            )
            reserved[_filesystem_path_key(final_prompt_path)] = final_prompt_path

        for context_path in _contexts_for_step(
            step,
            workspace=workspace,
        ):
            reserved[_filesystem_path_key(context_path)] = context_path

        config_path = _workspace_local_config_path(
            step,
            workspace=workspace,
            flow_dir=flow_dir,
        )
        if config_path is not None:
            reserved[_filesystem_path_key(config_path)] = config_path
    return reserved


def _string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} muss ein nichtleerer String sein.")
    return value


def _iter_string_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_iter_string_values(item))
        return result
    if isinstance(value, dict):
        result: list[str] = []
        for item in value.values():
            result.extend(_iter_string_values(item))
        return result
    if isinstance(value, (bool, int, float)):
        return []
    raise ValueError(
        "Subflow-Input unterstützt nur TOML-Strings, Zahlen, boolesche Werte, "
        "Listen und Tabellen."
    )


def _validate_input_references(
    values: list[str],
    *,
    declared_inputs: tuple[str, ...],
    field: str,
) -> None:
    declared = set(declared_inputs)
    for value in values:
        for match in _INPUT_EXPR.finditer(value):
            path = match.group(1)
            if path is None:
                continue
            root = path.split(".", 1)[0]
            if root not in declared:
                raise ValueError(
                    f"{field} referenziert nicht deklarierten Flow-Input {root!r}."
                )


def _path_list(value: Any, *, field: str) -> tuple[Path, ...]:
    if value is None:
        return ()
    raw_values = value if isinstance(value, list) else [value]
    if not raw_values or not all(
        isinstance(item, str) and item.strip()
        for item in raw_values
    ):
        raise ValueError(
            f"{field} muss ein nichtleerer String oder eine Liste "
            "nichtleerer Strings sein."
        )
    values = tuple(item.strip() for item in raw_values)
    if len(values) != len(set(values)):
        raise ValueError(f"{field} darf keine doppelten Pfade enthalten.")
    if any(_ITEM_EXPR.search(item) for item in values):
        raise ValueError(
            f"{field} darf nicht aus foreach-Daten parametrisiert werden."
        )
    return tuple(Path(item) for item in values)


def load_flow(path: Path, *, workspace: Path) -> FlowDefinition:
    workspace = workspace.expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"Arbeitsordner existiert nicht: {workspace}")

    source = _flow_source(workspace, path)
    try:
        with source.open("rb") as handle:
            raw_flow = handle.read(MAX_FLOW_FILE_BYTES + 1)
        if len(raw_flow) > MAX_FLOW_FILE_BYTES:
            raise ValueError(
                f"Flow-Datei überschreitet das Limit von "
                f"{MAX_FLOW_FILE_BYTES} Bytes."
            )
        values = tomllib.loads(raw_flow.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError("Flow-Datei ist nicht als UTF-8 lesbar.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"Flow-Datei enthält ungültiges TOML: {exc}"
        ) from exc
    except OSError as exc:
        raise ValueError(
            f"Flow-Datei konnte nicht gelesen werden: {exc}"
        ) from exc

    allowed_root = {
        "version",
        "config",
        "model",
        "steps",
        "exclude_paths",
        "vars",
        "retry",
        "inputs",
        "result",
    }
    unknown_root = set(values) - allowed_root
    if unknown_root:
        raise ValueError(
            "Unbekannte Flow-Schlüssel: "
            + ", ".join(sorted(unknown_root))
        )
    if values.get("version") != 1:
        raise ValueError("Flow-Datei benötigt version = 1.")

    raw_inputs = values.get("inputs", [])
    if (
        not isinstance(raw_inputs, list)
        or not all(isinstance(value, str) and value.strip() for value in raw_inputs)
    ):
        raise ValueError("inputs muss eine Liste nichtleerer Namen sein.")
    inputs = tuple(value.strip() for value in raw_inputs)
    if len(inputs) != len(set(inputs)):
        raise ValueError("inputs darf keine doppelten Namen enthalten.")
    for name in inputs:
        if _STEP_ID.fullmatch(name) is None:
            raise ValueError(
                f"Ungültiger Flow-Input {name!r}; erlaubt sind ASCII-Buchstaben, "
                "Ziffern, '_' und '-' und der Name muss mit einem Buchstaben "
                "oder '_' beginnen."
            )

    result_value = values.get("result")
    result_selector = (
        _string(result_value, field="result")
        if result_value is not None
        else None
    )

    global_config_value = values.get("config")
    global_config = (
        Path(_string(global_config_value, field="config"))
        if global_config_value is not None
        else None
    )
    global_model_value = values.get("model")
    global_model = (
        _string(global_model_value, field="model")
        if global_model_value is not None
        else None
    )

    allowed_retry = {
        "max_attempts",
        "initial_delay_seconds",
        "backoff_multiplier",
        "max_delay_seconds",
    }
    raw_global_retry = values.get("retry")
    global_retry_policy: ModelRetryPolicy | None = None
    if raw_global_retry is not None:
        if not isinstance(raw_global_retry, dict):
            raise ValueError("retry muss eine Tabelle sein.")
        unknown_retry = set(raw_global_retry) - allowed_retry
        if unknown_retry:
            raise ValueError(
                "retry enthält unbekannte Schlüssel: "
                + ", ".join(sorted(unknown_retry))
            )
        global_retry_policy = ModelRetryPolicy(
            max_attempts=raw_global_retry.get("max_attempts", 1),
            initial_delay_seconds=raw_global_retry.get(
                "initial_delay_seconds",
                1.0,
            ),
            backoff_multiplier=raw_global_retry.get(
                "backoff_multiplier",
                2.0,
            ),
            max_delay_seconds=raw_global_retry.get(
                "max_delay_seconds",
                10.0,
            ),
        )

    raw_global_vars = values.get("vars", {})
    if not isinstance(raw_global_vars, dict):
        raise ValueError("vars muss eine Tabelle sein.")
    global_variables: dict[str, str] = {}
    for name, value in raw_global_vars.items():
        if not isinstance(name, str) or _STEP_ID.fullmatch(name) is None:
            raise ValueError(
                f"Ungültiger globaler Variablenname {name!r}; erlaubt sind "
                "ASCII-Buchstaben, Ziffern, '_' und '-' und der Name muss mit "
                "einem Buchstaben oder '_' beginnen."
            )
        if not isinstance(value, str):
            raise ValueError("vars darf nur String-zu-String-Werte enthalten.")
        if _DYNAMIC_VALUE_EXPR.search(value):
            raise ValueError(
                f"Globale Variable {name!r} darf keine dynamischen "
                "Flow-Platzhalter verwenden."
            )
        global_variables[name] = value

    excluded_paths = _path_list(
        values.get("exclude_paths"),
        field="exclude_paths",
    )

    raw_steps = values.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError(
            "Flow-Datei benötigt mindestens einen [[steps]]-Eintrag."
        )
    if len(raw_steps) > MAX_FLOW_STEPS:
        raise ValueError(
            f"Flow enthält mehr als {MAX_FLOW_STEPS} Schritte."
        )

    steps: list[FlowStep] = []
    known_ids: set[str] = set()
    allowed_step = {
        "id",
        "config",
        "model",
        "prompt_file",
        "context_file",
        "add_file_context",
        "add_web_context",
        "output",
        "overwrite_output",
        "workspace_access",
        "with_data",
        "with_mcp",
        "exclude_paths",
        "retry",
        "response_format",
        "approve_tools",
        "vars",
        "foreach",
        "conversation_items",
        "conversation_final_prompt_file",
        "iteration_id",
        "json_inputs",
        "json_operation",
        "filesystem_operation",
        "flow",
        "input",
    }

    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            raise ValueError(
                f"Flow-Schritt {index} muss eine Tabelle sein."
            )
        unknown = set(raw) - allowed_step
        if unknown:
            raise ValueError(
                f"Flow-Schritt {index} enthält unbekannte Schlüssel: "
                + ", ".join(sorted(unknown))
            )

        step_id = _string(
            raw.get("id"),
            field=f"steps[{index}].id",
        )
        if not _STEP_ID.fullmatch(step_id):
            raise ValueError(
                f"Ungültige Step-ID {step_id!r}; erlaubt sind "
                "ASCII-Buchstaben, Ziffern, '_' und '-'."
            )
        if step_id in known_ids:
            raise ValueError(f"Doppelte Step-ID: {step_id!r}.")
        known_ids.add(step_id)

        flow_value = raw.get("flow")
        child_flow = (
            Path(_string(flow_value, field=f"steps[{index}].flow"))
            if flow_value is not None
            else None
        )
        if child_flow is not None and _DYNAMIC_VALUE_EXPR.search(str(child_flow)):
            raise ValueError(
                f"steps[{index}].flow muss ein statischer Pfad sein und darf "
                "keine dynamischen Flow-Platzhalter enthalten."
            )
        prompt_value = raw.get("prompt_file")
        raw_filesystem_operation = raw.get("filesystem_operation")
        if raw_filesystem_operation is not None:
            op = raw_filesystem_operation
            if not isinstance(op, dict) or set(op) - {"kind", "directory", "recursive", "extensions", "exclude_names", "max_entries"} or op.get("kind") != "inventory" or not isinstance(op.get("directory"), str):
                raise ValueError("filesystem_operation benötigt kind=inventory und directory.")
            if type(op.get("recursive", False)) is not bool:
                raise ValueError("filesystem_operation.recursive muss boolesch sein.")
            for key in ("extensions", "exclude_names"):
                if not isinstance(op.get(key, []), list) or any(not isinstance(x, str) for x in op.get(key, [])):
                    raise ValueError(f"filesystem_operation.{key} muss eine String-Liste sein.")
            if any(not x.startswith(".") or len(x) < 2 or "/" in x or chr(92) in x for x in op.get("extensions", [])):
                raise ValueError("filesystem_operation.extensions enthält ungültige Endungen.")
            if any(not x or x in {".", ".."} or "/" in x or chr(92) in x for x in op.get("exclude_names", [])):
                raise ValueError("filesystem_operation.exclude_names enthält ungültige Namen.")
            limit = op.get("max_entries", MAX_INVENTORY_ENTRIES)
            if type(limit) is not int or not 1 <= limit <= MAX_INVENTORY_ENTRIES:
                raise ValueError("filesystem_operation.max_entries ist ungültig.")
            if child_flow is not None or prompt_value is not None or raw.get("json_operation") is not None:
                raise ValueError("filesystem_operation darf nicht mit flow, prompt_file oder json_operation kombiniert werden.")
            if raw.get("foreach") is not None or "output" not in raw:
                raise ValueError("filesystem_operation benötigt output ohne foreach.")
            forbidden = {"config", "model", "context_file", "add_file_context", "add_web_context", "workspace_access", "with_data", "with_mcp", "retry", "approve_tools", "vars", "conversation_items", "conversation_final_prompt_file", "exclude_paths", "json_inputs", "input"}
            if forbidden & set(raw):
                raise ValueError("filesystem_operation darf keine Modell- oder Tool-Optionen setzen.")
        if raw_filesystem_operation is not None:
            directory_template = raw_filesystem_operation["directory"]
            # This operation deliberately supports only declared flow inputs;
            # step/item expressions cannot be resolved by _render_input_text.
            remainder = _INPUT_EXPR.sub("", directory_template)
            if "${" in remainder:
                raise ValueError(
                    "filesystem_operation.directory unterstützt nur ${input...}-Referenzen."
                )
            _validate_input_references(
                [raw_filesystem_operation["directory"]],
                declared_inputs=inputs,
                field=f"steps[{index}].filesystem_operation.directory",
            )
        raw_json_operation = raw.get("json_operation")
        if raw_json_operation is not None:
            if (
                not isinstance(raw_json_operation, dict)
                or set(raw_json_operation) - {
                    "kind", "input", "batch_size", "max_batch_bytes", "max_concept_bytes"
                }
                or raw_json_operation.get("kind") not in {"batch", "batch_routes", "flatten", "group_items", "verify_coverage"}
                or not isinstance(raw_json_operation.get("input"), str)
                or not _STEP_ID.fullmatch(raw_json_operation["input"])
            ):
                raise ValueError(
                    f"steps[{index}].json_operation benötigt kind=batch|batch_routes|flatten|group_items|verify_coverage "
                    "und den Namen eines json_inputs enthalten."
                )
            size = raw_json_operation.get("batch_size", 5)
            max_bytes = raw_json_operation.get("max_batch_bytes", 80000)
            if (
                type(size) is not int or not 1 <= size <= 1000
                or type(max_bytes) is not int or not 1024 <= max_bytes <= 10000000
            ):
                raise ValueError("Ungültige JSON-Batch-Limits.")
            if (type(raw_json_operation.get("max_concept_bytes", 80000)) is not int
                    or not 1024 <= raw_json_operation.get("max_concept_bytes", 80000) <= 10000000):
                raise ValueError("Ungültiges max_concept_bytes.")
            required_sources = (
                {"routes", "assignments", "topics"}
                if raw_json_operation["kind"] == "group_items" else
                {"plan", "results"}
                if raw_json_operation["kind"] == "verify_coverage" else set()
            )
            if required_sources and (raw_json_operation["input"] != sorted(required_sources)[0]
                                     or not required_sources <= set(raw.get("json_inputs", {}))):
                raise ValueError("JSON-Gruppierungsoperation benötigt die vorgesehenen json_inputs.")
            if child_flow is not None or prompt_value is not None:
                raise ValueError(
                    "json_operation darf weder prompt_file noch flow kombinieren."
                )
        if child_flow is not None and prompt_value is not None:
            raise ValueError(
                f"steps[{index}] darf nicht gleichzeitig flow und prompt_file setzen."
            )
        if child_flow is None and prompt_value is None and raw_json_operation is None and raw_filesystem_operation is None:
            raise ValueError(
                f"steps[{index}] benötigt prompt_file, flow oder json_operation."
            )
        prompt_file = (
            Path(_string(prompt_value, field=f"steps[{index}].prompt_file"))
            if prompt_value is not None
            else None
        )

        raw_flow_input = raw.get("input", {})
        if not isinstance(raw_flow_input, dict):
            raise ValueError(f"steps[{index}].input muss eine Tabelle sein.")
        flow_input: dict[str, Any] = {}
        for name, value in raw_flow_input.items():
            if not isinstance(name, str) or _STEP_ID.fullmatch(name) is None:
                raise ValueError(
                    f"Ungültiger Input-Name {name!r} in steps[{index}].input."
                )
            flow_input[name] = value

        if raw_json_operation is not None:
            forbidden = {
                "config", "model", "context_file", "add_file_context",
                "add_web_context", "workspace_access", "with_data", "with_mcp",
                "retry", "approve_tools", "vars", "conversation_items",
                "conversation_final_prompt_file", "exclude_paths",
            }
            if forbidden & set(raw):
                raise ValueError(
                    "json_operation darf keine Modell-, Tool- oder "
                    "Workspace-Optionen konfigurieren."
                )
            if raw.get("response_format", "json") != "json":
                raise ValueError("json_operation benötigt response_format=json.")
            if "output" not in raw or raw.get("foreach") is not None:
                raise ValueError("json_operation benötigt output ohne foreach.")
        if child_flow is not None:
            regular_only = {
                "config", "model", "prompt_file", "context_file",
                "add_file_context", "add_web_context", "output",
                "overwrite_output", "workspace_access", "with_data",
                "with_mcp", "exclude_paths", "retry", "response_format",
                "approve_tools", "vars", "conversation_items",
                "conversation_final_prompt_file", "json_inputs", "json_operation",
            }
            conflicting = sorted(regular_only & set(raw))
            if conflicting:
                raise ValueError(
                    f"Subflow-Schritt {step_id!r} darf diese Schlüssel nicht setzen: "
                    + ", ".join(conflicting)
                )
        elif raw_flow_input:
            raise ValueError(
                f"steps[{index}].input ist nur zusammen mit flow erlaubt."
            )

        context_value = raw.get("context_file")
        add_file_context_value = raw.get("add_file_context")
        if context_value is not None and add_file_context_value is not None:
            raise ValueError(
                f"steps[{index}] darf nicht gleichzeitig context_file "
                "und add_file_context setzen."
            )
        effective_context_value = (
            add_file_context_value
            if add_file_context_value is not None
            else context_value
        )
        context_field = (
            f"steps[{index}].add_file_context"
            if add_file_context_value is not None
            else f"steps[{index}].context_file"
        )
        if effective_context_value is None:
            context_files: tuple[Path, ...] = ()
        else:
            raw_contexts = (
                effective_context_value
                if isinstance(effective_context_value, list)
                else [effective_context_value]
            )
            if not raw_contexts or not all(
                isinstance(value, str) and value.strip()
                for value in raw_contexts
            ):
                raise ValueError(
                    f"{context_field} muss ein nichtleerer String oder "
                    "eine Liste nichtleerer Strings sein."
                )
            context_strings = tuple(value.strip() for value in raw_contexts)
            if len(context_strings) != len(set(context_strings)):
                raise ValueError(
                    f"{context_field} darf keine doppelten Dateien enthalten."
                )
            if any(
                _ITEM_EXPR.search(value)
                or _CONVERSATION_ITEM_EXPR.search(value)
                or _PREVIOUS_OUTPUT_EXPR.search(value)
                for value in context_strings
            ):
                raise ValueError(
                    f"{context_field} darf nicht aus foreach-, Conversation- "
                    "oder previous_output-Daten parametrisiert werden."
                )
            context_files = tuple(Path(value) for value in context_strings)

        raw_web_context = raw.get("add_web_context", [])
        if (
            not isinstance(raw_web_context, list)
            or not all(
                isinstance(value, str) and value.strip()
                for value in raw_web_context
            )
        ):
            raise ValueError(
                f"steps[{index}].add_web_context muss eine Liste "
                "nichtleerer URLs sein."
            )
        add_web_context = tuple(
            value.strip()
            for value in raw_web_context
        )
        if len(add_web_context) != len(set(add_web_context)):
            raise ValueError(
                f"steps[{index}].add_web_context darf keine "
                "doppelten URLs enthalten."
            )
        if any(
            _ITEM_EXPR.search(value)
            or _CONVERSATION_ITEM_EXPR.search(value)
            or _PREVIOUS_OUTPUT_EXPR.search(value)
            for value in add_web_context
        ):
            raise ValueError(
                f"steps[{index}].add_web_context darf nicht aus foreach-, "
                "Conversation- oder previous_output-Daten parametrisiert werden."
            )

        config_value = raw.get("config")
        config = (
            Path(
                _string(
                    config_value,
                    field=f"steps[{index}].config",
                )
            )
            if config_value is not None
            else global_config
        )
        model_value = raw.get("model")
        model = (
            _string(
                model_value,
                field=f"steps[{index}].model",
            )
            if model_value is not None
            else global_model
        )

        output_value = raw.get("output")
        output = (
            _string(
                output_value,
                field=f"steps[{index}].output",
            )
            if output_value is not None
            else None
        )
        if output is not None and (
            _CONVERSATION_ITEM_EXPR.search(output)
            or _PREVIOUS_OUTPUT_EXPR.search(output)
        ):
            raise ValueError(
                f"steps[{index}].output darf nicht aus Conversation- oder "
                "previous_output-Daten parametrisiert werden."
            )
        overwrite_output = raw.get("overwrite_output", False)
        if not isinstance(overwrite_output, bool):
            raise ValueError(
                f"steps[{index}].overwrite_output muss boolesch sein."
            )

        workspace_access_value = raw.get("workspace_access", "none")
        if workspace_access_value not in {"none", "read", "write"}:
            raise ValueError(
                f"steps[{index}].workspace_access muss "
                "'none', 'read' oder 'write' sein."
            )
        workspace_access = str(workspace_access_value)

        with_data = raw.get("with_data", False)
        if not isinstance(with_data, bool):
            raise ValueError(
                f"steps[{index}].with_data muss boolesch sein."
            )

        raw_with_mcp = raw.get("with_mcp", [])
        if (
            not isinstance(raw_with_mcp, list)
            or not all(
                isinstance(name, str) and name.strip()
                for name in raw_with_mcp
            )
        ):
            raise ValueError(
                f"steps[{index}].with_mcp muss eine Liste "
                "nichtleerer MCP-Servernamen sein."
            )
        with_mcp = tuple(name.strip() for name in raw_with_mcp)
        if any("${" in name for name in with_mcp):
            raise ValueError(
                f"steps[{index}].with_mcp darf keine dynamischen "
                "Flow-Platzhalter enthalten."
            )

        step_excluded_paths = _path_list(
            raw.get("exclude_paths"),
            field=f"steps[{index}].exclude_paths",
        )
        if step_excluded_paths and workspace_access == "none":
            raise ValueError(
                f"steps[{index}].exclude_paths benötigen workspace_access='read' "
                "oder 'write'."
            )

        response_format_value = raw.get(
            "response_format", "json" if raw_json_operation is not None or raw_filesystem_operation is not None else "text"
        )
        if response_format_value not in {"text", "json"}:
            raise ValueError(
                f"steps[{index}].response_format muss 'text' oder 'json' sein."
            )
        response_format = str(response_format_value)

        raw_retry = raw.get("retry")
        retry_policy = global_retry_policy
        if raw_retry is not None:
            if not isinstance(raw_retry, dict):
                raise ValueError(
                    f"steps[{index}].retry muss eine Tabelle sein."
                )
            unknown_retry = set(raw_retry) - allowed_retry
            if unknown_retry:
                raise ValueError(
                    f"steps[{index}].retry enthält unbekannte Schlüssel: "
                    + ", ".join(sorted(unknown_retry))
                )
            inherited_retry = global_retry_policy or ModelRetryPolicy()
            retry_policy = ModelRetryPolicy(
                max_attempts=raw_retry.get(
                    "max_attempts",
                    inherited_retry.max_attempts,
                ),
                initial_delay_seconds=raw_retry.get(
                    "initial_delay_seconds",
                    inherited_retry.initial_delay_seconds,
                ),
                backoff_multiplier=raw_retry.get(
                    "backoff_multiplier",
                    inherited_retry.backoff_multiplier,
                ),
                max_delay_seconds=raw_retry.get(
                    "max_delay_seconds",
                    inherited_retry.max_delay_seconds,
                ),
            )

        raw_approve_tools = raw.get("approve_tools", [])
        if (
            not isinstance(raw_approve_tools, list)
            or not all(
                isinstance(value, str) and value.strip()
                for value in raw_approve_tools
            )
        ):
            raise ValueError(
                f"steps[{index}].approve_tools muss eine Liste "
                "nichtleerer Toolnamen sein."
            )
        approve_tools = tuple(value.strip() for value in raw_approve_tools)
        if len(approve_tools) != len(set(approve_tools)):
            raise ValueError(
                f"steps[{index}].approve_tools darf keine "
                "doppelten Toolnamen enthalten."
            )
        if any(
            _ITEM_EXPR.search(value)
            or _CONVERSATION_ITEM_EXPR.search(value)
            or _PREVIOUS_OUTPUT_EXPR.search(value)
            for value in approve_tools
        ):
            raise ValueError(
                f"steps[{index}].approve_tools darf nicht aus foreach-, "
                "Conversation- oder previous_output-Daten parametrisiert werden."
            )

        raw_vars = raw.get("vars", {})
        if not isinstance(raw_vars, dict):
            raise ValueError(
                f"steps[{index}].vars muss eine Tabelle sein."
            )
        variables: dict[str, str] = {}
        for name, value in raw_vars.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError(
                    f"steps[{index}].vars darf nur "
                    "String-zu-String-Werte enthalten."
                )
            variables[name] = value

        raw_json_inputs = raw.get("json_inputs", {})
        if not isinstance(raw_json_inputs, dict):
            raise ValueError(f"steps[{index}].json_inputs muss eine Tabelle sein.")
        json_inputs: dict[str, JsonInputSpec] = {}
        for name, definition in raw_json_inputs.items():
            if not isinstance(name, str) or _STEP_ID.fullmatch(name) is None:
                raise ValueError(f"Ungültiger json_inputs-Name: {name!r}.")
            if name in variables or name in global_variables:
                raise ValueError(
                    f"json_inputs-Variable {name!r} kollidiert mit vars."
                )
            if not isinstance(definition, dict) or (
                set(definition) - {"path", "iterate"}
            ):
                raise ValueError(
                    f"steps[{index}].json_inputs.{name} benötigt path und "
                    "optional iterate."
                )
            path_template = _string(
                definition.get("path"),
                field=f"steps[{index}].json_inputs.{name}.path",
            )
            iterate_value = definition.get("iterate")
            iterate = (
                _string(
                    iterate_value,
                    field=f"steps[{index}].json_inputs.{name}.iterate",
                )
                if iterate_value is not None
                else None
            )
            if iterate is not None and _JSON_INPUT_ITERATE.fullmatch(iterate) is None:
                raise ValueError(
                    f"steps[{index}].json_inputs.{name}.iterate muss eine "
                    "input-/item-Liste mit [] auswählen."
                )
            if "${" in _JSON_INPUT_TEMPLATE.sub("", path_template):
                raise ValueError(
                    f"steps[{index}].json_inputs.{name}.path enthält "
                    "nicht unterstützte Platzhalter."
                )
            if "${each" in path_template and iterate is None:
                raise ValueError(
                    f"steps[{index}].json_inputs.{name}.path verwendet "
                    "each ohne iterate."
                )
            if "${item" in path_template and raw.get("foreach") is None:
                raise ValueError(
                    f"steps[{index}].json_inputs.{name}.path verwendet "
                    "item ohne foreach."
                )
            if "${iteration.id}" in path_template and raw.get("foreach") is None:
                raise ValueError(
                    f"steps[{index}].json_inputs.{name}.path verwendet "
                    "iteration.id ohne foreach."
                )
            if iterate is not None and iterate.startswith("item.") and raw.get("foreach") is None:
                raise ValueError(
                    f"steps[{index}].json_inputs.{name}.iterate verwendet "
                    "item ohne foreach."
                )
            _validate_input_references(
                [path_template],
                declared_inputs=inputs,
                field=f"steps[{index}].json_inputs.{name}.path",
            )
            if iterate is not None and iterate.startswith("input."):
                root_name = iterate[6:].split(".", 1)[0].split("[", 1)[0]
                if root_name not in inputs:
                    raise ValueError(
                        f"steps[{index}].json_inputs.{name}.iterate "
                        f"verweist auf unbekannten Input {root_name!r}."
                    )
            json_inputs[name] = JsonInputSpec(
                path=path_template, iterate=iterate,
            )
        if (
            raw_json_operation is not None
            and raw_json_operation["input"] not in json_inputs
        ):
            raise ValueError(
                f"steps[{index}].json_operation.input muss ein json_inputs-Name sein."
            )

        input_reference_values = list(variables.values())
        if output is not None:
            input_reference_values.append(output)
        _validate_input_references(
            input_reference_values,
            declared_inputs=inputs,
            field=f"steps[{index}]",
        )
        flow_input_strings = _iter_string_values(flow_input)
        _validate_input_references(
            flow_input_strings,
            declared_inputs=inputs,
            field=f"steps[{index}].input",
        )
        if any(
            _PREVIOUS_OUTPUT_EXPR.search(value)
            or _CONVERSATION_ITEM_EXPR.search(value)
            for value in flow_input_strings
        ):
            raise ValueError(
                f"steps[{index}].input darf previous_output und "
                "conversation.item nicht verwenden."
            )

        for value in [*variables.values(), *flow_input_strings]:
            for match in _DYNAMIC_VALUE_EXPR.finditer(value):
                source_id = match.group("step_output_id")
                if source_id is None:
                    continue
                if source_id not in known_ids or source_id == step_id:
                    raise ValueError(
                        f"steps[{index}] darf nur Output eines vorherigen "
                        f"Schritts referenzieren: {source_id!r}."
                    )

        foreach_value = raw.get("foreach")
        foreach = (
            _string(
                foreach_value,
                field=f"steps[{index}].foreach",
            )
            if foreach_value is not None
            else None
        )
        conversation_items_value = raw.get("conversation_items")
        conversation_items: tuple[Any, ...] | str | None
        if conversation_items_value is None:
            conversation_items = None
        elif isinstance(conversation_items_value, list):
            if len(conversation_items_value) > MAX_FOREACH_ITEMS:
                raise ValueError(
                    f"steps[{index}].conversation_items enthält mehr als "
                    f"{MAX_FOREACH_ITEMS} Elemente."
                )
            conversation_items = tuple(conversation_items_value)
        elif isinstance(conversation_items_value, str) and conversation_items_value.strip():
            conversation_items = conversation_items_value.strip()
        else:
            raise ValueError(
                f"steps[{index}].conversation_items muss eine Liste oder "
                "ein nichtleerer String sein."
            )

        final_prompt_value = raw.get("conversation_final_prompt_file")
        conversation_final_prompt_file = (
            Path(
                _string(
                    final_prompt_value,
                    field=f"steps[{index}].conversation_final_prompt_file",
                )
            )
            if final_prompt_value is not None
            else None
        )
        if conversation_final_prompt_file is not None and conversation_items is None:
            raise ValueError(
                f"steps[{index}].conversation_final_prompt_file benötigt "
                "conversation_items."
            )
        if isinstance(conversation_items, str):
            item_match = _ITEM_EXPR.fullmatch(conversation_items)
            source_match = _FOREACH.fullmatch(conversation_items)
            if item_match is not None:
                if foreach is None:
                    raise ValueError(
                        f"steps[{index}].conversation_items verwendet "
                        "${item...} ohne foreach."
                    )
            elif source_match is not None:
                source_id = source_match.group(1)
                if source_id == step_id or source_id not in known_ids:
                    raise ValueError(
                        f"steps[{index}].conversation_items darf nur auf einen "
                        f"vorherigen Schritt verweisen: {source_id!r}."
                    )
            else:
                raise ValueError(
                    f"steps[{index}].conversation_items muss entweder eine "
                    "statische Liste, ${item...} oder "
                    "steps.<id>.output[.<pfad>] sein."
                )
        if (
            isinstance(conversation_items, tuple)
            and not conversation_items
            and conversation_final_prompt_file is None
        ):
            raise ValueError(
                f"steps[{index}].conversation_items darf ohne "
                "conversation_final_prompt_file nicht leer sein."
            )

        iteration_id_value = raw.get("iteration_id")
        iteration_id = (
            _string(
                iteration_id_value,
                field=f"steps[{index}].iteration_id",
            )
            if iteration_id_value is not None
            else None
        )
        if iteration_id is not None and foreach is None:
            raise ValueError(
                f"steps[{index}].iteration_id ist nur zusammen mit foreach erlaubt."
            )
        if iteration_id is not None and _ITERATION_ID_EXPR.search(iteration_id):
            raise ValueError(
                f"steps[{index}].iteration_id darf nicht "
                "'${iteration.id}' verwenden."
            )
        if iteration_id is not None:
            _validate_input_references(
                [iteration_id],
                declared_inputs=inputs,
                field=f"steps[{index}].iteration_id",
            )
        if foreach is not None:
            input_match = _INPUT_FOREACH.fullmatch(foreach)
            if input_match is not None:
                _validate_input_references(
                    ["${" + foreach + "}"],
                    declared_inputs=inputs,
                    field=f"steps[{index}].foreach",
                )
            else:
                match = _FOREACH.fullmatch(foreach)
                if match is None:
                    raise ValueError(
                        f"steps[{index}].foreach muss auf "
                        "steps.<id>.output[.<pfad>] oder input.<pfad> verweisen."
                    )
                source_id = match.group(1)
                if source_id not in known_ids:
                    raise ValueError(
                        f"steps[{index}].foreach darf nur auf einen "
                        f"vorherigen Schritt verweisen: {source_id!r}."
                    )
        else:
            dynamic_values = [*variables.values(), *flow_input_strings]
            if output is not None:
                dynamic_values.append(output)
            if any(
                _ITEM_EXPR.search(value) or _ITERATION_ID_EXPR.search(value)
                for value in dynamic_values
            ):
                raise ValueError(
                    f"steps[{index}] verwendet "
                    "${item...} ohne foreach."
                )

        if conversation_items is None and any(
            _CONVERSATION_ITEM_EXPR.search(value)
            for value in variables.values()
        ):
            raise ValueError(
                f"steps[{index}] verwendet ${{conversation.item...}} "
                "ohne conversation_items."
            )

        uses_previous_output = any(
            _PREVIOUS_OUTPUT_EXPR.search(value)
            for value in variables.values()
        )
        if uses_previous_output and (
            output is None
            or not overwrite_output
            or response_format != "json"
        ):
            raise ValueError(
                f"steps[{index}] darf ${{previous_output...}} nur mit "
                "response_format='json', output und overwrite_output=true verwenden."
            )

        steps.append(
            FlowStep(
                step_id=step_id,
                config=config,
                model=model,
                prompt_file=prompt_file,
                flow=child_flow,
                flow_input=flow_input,
                context_files=context_files,
                add_web_context=add_web_context,
                output=output,
                overwrite_output=overwrite_output,
                workspace_access=workspace_access,
                with_data=with_data,
                with_mcp=with_mcp,
                retry_policy=retry_policy,
                retry_overrides=dict(raw_retry) if raw_retry is not None else None,
                response_format=response_format,
                approve_tools=approve_tools,
                variables=variables,
                global_variables=global_variables,
                foreach=foreach,
                json_inputs=json_inputs,
                json_operation=dict(raw_json_operation) if raw_json_operation else None,
                filesystem_operation=dict(raw_filesystem_operation) if raw_filesystem_operation else None,
                conversation_items=conversation_items,
                conversation_final_prompt_file=conversation_final_prompt_file,
                iteration_id=iteration_id,
                excluded_paths=step_excluded_paths,
            )
        )

    if result_selector is not None:
        match = _FOREACH.fullmatch(result_selector)
        if match is None:
            raise ValueError(
                "result muss auf steps.<id>.output[.<pfad>] verweisen."
            )
        source_id = match.group(1)
        if source_id not in known_ids:
            raise ValueError(
                f"result verweist auf unbekannten Schritt {source_id!r}."
            )

    return FlowDefinition(
        source=source,
        steps=tuple(steps),
        excluded_paths=excluded_paths,
        inputs=inputs,
        result=result_selector,
        retry_policy=global_retry_policy,
    )


class _JsonNumber(str):
    """Lossless representation of a validated JSON non-integer number."""


def _json_dump_string(value: str) -> str:
    dumped = json.dumps(value, ensure_ascii=False)
    return "".join(
        f"\\u{ord(char):04x}"
        if 0xD800 <= ord(char) <= 0xDFFF
        else char
        for char in dumped
    )


def _json_dumps_preserving_numbers(value: Any) -> str:
    if isinstance(value, _JsonNumber):
        return str(value)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _json_dump_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except ValueError as exc:
            raise TypeError(
                "Nicht unterstützter JSON-Wert: nicht-endlicher float"
            ) from exc
    if isinstance(value, list):
        return "[" + ",".join(
            _json_dumps_preserving_numbers(item)
            for item in value
        ) + "]"
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON-Objektschlüssel müssen Strings sein.")
            parts.append(
                _json_dump_string(key)
                + ":"
                + _json_dumps_preserving_numbers(item)
            )
        return "{" + ",".join(parts) + "}"
    raise TypeError(
        f"Nicht unterstützter JSON-Wert: {type(value).__name__}"
    )


def _lookup(
    value: Any,
    path: str | None,
    *,
    label: str,
) -> Any:
    current = value
    if path:
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                raise ValueError(
                    f"{label} enthält kein Feld {part!r}."
                )
            current = current[part]
    return current


def _render_item_text(
    template: str,
    item: Any,
) -> str:
    def replace(match: re.Match[str]) -> str:
        path = match.group(1)
        value = (
            _lookup(
                item,
                path,
                label="foreach-Element",
            )
            if path
            else item
        )
        if isinstance(value, (dict, list)):
            return _json_dumps_preserving_numbers(value)
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return _ITEM_EXPR.sub(replace, template)


def _render_iteration_text(
    template: str,
    *,
    item: Any,
    iteration_id: str | None,
) -> str:
    rendered = template
    if iteration_id is not None:
        rendered = _ITERATION_ID_EXPR.sub(iteration_id, rendered)
    return _render_item_text(rendered, item)


def _render_input_text(
    template: str,
    flow_input: dict[str, Any],
) -> str:
    def replace(match: re.Match[str]) -> str:
        value = _lookup(
            flow_input,
            match.group(1),
            label="Flow-Input",
        )
        if isinstance(value, (dict, list)):
            return _json_dumps_preserving_numbers(value)
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return _INPUT_EXPR.sub(replace, template)


def _dynamic_value(
    match: re.Match[str],
    *,
    item: Any,
    iteration_id: str | None,
    conversation_item: Any,
    previous_output: Any,
    outputs: dict[str, str],
    flow_input: dict[str, Any],
) -> Any:
    if match.group("iteration") is not None:
        if iteration_id is None:
            raise ValueError("${iteration.id} ist ohne foreach nicht verfügbar.")
        return iteration_id
    if match.group("item_path") is not None or match.group(0).startswith("${item"):
        return _lookup(
            item,
            match.group("item_path"),
            label="foreach-Element",
        )
    if match.group(0).startswith("${previous_output"):
        return _lookup(
            previous_output,
            match.group("previous_output_path"),
            label="previous_output",
        )
    if match.group("input_path") is not None or match.group(0).startswith("${input"):
        return _lookup(
            flow_input,
            match.group("input_path"),
            label="Flow-Input",
        )
    if match.group("step_output_id") is not None:
        source_id = match.group("step_output_id")
        if source_id not in outputs:
            raise ValueError(
                f"Output von Schritt {source_id!r} ist noch nicht verfügbar."
            )
        path = match.group("step_output_path")
        if path is None:
            return outputs[source_id]
        parsed = _parse_structured_output(
            outputs[source_id],
            step_id=source_id,
        )
        return _lookup(
            parsed,
            path,
            label=f"Output von Schritt {source_id!r}",
        )
    return _lookup(
        conversation_item,
        match.group("conversation_path"),
        label="Conversation-Element",
    )


def _render_dynamic_text(
    template: str,
    *,
    item: Any,
    iteration_id: str | None,
    conversation_item: Any,
    previous_output: Any,
    outputs: dict[str, str],
    flow_input: dict[str, Any],
) -> str:
    def replace(match: re.Match[str]) -> str:
        value = _dynamic_value(
            match,
            item=item,
            iteration_id=iteration_id,
            conversation_item=conversation_item,
            previous_output=previous_output,
            outputs=outputs,
            flow_input=flow_input,
        )
        is_previous_output = match.group(0).startswith("${previous_output")
        if (
            is_previous_output
            and match.group("previous_output_path") is None
        ):
            return _json_dumps_preserving_numbers(value)
        if isinstance(value, (dict, list)):
            return _json_dumps_preserving_numbers(value)
        if value is None:
            return "null" if is_previous_output else ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return _DYNAMIC_VALUE_EXPR.sub(replace, template)


def _render_flow_input_value(
    value: Any,
    *,
    item: Any,
    iteration_id: str | None,
    outputs: dict[str, str],
    output_json: dict[str, bool],
    flow_input: dict[str, Any],
) -> Any:
    if isinstance(value, str):
        full = _DYNAMIC_VALUE_EXPR.fullmatch(value)
        if full is not None:
            rendered_value = _dynamic_value(
                full,
                item=item,
                iteration_id=iteration_id,
                conversation_item=None,
                previous_output=None,
                outputs=outputs,
                flow_input=flow_input,
            )
            source_id = full.group("step_output_id")
            if (
                source_id is not None
                and full.group("step_output_path") is None
                and output_json.get(source_id, False)
            ):
                return _parse_structured_output(
                    outputs[source_id],
                    step_id=source_id,
                )
            return rendered_value
        return _render_dynamic_text(
            value,
            item=item,
            iteration_id=iteration_id,
            conversation_item=None,
            previous_output=None,
            outputs=outputs,
            flow_input=flow_input,
        )
    if isinstance(value, list):
        return [
            _render_flow_input_value(
                item_value,
                item=item,
                iteration_id=iteration_id,
                outputs=outputs,
                output_json=output_json,
                flow_input=flow_input,
            )
            for item_value in value
        ]
    if isinstance(value, dict):
        return {
            key: _render_flow_input_value(
                item_value,
                item=item,
                iteration_id=iteration_id,
                outputs=outputs,
                output_json=output_json,
                flow_input=flow_input,
            )
            for key, item_value in value.items()
        }
    return value


def _conversation_items_for_iteration(
    step: FlowStep,
    *,
    outputs: dict[str, str],
    item: Any,
) -> list[Any] | None:
    source = step.conversation_items
    if source is None:
        return None
    if isinstance(source, tuple):
        values = list(source)
    else:
        item_match = _ITEM_EXPR.fullmatch(source)
        if item_match is not None:
            values = _lookup(
                item,
                item_match.group(1),
                label="foreach-Element",
            )
        else:
            match = _FOREACH.fullmatch(source)
            assert match is not None
            source_id, path = match.groups()
            if source_id not in outputs:
                raise ValueError(
                    f"conversation_items-Quelle {source_id!r} besitzt keinen "
                    "verfügbaren Output."
                )
            parsed = _parse_structured_output(
                outputs[source_id],
                step_id=source_id,
            )
            values = _lookup(
                parsed,
                path,
                label=f"Output von Schritt {source_id!r}",
            )
    if not isinstance(values, list):
        raise ValueError(
            f"conversation_items von Schritt {step.step_id!r} ist keine Liste."
        )
    if len(values) > MAX_FOREACH_ITEMS:
        raise ValueError(
            f"conversation_items enthält {len(values)} Elemente; "
            f"erlaubt sind höchstens {MAX_FOREACH_ITEMS}."
        )
    if not values and step.conversation_final_prompt_file is None:
        raise ValueError(
            f"conversation_items von Schritt {step.step_id!r} ist leer und "
            "es gibt keinen conversation_final_prompt_file."
        )
    return values


def _iteration_ids(
    step: FlowStep,
    *,
    items: list[Any],
    flow_input: dict[str, Any] | None = None,
) -> list[str | None]:
    if step.foreach is None:
        return [None]
    if step.iteration_id is None:
        return [str(index) for index in range(1, len(items) + 1)]

    bases: list[str] = []
    rendered_template = _render_input_text(
        step.iteration_id,
        flow_input or {},
    )
    for item in items:
        base = _render_item_text(rendered_template, item).strip()
        if not base or _ITERATION_ID.fullmatch(base) is None:
            raise ValueError(
                f"Schritt {step.step_id!r} erzeugt eine ungültige iteration_id "
                f"{base!r}; erlaubt sind ASCII-Buchstaben, Ziffern, '_' und '-'."
            )
        bases.append(base)

    reserved_bases = {base.casefold() for base in bases}
    used: set[str] = set()
    result: list[str] = []
    for base in bases:
        candidate = base
        if candidate.casefold() in used:
            suffix = 2
            candidate = f"{base}-{suffix}"
            while (
                candidate.casefold() in used
                or candidate.casefold() in reserved_bases
            ):
                suffix += 1
                candidate = f"{base}-{suffix}"
        used.add(candidate.casefold())
        result.append(candidate)
    return result


def _parse_structured_output(
    text: str,
    *,
    step_id: str,
) -> Any:
    raw = text.encode("utf-8")
    if len(raw) > MAX_STRUCTURED_OUTPUT_BYTES:
        raise ValueError(
            f"Output von Schritt {step_id!r} überschreitet das "
            f"JSON-Limit von {MAX_STRUCTURED_OUTPUT_BYTES} Bytes."
        )
    def reject_constant(value: str) -> None:
        raise ValueError(
            f"nicht standardkonstante JSON-Zahl {value!r}"
        )

    try:
        return json.loads(
            text,
            parse_float=_JsonNumber,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(
            f"Output von Schritt {step_id!r} muss gültiges JSON sein: {exc}"
        ) from exc


def _output_owner(
    step: FlowStep,
    *,
    iteration_id: str | None,
) -> str:
    if step.foreach is None:
        return f"Schritt {step.step_id!r}"
    return f"Schritt {step.step_id!r}, Iteration {iteration_id!r}"


def _claim_output(
    claimed_outputs: dict[str, str],
    *,
    path: Path,
    owner: str,
    allow_replace: bool = False,
) -> None:
    key = _filesystem_path_key(path)
    previous_owner = claimed_outputs.get(key)
    if previous_owner is not None and not allow_replace:
        raise ValueError(
            f"Output-Datei {path} wird in diesem Flow-Lauf bereits von "
            f"{previous_owner} beansprucht; {owner} darf denselben Output "
            "nicht erneut verwenden."
        )
    claimed_outputs[key] = owner


def _checkpoint_fingerprint(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def _checkpoint_snapshot_key(
    step: FlowStep,
    output: Path,
) -> tuple[str, str]:
    return step.step_id, _filesystem_path_key(output)


def _verified_preflight_checkpoint(
    step: FlowStep,
    *,
    workspace: Path,
    output: Path | None,
    expected_fingerprint: bytes,
) -> str:
    checkpoint = _existing_json_checkpoint(
        step,
        workspace=workspace,
        output=output,
    )
    if checkpoint is None:
        raise ValueError(
            f"JSON-Checkpoint von Schritt {step.step_id!r} wurde nach dem "
            f"Preflight entfernt: {output}"
        )
    if _checkpoint_fingerprint(checkpoint) != expected_fingerprint:
        raise ValueError(
            f"JSON-Checkpoint von Schritt {step.step_id!r} wurde nach dem "
            f"Preflight verändert: {output}"
        )
    return checkpoint


def _existing_json_output(
    step: FlowStep,
    *,
    workspace: Path,
    output: Path | None,
) -> str | None:
    if (
        output is None
        or step.response_format != "json"
        or not output.exists()
    ):
        return None

    text = read_existing_output_text(
        workspace,
        output,
        max_bytes=MAX_STRUCTURED_OUTPUT_BYTES,
    )
    _parse_structured_output(text, step_id=step.step_id)
    return text


def _existing_json_checkpoint(
    step: FlowStep,
    *,
    workspace: Path,
    output: Path | None,
) -> str | None:
    if step.overwrite_output:
        return None
    return _existing_json_output(
        step,
        workspace=workspace,
        output=output,
    )


def _previous_output_from_snapshot(
    step: FlowStep,
    *,
    workspace: Path,
    output: Path | None,
    fingerprints: dict[tuple[str, str], bytes | None],
) -> Any:
    if not _uses_previous_output(step):
        return None
    assert output is not None
    key = _checkpoint_snapshot_key(step, output)
    if key not in fingerprints:
        raise ValueError(
            f"previous_output von Schritt {step.step_id!r} konnte beim "
            "Run-Start nicht eindeutig bestimmt werden."
        )
    expected_fingerprint = fingerprints[key]
    if expected_fingerprint is None:
        return None

    text = _existing_json_output(
        step,
        workspace=workspace,
        output=output,
    )
    if text is None:
        raise ValueError(
            f"previous_output von Schritt {step.step_id!r} wurde nach dem "
            f"Run-Start entfernt: {output}"
        )
    if _checkpoint_fingerprint(text) != expected_fingerprint:
        raise ValueError(
            f"previous_output von Schritt {step.step_id!r} wurde nach dem "
            f"Run-Start verändert: {output}"
        )
    return _parse_structured_output(
        text,
        step_id=step.step_id,
    )


def _prepare_flow_output(
    step: FlowStep,
    *,
    workspace: Path,
    output: Path,
) -> str | None:
    checkpoint = _existing_json_checkpoint(
        step,
        workspace=workspace,
        output=output,
    )
    if checkpoint is not None:
        return checkpoint

    prepare_output_target(
        workspace,
        output,
        overwrite=step.overwrite_output,
        allow_missing_parent=True,
    )
    return None


@dataclass(frozen=True)
class _RunStartSnapshot:
    checkpoint_fingerprints: dict[tuple[str, str], bytes]
    checkpoint_paths: tuple[Path, ...]
    previous_output_fingerprints: dict[tuple[str, str], bytes | None]
    available_outputs: dict[str, str]
    output_json: dict[str, bool]


def _uses_previous_output(step: FlowStep) -> bool:
    return any(
        _PREVIOUS_OUTPUT_EXPR.search(value)
        for value in step.variables.values()
    )


def _subflow_inputs_resolvable_at_run_start(
    step: FlowStep,
    *,
    available_outputs: dict[str, str],
) -> bool:
    for value in _iter_string_values(step.flow_input):
        for match in _DYNAMIC_VALUE_EXPR.finditer(value):
            source_id = match.group("step_output_id")
            if source_id is not None and source_id not in available_outputs:
                return False
    return True


def _child_invocation_namespace(
    parent_namespace: str,
    step: FlowStep,
    *,
    iteration_id: str | None,
    case_colliding_step_ids: frozenset[str],
) -> str:
    suffix = str(iteration_id) if iteration_id is not None else "once"
    step_segment = step.step_id
    if step.step_id in case_colliding_step_ids:
        digest = hashlib.sha256(
            step.step_id.encode("utf-8")
        ).hexdigest()[:16]
        step_segment = f"{step_segment}.case-{digest}"
    return f"{parent_namespace}{step_segment}.{suffix}."

def _capture_run_start_invocation(
    flow: FlowDefinition,
    *,
    workspace: Path,
    cache: dict[str, FlowDefinition],
    flow_input: dict[str, Any],
    namespace: str,
    snapshots: dict[str, _RunStartSnapshot],
    protected_paths: dict[str, Path],
    budget: _SubflowTraversalBudget,
    call_stack: tuple[str, ...] = (),
) -> _FlowResultValue | None:
    _set_flow_diagnostic(
        phase="run-start-snapshot", flow=flow.source,
        namespace=namespace,
    )
    budget.consume(purpose="Run-Start-Planung")
    flow_key = _filesystem_path_key(flow.source)
    if flow_key in call_stack:
        raise ValueError("Zyklischer Subflow-Aufruf erkannt.")
    if len(call_stack) >= MAX_SUBFLOW_DEPTH:
        raise ValueError(
            f"Maximale Subflow-Tiefe von {MAX_SUBFLOW_DEPTH} überschritten."
        )

    next_stack = (*call_stack, flow_key)
    fingerprints: dict[tuple[str, str], bytes] = {}
    checkpoint_paths: dict[str, Path] = {}
    previous_output_fingerprints: dict[
        tuple[str, str],
        bytes | None,
    ] = {}
    available_outputs: dict[str, str] = {}
    output_json: dict[str, bool] = {}
    case_colliding_step_ids = _case_colliding_step_ids(
        flow,
        workspace=workspace,
    )

    def remember_checkpoint(
        step: FlowStep,
        output: Path,
        checkpoint: str,
    ) -> None:
        fingerprints[_checkpoint_snapshot_key(step, output)] = (
            _checkpoint_fingerprint(checkpoint)
        )
        key = _filesystem_path_key(output)
        checkpoint_paths[key] = output
        protected_paths[key] = output

    def remember_previous_output(
        step: FlowStep,
        output: Path,
    ) -> None:
        key = _checkpoint_snapshot_key(step, output)
        text = _existing_json_output(
            step,
            workspace=workspace,
            output=output,
        )
        if text is None:
            previous_output_fingerprints[key] = None
            return
        previous_output_fingerprints[key] = _checkpoint_fingerprint(text)
        path_key = _filesystem_path_key(output)
        checkpoint_paths[path_key] = output
        protected_paths[path_key] = output

    for step in flow.steps:
        _set_flow_diagnostic(
            phase="run-start-step", flow=flow.source,
            namespace=namespace, step=step.step_id,
        )
        if step.flow is not None:
            if step.foreach is None:
                items = [None]
                iteration_ids: list[str | None] = [None]
            else:
                source_id = _foreach_output_source(step.foreach)
                if source_id is not None and source_id not in available_outputs:
                    continue
                items = _foreach_items(
                    step,
                    outputs=available_outputs,
                    flow_input=flow_input,
                )
                iteration_ids = _iteration_ids(
                    step,
                    items=items,
                    flow_input=flow_input,
                )

            if not _subflow_inputs_resolvable_at_run_start(
                step,
                available_outputs=available_outputs,
            ):
                continue

            child = _load_subflow_definition(
                step,
                workspace=workspace,
                cache=cache,
            )
            _validate_subflow_interface(step, child)

            child_results: list[tuple[str, _FlowResultValue]] = []
            complete = True
            for item, iteration_id in zip(items, iteration_ids):
                _set_flow_diagnostic(
                    phase="run-start-subflow-inputs", flow=flow.source,
                    namespace=namespace, step=step.step_id,
                    iteration_id=iteration_id,
                )
                child_input = _render_subflow_inputs(
                    step,
                    item=item,
                    iteration_id=iteration_id,
                    outputs=available_outputs,
                    output_json=output_json,
                    flow_input=flow_input,
                )
                child_namespace = _child_invocation_namespace(
                    namespace,
                    step,
                    iteration_id=iteration_id,
                    case_colliding_step_ids=case_colliding_step_ids,
                )
                child_result = _capture_run_start_invocation(
                    child,
                    workspace=workspace,
                    cache=cache,
                    flow_input=child_input,
                    namespace=child_namespace,
                    snapshots=snapshots,
                    protected_paths=protected_paths,
                    budget=budget,
                    call_stack=next_stack,
                )
                _set_flow_diagnostic(
                    phase="run-start-subflow-result", flow=flow.source,
                    namespace=namespace, step=step.step_id,
                    iteration_id=iteration_id,
                )
                if child_result is None:
                    complete = False
                    continue
                child_results.append(
                    (
                        str(iteration_id) if iteration_id is not None else "once",
                        child_result,
                    )
                )

            if not complete:
                continue
            if step.foreach is None:
                if child_results:
                    result = child_results[0][1]
                    available_outputs[step.step_id] = result.text
                    output_json[step.step_id] = result.is_json
                continue

            parts: list[str] = []
            payload_bytes = 0
            for iteration_id, result in child_results:
                _set_flow_diagnostic(
                    phase="run-start-aggregate-subflow", flow=flow.source,
                    namespace=namespace, step=step.step_id,
                    iteration_id=iteration_id,
                )
                payload_bytes = _append_foreach_iteration(
                    step,
                    parts=parts,
                    payload_bytes=payload_bytes,
                    iteration_id=iteration_id,
                    answer=result.text,
                    answer_is_json=result.is_json,
                )
            available_outputs[step.step_id] = _finish_foreach_output(parts)
            output_json[step.step_id] = True
            continue

        if step.foreach is None:
            if step.output is None:
                continue
            output = _output_for_iteration(
                step,
                workspace=workspace,
                item=None,
                flow_input=flow_input,
            )
            assert output is not None
            _set_flow_diagnostic(
                phase="run-start-checkpoint", flow=flow.source,
                namespace=namespace, step=step.step_id, output=output,
            )

            if _uses_previous_output(step):
                remember_previous_output(step, output)

            checkpoint = _existing_json_checkpoint(
                step,
                workspace=workspace,
                output=output,
            )
            if checkpoint is not None:
                remember_checkpoint(step, output, checkpoint)
                available_outputs[step.step_id] = checkpoint
                output_json[step.step_id] = True
            continue

        source_id = _foreach_output_source(step.foreach)
        if source_id is not None and source_id not in available_outputs:
            if _uses_previous_output(step):
                raise ValueError(
                    f"previous_output von foreach-Schritt {step.step_id!r} "
                    "kann beim Run-Start nicht bestimmt werden, weil seine "
                    f"Quelle {source_id!r} nicht vollständig aus "
                    "Run-Start-Checkpoints rekonstruierbar ist."
                )
            continue

        items = _foreach_items(
            step,
            outputs=available_outputs,
            flow_input=flow_input,
        )
        iteration_ids = _iteration_ids(
            step,
            items=items,
            flow_input=flow_input,
        )
        outputs: list[Path | None] = []
        for item, iteration_id in zip(items, iteration_ids):
            _set_flow_diagnostic(
                phase="run-start-resolve-output", flow=flow.source,
                namespace=namespace, step=step.step_id,
                iteration_id=iteration_id,
            )
            output = _output_for_iteration(
                step,
                workspace=workspace,
                item=item,
                iteration_id=iteration_id,
                flow_input=flow_input,
            )
            outputs.append(output)

        seen_output_keys: set[str] = set()
        for iteration_id, output in zip(iteration_ids, outputs):
            _set_flow_diagnostic(
                phase="run-start-check-output-path", flow=flow.source,
                namespace=namespace, step=step.step_id,
                iteration_id=iteration_id, output=output,
            )
            if output is None:
                continue
            key = _filesystem_path_key(output)
            if key in seen_output_keys:
                raise ValueError(
                    f"Schritt {step.step_id!r} erzeugt für mehrere "
                    "foreach-Elemente nicht eindeutige Output-Pfade."
                )
            seen_output_keys.add(key)

        if _uses_previous_output(step):
            for iteration_id, output in zip(iteration_ids, outputs):
                assert output is not None
                _set_flow_diagnostic(
                    phase="run-start-previous-output", flow=flow.source,
                    namespace=namespace, step=step.step_id,
                    iteration_id=iteration_id, output=output,
                )
                remember_previous_output(step, output)

        if not items:
            available_outputs[step.step_id] = _finish_foreach_output([])
            output_json[step.step_id] = True
            continue

        if (
            step.output is None
            or step.overwrite_output
            or step.response_format != "json"
        ):
            continue

        parts: list[str] = []
        payload_bytes = 0
        complete = True
        for iteration_id, output in zip(iteration_ids, outputs):
            assert iteration_id is not None
            assert output is not None
            _set_flow_diagnostic(
                phase="run-start-checkpoint", flow=flow.source,
                namespace=namespace, step=step.step_id,
                iteration_id=iteration_id, output=output,
            )
            checkpoint = _existing_json_checkpoint(
                step,
                workspace=workspace,
                output=output,
            )
            if checkpoint is None:
                complete = False
                continue
            remember_checkpoint(step, output, checkpoint)
            if complete:
                payload_bytes = _append_foreach_iteration(
                    step,
                    parts=parts,
                    payload_bytes=payload_bytes,
                    iteration_id=iteration_id,
                    answer=checkpoint,
                )

        if complete:
            available_outputs[step.step_id] = _finish_foreach_output(parts)
            output_json[step.step_id] = True

    _set_flow_diagnostic(
        phase="run-start-save-snapshot", flow=flow.source,
        namespace=namespace,
    )
    snapshot = _RunStartSnapshot(
        checkpoint_fingerprints=fingerprints,
        checkpoint_paths=tuple(checkpoint_paths.values()),
        previous_output_fingerprints=previous_output_fingerprints,
        available_outputs=dict(available_outputs),
        output_json=dict(output_json),
    )
    snapshots[namespace] = snapshot

    if flow.result is None:
        return None
    match = _FOREACH.fullmatch(flow.result)
    assert match is not None
    source_id = match.group(1)
    if source_id not in available_outputs:
        return None
    return _flow_result_value(
        flow,
        available_outputs,
        output_json,
    )



def _snapshot_dynamic_checkpoint_candidates(
    *,
    workspace: Path,
    cache: dict[str, FlowDefinition],
    known_checkpoint_fingerprints: dict[str, bytes] | None = None,
) -> tuple[dict[tuple[str, str, str], bytes], tuple[Path, ...]]:
    """Snapshot startup JSON outputs, scoped to their declaring flow/step.

    File fingerprints are only available to the exact dynamic output template
    that declared them. A different (including static) step must never adopt
    those files merely because it resolves to the same path.
    """
    fingerprints: dict[tuple[str, str, str], bytes] = {}
    paths: dict[str, Path] = {}
    known_fingerprints = known_checkpoint_fingerprints or {}
    # Combine identical search templates while retaining all declaring owners.
    owners_by_template: dict[str, list[tuple[str, str]]] = {}
    for flow in cache.values():
        for step in flow.steps:
            if (
                step.flow is not None
                or step.output is None
                or step.response_format != "json"
                or step.overwrite_output
                or not _DYNAMIC_VALUE_EXPR.search(step.output)
            ):
                continue
            template_path = Path(step.output)
            if template_path.is_absolute():
                try:
                    template_path = template_path.relative_to(workspace)
                except ValueError:
                    continue
            template = template_path.as_posix()
            owners_by_template.setdefault(template, []).append(
                (_filesystem_path_key(flow.source), step.step_id)
            )

    # A busy flow can have many dynamic templates under the same state
    # directory. Walking that directory once per template repeatedly counts
    # unrelated files and exhausts the candidate limit even for a modest
    # number of real checkpoint files. Group templates by their search root.
    patterns_by_root: dict[
        str, tuple[Path, list[tuple[re.Pattern[str], list[tuple[str, str]]]]]
    ] = {}
    case_insensitive = _filesystem_is_case_insensitive(workspace)
    for template, declaring_steps in owners_by_template.items():
        # Dynamic values may contain separators. Keep the same conservative
        # static-prefix root and full-template matching rules as before.
        matches = list(_DYNAMIC_VALUE_EXPR.finditer(template))
        first_prefix = template[:matches[0].start()]
        if first_prefix.endswith("/"):
            root = first_prefix.rstrip("/") or "."
        else:
            root = first_prefix.rpartition("/")[0] or "."
        search_root = _workspace_path(
            workspace,
            root,
            purpose="Verzeichnis für dynamische JSON-Checkpoints",
            must_exist=False,
        )
        if not search_root.is_dir():
            continue

        regex_parts: list[str] = []
        cursor = 0
        for match in matches:
            regex_parts.append(re.escape(template[cursor:match.start()]))
            regex_parts.append("(?s:.*?)")
            cursor = match.end()
        regex_parts.append(re.escape(template[cursor:]))
        matches_template = re.compile(
            r"\A" + "".join(regex_parts) + r"\Z",
            re.IGNORECASE if case_insensitive else 0,
        )
        root_key = _filesystem_path_key(search_root)
        if root_key not in patterns_by_root:
            patterns_by_root[root_key] = (search_root, [])
        patterns_by_root[root_key][1].append(
            (matches_template, declaring_steps)
        )

    # Also deduplicate entries when different template roots overlap (e.g.
    # state/ and state/concepts/). Preserve the global resource limit for
    # DISTINCT examined filesystem entries, including unrelated entries.
    examined_paths: set[str] = set()
    total_bytes = 0
    content_fingerprints: dict[str, bytes] = dict(known_fingerprints)
    for search_root, patterns in patterns_by_root.values():
        for directory, subdirectories, filenames in os.walk(
            search_root, followlinks=False
        ):
            folder = Path(directory)
            for entry in (
                folder,
                *(folder / dirname for dirname in subdirectories),
                *(folder / filename for filename in filenames),
            ):
                # Count directory ENTRIES, not resolved targets: many
                # symlinks to a single file must still consume the budget.
                # abspath/normpath are lexical here (unlike Path.resolve).
                key = os.path.normpath(os.path.abspath(entry))
                if case_insensitive:
                    key = key.casefold()
                if key not in examined_paths:
                    examined_paths.add(key)
                    if len(examined_paths) > MAX_DYNAMIC_CHECKPOINT_CANDIDATES:
                        raise ValueError(
                            "Zu viele eindeutige dynamische JSON-Checkpoint-"
                            "Kandidaten beim Run-Start."
                        )

            # Never follow symlinks or Windows reparse points.
            safe_subdirectories = []
            for dirname in subdirectories:
                try:
                    if not path_entry_is_symlink_or_reparse(folder / dirname):
                        safe_subdirectories.append(dirname)
                except OSError:
                    continue
            subdirectories[:] = safe_subdirectories

            for filename in filenames:
                candidate = folder / filename
                relative = candidate.relative_to(workspace).as_posix()
                declaring_steps: list[tuple[str, str]] = []
                for matcher, owners in patterns:
                    if matcher.fullmatch(relative):
                        declaring_steps.extend(owners)
                if not declaring_steps:
                    continue

                try:
                    path = _workspace_path(
                        workspace,
                        candidate,
                        purpose="Dynamischer JSON-Checkpoint",
                        must_exist=True,
                    )
                except ValueError:
                    continue
                key = _filesystem_path_key(path)
                # Keep template/declaring-step ownership strict. An unrelated
                # step (including a static-output step) must not adopt a
                # checkpoint merely because its path happens to coincide.
                fingerprint = content_fingerprints.get(key)
                if fingerprint is None:
                    try:
                        size = path.stat().st_size
                    except OSError:
                        continue
                    if size > MAX_STRUCTURED_OUTPUT_BYTES:
                        continue
                    total_bytes += size
                    if total_bytes > MAX_DYNAMIC_CHECKPOINT_TOTAL_BYTES:
                        raise ValueError("Run-Start-Checkpoint-Budget überschritten.")
                    try:
                        content = read_existing_output_text(
                            workspace,
                            path,
                            max_bytes=MAX_STRUCTURED_OUTPUT_BYTES,
                        )
                    except (OSError, ValueError):
                        continue
                    fingerprint = _checkpoint_fingerprint(content)
                    content_fingerprints[key] = fingerprint
                    paths[key] = path
                for flow_key, step_id in dict.fromkeys(declaring_steps):
                    fingerprints[(flow_key, step_id, key)] = fingerprint
    return fingerprints, tuple(paths.values())


def _run_start_fingerprint(
    step: FlowStep,
    output: Path,
    local: dict[tuple[str, str], bytes],
    dynamic: dict[tuple[str, str, str], bytes] | None,
    *,
    flow_source: Path,
) -> bytes | None:
    known = local.get(_checkpoint_snapshot_key(step, output))
    if known is not None:
        return known
    if (
        step.response_format != "json"
        or step.overwrite_output
        or step.output is None
        or not _DYNAMIC_VALUE_EXPR.search(step.output)
    ):
        return None
    return (dynamic or {}).get(
        (_filesystem_path_key(flow_source), step.step_id, _filesystem_path_key(output))
    )


def _capture_run_start_snapshots(
    *,
    workspace: Path,
    root_flow: FlowDefinition,
    cache: dict[str, FlowDefinition],
) -> tuple[
    dict[str, _RunStartSnapshot],
    tuple[Path, ...],
]:
    snapshots: dict[str, _RunStartSnapshot] = {}
    protected_paths: dict[str, Path] = {}
    _capture_run_start_invocation(
        root_flow,
        workspace=workspace,
        cache=cache,
        flow_input={},
        namespace="",
        snapshots=snapshots,
        protected_paths=protected_paths,
        budget=_SubflowTraversalBudget(),
    )
    return snapshots, tuple(protected_paths.values())


_FOREACH_AGGREGATE_PREFIX = '{"iterations":['
_FOREACH_AGGREGATE_SUFFIX = "]}"


def _serialize_foreach_iteration(
    step: FlowStep,
    *,
    iteration_id: str,
    answer: str,
    answer_is_json: bool | None = None,
) -> str:
    value: Any = answer
    should_parse_json = (
        answer_is_json
        if answer_is_json is not None
        else step.response_format == "json"
    )
    if should_parse_json:
        value = _parse_structured_output(
            answer,
            step_id=step.step_id,
        )
    return _json_dumps_preserving_numbers(
        {
            "id": iteration_id,
            "output": value,
        }
    )


def _append_foreach_iteration(
    step: FlowStep,
    *,
    parts: list[str],
    payload_bytes: int,
    iteration_id: str,
    answer: str,
    answer_is_json: bool | None = None,
) -> int:
    fragment = _serialize_foreach_iteration(
        step,
        iteration_id=iteration_id,
        answer=answer,
        answer_is_json=answer_is_json,
    )
    fragment_bytes = len(fragment.encode("utf-8"))
    separator_bytes = 1 if parts else 0
    next_payload_bytes = (
        payload_bytes
        + separator_bytes
        + fragment_bytes
    )
    total_bytes = (
        len(_FOREACH_AGGREGATE_PREFIX.encode("utf-8"))
        + next_payload_bytes
        + len(_FOREACH_AGGREGATE_SUFFIX.encode("utf-8"))
    )
    if total_bytes > MAX_STRUCTURED_OUTPUT_BYTES:
        raise ValueError(
            f"Aggregierter Output von Schritt {step.step_id!r} überschreitet "
            f"das JSON-Limit von {MAX_STRUCTURED_OUTPUT_BYTES} Bytes."
        )
    parts.append(fragment)
    return next_payload_bytes


def _finish_foreach_output(parts: list[str]) -> str:
    return (
        _FOREACH_AGGREGATE_PREFIX
        + ",".join(parts)
        + _FOREACH_AGGREGATE_SUFFIX
    )


def _foreach_items(
    step: FlowStep,
    *,
    outputs: dict[str, Any],
    flow_input: dict[str, Any],
) -> list[Any]:
    if step.foreach is None:
        return [None]

    input_match = _INPUT_FOREACH.fullmatch(step.foreach)
    if input_match is not None:
        items = _lookup(
            flow_input,
            input_match.group(1),
            label="Flow-Input",
        )
    else:
        match = _FOREACH.fullmatch(step.foreach)
        assert match is not None
        source_id, path = match.groups()
        if source_id not in outputs:
            raise ValueError(
                f"foreach-Quelle {source_id!r} besitzt keinen "
                "verfügbaren Output."
            )
        parsed = _parse_structured_output(
            outputs[source_id],
            step_id=source_id,
        )
        items = _lookup(
            parsed,
            path,
            label=f"Output von Schritt {source_id!r}",
        )
    if not isinstance(items, list):
        raise ValueError(
            f"foreach-Quelle {step.foreach!r} ist keine Liste."
        )
    if len(items) > MAX_FOREACH_ITEMS:
        raise ValueError(
            f"foreach-Quelle enthält {len(items)} Elemente; "
            f"erlaubt sind höchstens {MAX_FOREACH_ITEMS}."
        )
    return items


def _resolve_config(
    step: FlowStep,
    *,
    flow_dir: Path,
) -> Path:
    if step.config is None:
        return default_config_file()
    path = step.config.expanduser()
    if path.is_absolute():
        return path
    return flow_dir / path


def _json_input_items(
    selector: str,
    *,
    flow_input: dict[str, Any],
    item: Any,
) -> list[Any]:
    """Expand a bounded, explicit input.scans[].output.chunks[] selector."""
    parts = selector.split("[]")
    head = parts[0]
    root, _, field = head.partition(".")
    source = flow_input if root == "input" else item
    values = [_lookup(source, field, label="json_inputs.iterate")]
    for index, segment in enumerate(parts[:-1]):
        if index > 0:
            nested = segment.lstrip(".")
            values = [
                _lookup(value, nested, label="json_inputs.iterate")
                if nested else value
                for value in values
            ]
        next_values: list[Any] = []
        for value in values:
            if not isinstance(value, list):
                raise ValueError(
                    f"json_inputs.iterate {selector!r} erwartet Listen."
                )
            if len(next_values) + len(value) > MAX_JSON_INPUT_FILES:
                raise ValueError(
                    f"json_inputs.iterate überschreitet {MAX_JSON_INPUT_FILES} Dateien."
                )
            next_values.extend(value)
        values = next_values
    return values


def _render_json_input_path(
    template: str,
    *,
    flow_input: dict[str, Any],
    item: Any,
    iteration_id: str | None,
    each: Any,
) -> str:
    def replace(match: re.Match[str]) -> str:
        expr = match.group(0)
        if expr.startswith("${each"):
            field = expr[6:-1].lstrip(".")
            value = _lookup(each, field or None, label="json_inputs.each")
        else:
            dynamic = _DYNAMIC_VALUE_EXPR.fullmatch(expr)
            assert dynamic is not None
            value = _dynamic_value(
                dynamic,
                item=item,
                iteration_id=iteration_id,
                conversation_item=None,
                previous_output=None,
                outputs={},
                flow_input=flow_input,
            )
        if not isinstance(value, (str, int)) or isinstance(value, bool):
            raise ValueError(
                "json_inputs.path darf nur Strings oder Ganzzahlen einsetzen."
            )
        return str(value)

    rendered = _JSON_INPUT_TEMPLATE.sub(replace, template)
    if "${" in rendered:
        raise ValueError("json_inputs.path enthält unaufgelöste Platzhalter.")
    return rendered


def _resolve_json_inputs(
    step: FlowStep,
    *,
    workspace: Path,
    flow_input: dict[str, Any],
    item: Any,
    iteration_id: str | None,
    completed_json_outputs: dict[str, bytes],
) -> dict[str, str]:
    """Read only completed JSON outputs of prior steps; never invoke OS tools."""
    result: dict[str, str] = {}
    total_bytes = 0
    total_files = 0
    for name, spec in (step.json_inputs or {}).items():
        elements = (
            _json_input_items(spec.iterate, flow_input=flow_input, item=item)
            if spec.iterate is not None
            else [None]
        )
        records: list[Any] = []
        for element in elements:
            total_files += 1
            if total_files > MAX_JSON_INPUT_FILES:
                raise ValueError(
                    f"json_inputs überschreitet {MAX_JSON_INPUT_FILES} Dateien."
                )
            rendered = _render_json_input_path(
                spec.path,
                flow_input=flow_input,
                item=item,
                iteration_id=iteration_id,
                each=element,
            )
            path = _workspace_path(
                workspace,
                Path(rendered),
                purpose=f"JSON-Input {name!r} von Schritt {step.step_id!r}",
                must_exist=True,
            )
            expected = completed_json_outputs.get(_filesystem_path_key(path))
            if expected is None:
                raise ValueError(
                    f"JSON-Input {name!r} darf nur einen bereits abgeschlossenen "
                    f"Flow-JSON-Output referenzieren: {path}"
                )
            raw = read_existing_output_text(
                workspace,
                path,
                max_bytes=MAX_STRUCTURED_OUTPUT_BYTES,
            )
            if _checkpoint_fingerprint(raw) != expected:
                raise ValueError(
                    f"JSON-Input {name!r} wurde seit seiner Erstellung verändert: "
                    f"{path}"
                )
            total_bytes += len(raw.encode("utf-8"))
            if total_bytes > MAX_JSON_INPUT_TOTAL_BYTES:
                raise ValueError(
                    f"json_inputs überschreitet das Gesamtlimit von "
                    f"{MAX_JSON_INPUT_TOTAL_BYTES} Bytes."
                )
            records.append(_parse_structured_output(raw, step_id=step.step_id))
        value = records if spec.iterate is not None else records[0]
        result[name] = _json_dumps_preserving_numbers(value)
    return result


def _render_prompt_file(
    step: FlowStep,
    *,
    workspace: Path,
    prompt_file: Path,
    item: Any,
    iteration_id: str | None,
    conversation_item: Any,
    previous_output: Any,
    outputs: dict[str, str],
    flow_input: dict[str, Any],
    final_prompt: bool = False,
    json_input_values: dict[str, str] | None = None,
) -> str:
    prompt_path = _workspace_path(
        workspace,
        prompt_file,
        purpose=(
            f"Finale Conversation-Prompt-Datei von Schritt {step.step_id!r}"
            if final_prompt
            else f"Prompt-Datei von Schritt {step.step_id!r}"
        ),
        must_exist=True,
    )
    prompt = prepare_prompt_file(workspace, prompt_path)
    template = PromptTemplate.parse(prompt.content)
    values: dict[str, str] = {}
    for name in template.variables:
        if name in (json_input_values or {}):
            values[name] = json_input_values[name]
            continue
        if name in step.variables:
            raw = step.variables[name]
        elif name in step.global_variables:
            raw = step.global_variables[name]
        else:
            raise ValueError(
                f"Schritt {step.step_id!r} setzt Prompt-Variable {name!r} nicht."
            )
        if final_prompt and _CONVERSATION_ITEM_EXPR.search(raw):
            raise ValueError(
                f"Finaler Conversation-Prompt von Schritt {step.step_id!r} "
                f"kann Variable {name!r} mit ${{conversation.item...}} "
                "nicht verwenden."
            )
        values[name] = _render_dynamic_text(
            raw,
            item=item,
            iteration_id=iteration_id,
            conversation_item=conversation_item,
            previous_output=previous_output,
            outputs=outputs,
            flow_input=flow_input,
        )
    rendered = template.render(values)
    if not rendered or rendered.isspace():
        raise ValueError(
            f"Gerenderter Prompt von Schritt {step.step_id!r} darf nicht leer sein."
        )
    if is_local_agent_command(rendered):
        raise ValueError(
            f"Gerenderter Prompt von Schritt {step.step_id!r} "
            "darf kein lokaler Agent-Befehl sein."
        )
    return rendered


def _prompt_for_iteration(
    step: FlowStep,
    *,
    workspace: Path,
    item: Any,
    iteration_id: str | None = None,
    conversation_item: Any = None,
    previous_output: Any = None,
    outputs: dict[str, str] | None = None,
    flow_input: dict[str, Any] | None = None,
    json_input_values: dict[str, str] | None = None,
) -> str:
    assert step.prompt_file is not None
    return _render_prompt_file(
        step,
        workspace=workspace,
        prompt_file=step.prompt_file,
        item=item,
        iteration_id=iteration_id,
        conversation_item=conversation_item,
        previous_output=previous_output,
        outputs=outputs or {},
        flow_input=flow_input or {},
        json_input_values=json_input_values,
    )


def _conversation_prompts_for_iteration(
    step: FlowStep,
    *,
    workspace: Path,
    outputs: dict[str, str],
    item: Any,
    iteration_id: str | None,
    previous_output: Any,
    flow_input: dict[str, Any],
    json_input_values: dict[str, str] | None = None,
) -> tuple[str, ...] | None:
    conversation_items = _conversation_items_for_iteration(
        step,
        outputs=outputs,
        item=item,
    )
    if conversation_items is None:
        return None
    prompts = [
        _prompt_for_iteration(
            step,
            workspace=workspace,
            item=item,
            iteration_id=iteration_id,
            conversation_item=conversation_item,
            previous_output=previous_output,
            outputs=outputs,
            flow_input=flow_input,
            json_input_values=json_input_values,
        )
        for conversation_item in conversation_items
    ]
    if step.conversation_final_prompt_file is not None:
        prompts.append(
            _render_prompt_file(
                step,
                workspace=workspace,
                prompt_file=step.conversation_final_prompt_file,
                item=item,
                iteration_id=iteration_id,
                conversation_item=None,
                previous_output=previous_output,
                outputs=outputs,
                flow_input=flow_input,
                final_prompt=True,
                json_input_values=json_input_values,
            )
        )
    return tuple(prompts)


def _contexts_for_step(
    step: FlowStep,
    *,
    workspace: Path,
) -> tuple[Path, ...]:
    return tuple(
        _workspace_path(
            workspace,
            context_file,
            purpose=f"Context-Datei von Schritt {step.step_id!r}",
            must_exist=True,
        )
        for context_file in step.context_files
    )


def _output_for_iteration(
    step: FlowStep,
    *,
    workspace: Path,
    item: Any,
    iteration_id: str | None = None,
    flow_input: dict[str, Any] | None = None,
) -> Path | None:
    if step.output is None:
        return None

    rendered = (
        _render_iteration_text(
            step.output,
            item=item,
            iteration_id=iteration_id,
        )
        if step.foreach is not None
        else step.output
    )
    rendered = _render_input_text(rendered, flow_input or {})
    path = Path(rendered)
    return _workspace_path(
        workspace,
        path,
        purpose=f"Output-Datei von Schritt {step.step_id!r}",
        must_exist=False,
    )


def _load_subflow_definition(
    step: FlowStep,
    *,
    workspace: Path,
    cache: dict[str, FlowDefinition],
) -> FlowDefinition:
    path = _resolve_subflow_path(
        step,
        workspace=workspace,
    )
    key = _filesystem_path_key(path)
    cached = cache.get(key)
    if cached is not None:
        return cached
    child = load_flow(path, workspace=workspace)
    cache[key] = child
    return child


def _validate_subflow_interface(
    step: FlowStep,
    child: FlowDefinition,
) -> None:
    if child.result is None:
        raise ValueError(
            f"Subflow {child.source} benötigt ein root-level result, "
            f"weil er von Schritt {step.step_id!r} aufgerufen wird."
        )
    expected = set(child.inputs)
    provided = set(step.flow_input)
    missing = sorted(expected - provided)
    unknown = sorted(provided - expected)
    if missing or unknown:
        parts: list[str] = []
        if missing:
            parts.append("fehlend: " + ", ".join(missing))
        if unknown:
            parts.append("unbekannt: " + ", ".join(unknown))
        raise ValueError(
            f"Inputs für Subflow-Schritt {step.step_id!r} stimmen nicht mit "
            f"{child.source} überein ({'; '.join(parts)})."
        )


def _collect_reserved_flow_input_paths(
    flow: FlowDefinition,
    *,
    workspace: Path,
    cache: dict[str, FlowDefinition],
    stack: tuple[str, ...] = (),
    budget: _SubflowTraversalBudget | None = None,
) -> dict[str, Path]:
    traversal_budget = budget or _SubflowTraversalBudget()
    traversal_budget.consume(purpose="Eingabe-Validierung")
    key = _filesystem_path_key(flow.source)
    if key in stack:
        chain = " -> ".join((*stack, key))
        raise ValueError(f"Zyklischer Subflow-Aufruf erkannt: {chain}")
    if len(stack) >= MAX_SUBFLOW_DEPTH:
        raise ValueError(
            f"Maximale Subflow-Tiefe von {MAX_SUBFLOW_DEPTH} überschritten."
        )

    reserved = _reserved_flow_input_paths(flow, workspace=workspace)
    next_stack = (*stack, key)
    for step in flow.steps:
        if step.flow is None:
            continue
        _set_flow_diagnostic(
            phase="validate-reserved-subflow", flow=flow.source,
            step=step.step_id,
        )
        child = _load_subflow_definition(
            step,
            workspace=workspace,
            cache=cache,
        )
        _validate_subflow_interface(step, child)
        reserved.update(
            _collect_reserved_flow_input_paths(
                child,
                workspace=workspace,
                cache=cache,
                stack=next_stack,
                budget=traversal_budget,
            )
        )
    return reserved


def _flow_result_value(
    flow: FlowDefinition,
    outputs: dict[str, str],
    output_json: dict[str, bool],
) -> _FlowResultValue | None:
    if flow.result is None:
        return None
    match = _FOREACH.fullmatch(flow.result)
    assert match is not None
    source_id, path = match.groups()
    if source_id not in outputs:
        raise ValueError(
            f"Result-Quelle {source_id!r} von Flow {flow.source} "
            "besitzt keinen verfügbaren Output."
        )
    text = outputs[source_id]
    if path is None:
        return _FlowResultValue(
            text=text,
            is_json=output_json.get(source_id, False),
        )
    parsed = _parse_structured_output(text, step_id=source_id)
    value = _lookup(
        parsed,
        path,
        label=f"Result von Flow {flow.source}",
    )
    is_json = isinstance(value, _JsonNumber) or not isinstance(value, str)
    return _FlowResultValue(
        text=_json_dumps_preserving_numbers(value)
        if isinstance(value, (dict, list, bool, int, float, _JsonNumber))
        or value is None
        else str(value),
        is_json=is_json,
    )


def _render_subflow_inputs(
    step: FlowStep,
    *,
    item: Any,
    iteration_id: str | None,
    outputs: dict[str, str],
    output_json: dict[str, bool],
    flow_input: dict[str, Any],
) -> dict[str, Any]:
    return {
        name: _render_flow_input_value(
            value,
            item=item,
            iteration_id=iteration_id,
            outputs=outputs,
            output_json=output_json,
            flow_input=flow_input,
        )
        for name, value in step.flow_input.items()
    }


def validate_flow(
    flow: FlowDefinition,
    *,
    workspace: Path,
    config_loader: Callable[[Path | None], AppConfig] = load_config,
    admin_config_loader: Callable[[], AdminConfig] = load_admin_config,
    _cache: dict[str, FlowDefinition] | None = None,
    _stack: tuple[str, ...] = (),
    _inherited_excluded_paths: tuple[Path, ...] = (),
    _budget: _SubflowTraversalBudget | None = None,
    _planned_static_outputs: dict[str, str] | None = None,
    _all_reserved_inputs: dict[str, Path] | None = None,
) -> None:
    _set_flow_diagnostic(phase="validate-flow", flow=flow.source)
    workspace = workspace.expanduser().resolve()
    traversal_budget = _budget or _SubflowTraversalBudget()
    traversal_budget.consume(purpose="Validierung")
    cache = _cache if _cache is not None else {}
    planned_static_outputs = (
        _planned_static_outputs
        if _planned_static_outputs is not None
        else {}
    )
    flow_key = _filesystem_path_key(flow.source)
    if flow_key in _stack:
        raise ValueError("Zyklischer Subflow-Aufruf erkannt.")
    if len(_stack) >= MAX_SUBFLOW_DEPTH:
        raise ValueError(
            f"Maximale Subflow-Tiefe von {MAX_SUBFLOW_DEPTH} überschritten."
        )
    stack = (*_stack, flow_key)
    cache.setdefault(flow_key, flow)
    all_reserved_inputs = _all_reserved_inputs
    if all_reserved_inputs is None:
        all_reserved_inputs = _collect_reserved_flow_input_paths(
            flow,
            workspace=workspace,
            cache=cache,
            budget=traversal_budget,
        )
    flow_dir = flow.source.parent
    effective_flow_excluded_paths = tuple(
        dict.fromkeys((*_inherited_excluded_paths, *flow.excluded_paths))
    )
    _set_flow_diagnostic(
        phase="validate-flow-exclusions", flow=flow.source,
    )
    resolve_excluded_paths(workspace, effective_flow_excluded_paths)
    produced: set[str] = set()

    for step in flow.steps:
        _set_flow_diagnostic(
            phase="validate-step", flow=flow.source,
            step=step.step_id,
        )
        if step.flow is not None:
            child = _load_subflow_definition(
                step,
                workspace=workspace,
                cache=cache,
            )
            _validate_subflow_interface(step, child)
            validate_flow(
                child,
                workspace=workspace,
                config_loader=config_loader,
                admin_config_loader=admin_config_loader,
                _cache=cache,
                _stack=stack,
                _inherited_excluded_paths=effective_flow_excluded_paths,
                _budget=traversal_budget,
                _planned_static_outputs=planned_static_outputs,
                _all_reserved_inputs=all_reserved_inputs,
            )
            _set_flow_diagnostic(
                phase="validate-step", flow=flow.source,
                step=step.step_id,
            )
            if step.foreach is not None:
                source_id = _foreach_output_source(step.foreach)
                if source_id is not None and source_id not in produced:
                    raise ValueError(
                        f"foreach-Quelle {source_id!r} ist nicht vor "
                        f"Schritt {step.step_id!r} verfügbar."
                    )
            produced.add(step.step_id)
            continue

        if step.json_operation is not None or step.filesystem_operation is not None:
            assert step.output is not None
            if step.workspace_access != "none" or step.response_format != "json":
                raise ValueError("JSON-Operation benötigt workspace_access=none und JSON.")
            if step.foreach is not None:
                raise ValueError("JSON-Operation unterstützt kein foreach.")
            if _INPUT_EXPR.search(step.output) is None:
                static_output = _output_for_iteration(
                    step, workspace=workspace, item=None,
                )
                assert static_output is not None
                key = _filesystem_path_key(static_output)
                if key in all_reserved_inputs:
                    raise ValueError("JSON-Output kollidiert mit reservierter Eingabe.")
                _prepare_flow_output(
                    step, workspace=workspace, output=static_output,
                )
                previous = planned_static_outputs.get(key)
                if previous is not None and not step.overwrite_output:
                    raise ValueError("JSON-Output kollidiert mit geplantem Output.")
                planned_static_outputs[key] = f"{flow.source.name}:{step.step_id}"
            produced.add(step.step_id)
            continue

        assert step.prompt_file is not None
        prompt_path = _workspace_path(
            workspace,
            step.prompt_file,
            purpose=f"Prompt-Datei von Schritt {step.step_id!r}",
            must_exist=True,
        )
        prompt = prepare_prompt_file(workspace, prompt_path)
        template = PromptTemplate.parse(prompt.content)
        known_templates = [template]
        required_templates = (
            []
            if (
                isinstance(step.conversation_items, tuple)
                and not step.conversation_items
                and step.conversation_final_prompt_file is not None
            )
            else [template]
        )
        if step.conversation_final_prompt_file is not None:
            final_prompt_path = _workspace_path(
                workspace,
                step.conversation_final_prompt_file,
                purpose=(
                    f"Finale Conversation-Prompt-Datei von Schritt "
                    f"{step.step_id!r}"
                ),
                must_exist=True,
            )
            final_prompt = prepare_prompt_file(workspace, final_prompt_path)
            final_template = PromptTemplate.parse(final_prompt.content)
            known_templates.append(final_template)
            required_templates.append(final_template)
            for name in final_template.variables:
                raw = step.variables.get(name, step.global_variables.get(name))
                if raw is not None and _CONVERSATION_ITEM_EXPR.search(raw):
                    raise ValueError(
                        f"Finaler Conversation-Prompt von Schritt "
                        f"{step.step_id!r} kann Variable {name!r} mit "
                        "${conversation.item...} nicht verwenden."
                    )

        supplied = set(step.variables)
        available = supplied | set(step.global_variables) | set(step.json_inputs or {})
        known = {
            name
            for current_template in known_templates
            for name in current_template.variables
        }
        required = {
            name
            for current_template in required_templates
            for name in current_template.variables
        }
        unknown = sorted(supplied - known)
        missing = sorted(required - available)
        if unknown:
            raise ValueError(
                f"Schritt {step.step_id!r} setzt unbekannte "
                "Prompt-Variable(n): "
                + ", ".join(unknown)
            )
        if missing:
            raise ValueError(
                f"Schritt {step.step_id!r} setzt nicht alle "
                "Prompt-Variablen: "
                + ", ".join(missing)
            )

        context_paths = _contexts_for_step(
            step,
            workspace=workspace,
        )
        prepare_file_options(
            workspace,
            context_files=context_paths,
            prompt_file=None,
            output=None,
            overwrite_output=False,
        )

        config = _resolve_config(
            step,
            flow_dir=flow_dir,
        ).expanduser()
        config_argument: Path | None = None
        if step.config is not None:
            if not config.exists():
                raise ValueError(
                    f"Konfiguration von Schritt {step.step_id!r} "
                    f"existiert nicht: {config}"
                )
            if not config.is_file():
                raise ValueError(
                    f"Konfiguration von Schritt {step.step_id!r} "
                    f"ist keine Datei: {config}"
                )
            config_argument = config
        try:
            config = config_loader(config_argument)
        except Exception as exc:
            raise ValueError(
                f"Konfiguration von Schritt {step.step_id!r} "
                f"ist ungültig: {exc}"
            ) from exc

        if step.with_mcp:
            # MCP selection is static: reject invalid admin/identity/access
            # combinations before any previous step can invoke tools or write.
            try:
                apply_named_mcp_overrides(
                    config,
                    admin_config=admin_config_loader(),
                    server_names=step.with_mcp,
                    workspace_access=step.workspace_access,
                    workspace=workspace,
                )
            except (ValueError, PermissionError) as exc:
                raise type(exc)(
                    f"MCP-Auswahl von Schritt {step.step_id!r} "
                    f"ist ungültig: {exc}"
                ) from exc

        if (
            step.foreach is None
            and step.output is not None
            and _INPUT_EXPR.search(step.output) is None
        ):
            static_output = _output_for_iteration(
                step,
                workspace=workspace,
                item=None,
            )
            assert static_output is not None
            static_output_key = _filesystem_path_key(static_output)
            reserved_input = all_reserved_inputs.get(static_output_key)
            if reserved_input is not None:
                raise ValueError(
                    f"Output-Datei von Schritt {step.step_id!r} kollidiert "
                    "mit einer reservierten Flow-Eingabe: "
                    f"{reserved_input}"
                )
            _prepare_flow_output(
                step,
                workspace=workspace,
                output=static_output,
            )
            previous_writer = planned_static_outputs.get(static_output_key)
            if previous_writer is not None and not step.overwrite_output:
                raise ValueError(
                    f"Output-Datei von Schritt {step.step_id!r} kollidiert "
                    f"mit dem geplanten Output von Schritt {previous_writer!r}; "
                    "der spätere Schritt erlaubt kein Überschreiben."
                )
            planned_static_outputs[static_output_key] = (
                f"{flow.source.name}:{step.step_id}"
            )

        if step.foreach is not None:
            source_id = _foreach_output_source(step.foreach)
            if source_id is not None and source_id not in produced:
                raise ValueError(
                    f"foreach-Quelle {source_id!r} ist nicht vor "
                    f"Schritt {step.step_id!r} verfügbar."
                )

        if isinstance(step.conversation_items, str):
            source_match = _FOREACH.fullmatch(step.conversation_items)
            if source_match is not None:
                source_id = source_match.group(1)
                if source_id not in produced:
                    raise ValueError(
                        f"conversation_items-Quelle {source_id!r} ist nicht vor "
                        f"Schritt {step.step_id!r} verfügbar."
                    )

        produced.add(step.step_id)

def _preflight_concrete_invocation_outputs(
    flow: FlowDefinition,
    *,
    workspace: Path,
    cache: dict[str, FlowDefinition],
    flow_input: dict[str, Any],
    reserved_inputs: dict[str, Path],
    claimed_outputs: dict[str, str],
    namespace: str,
    run_start_snapshots: dict[str, _RunStartSnapshot],
    budget: _SubflowTraversalBudget,
    call_stack: tuple[str, ...] = (),
) -> None:
    """Preflight outputs that are concrete before an invocation starts.

    Static outputs are always checked. Foreach outputs are additionally
    checked when their source can be reconstructed from the immutable
    overall run-start snapshot for this concrete invocation. Descendants
    whose source is first produced during the current run remain deferred
    until that source becomes available.
    """

    budget.consume(purpose="Preflight")
    flow_key = _filesystem_path_key(flow.source)
    if flow_key in call_stack:
        raise ValueError("Zyklischer Subflow-Aufruf erkannt.")
    if len(call_stack) >= MAX_SUBFLOW_DEPTH:
        raise ValueError(
            f"Maximale Subflow-Tiefe von {MAX_SUBFLOW_DEPTH} überschritten."
        )
    next_stack = (*call_stack, flow_key)
    case_colliding_step_ids = _case_colliding_step_ids(
        flow,
        workspace=workspace,
    )
    snapshot = run_start_snapshots.get(namespace)
    available_outputs = (
        snapshot.available_outputs
        if snapshot is not None
        else {}
    )
    output_json = snapshot.output_json if snapshot is not None else {}

    for step in flow.steps:
        _set_flow_diagnostic(
            phase="preflight-subflow-batch", flow=flow.source,
            namespace=namespace, step=step.step_id,
        )
        if step.flow is not None:
            if step.foreach is None:
                items = [None]
                iteration_ids: list[str | None] = [None]
            else:
                source_id = _foreach_output_source(step.foreach)
                if source_id is not None and source_id not in available_outputs:
                    continue
                items = _foreach_items(
                    step,
                    outputs=available_outputs,
                    flow_input=flow_input,
                )
                iteration_ids = _iteration_ids(
                    step,
                    items=items,
                    flow_input=flow_input,
                )

            if not _subflow_inputs_resolvable_at_run_start(
                step,
                available_outputs=available_outputs,
            ):
                continue

            child = _load_subflow_definition(
                step,
                workspace=workspace,
                cache=cache,
            )
            _validate_subflow_interface(step, child)
            for item, iteration_id in zip(items, iteration_ids):
                _set_flow_diagnostic(
                    phase="preflight-render-subflow-inputs",
                    flow=flow.source, namespace=namespace,
                    step=step.step_id, iteration_id=iteration_id,
                )
                child_input = _render_subflow_inputs(
                    step,
                    item=item,
                    iteration_id=iteration_id,
                    outputs=available_outputs,
                    output_json=output_json,
                    flow_input=flow_input,
                )
                child_namespace = _child_invocation_namespace(
                    namespace,
                    step,
                    iteration_id=iteration_id,
                    case_colliding_step_ids=case_colliding_step_ids,
                )
                _preflight_concrete_invocation_outputs(
                    child,
                    workspace=workspace,
                    cache=cache,
                    flow_input=child_input,
                    reserved_inputs=reserved_inputs,
                    claimed_outputs=claimed_outputs,
                    namespace=child_namespace,
                    run_start_snapshots=run_start_snapshots,
                    budget=budget,
                    call_stack=next_stack,
                )
            continue

        if step.output is None:
            continue

        if step.foreach is None:
            _set_flow_diagnostic(
                phase="preflight-resolve-output", flow=flow.source,
                namespace=namespace, step=step.step_id,
            )
            outputs_to_claim = [
                (
                    _output_for_iteration(
                        step,
                        workspace=workspace,
                        item=None,
                        flow_input=flow_input,
                    ),
                    None,
                )
            ]
        else:
            source_id = _foreach_output_source(step.foreach)
            if source_id is not None and source_id not in available_outputs:
                continue
            items = _foreach_items(
                step,
                outputs=available_outputs,
                flow_input=flow_input,
            )
            iteration_ids = _iteration_ids(
                step,
                items=items,
                flow_input=flow_input,
            )
            outputs_to_claim = []
            for item, iteration_id in zip(items, iteration_ids):
                _set_flow_diagnostic(
                    phase="preflight-resolve-output", flow=flow.source,
                    namespace=namespace, step=step.step_id,
                    iteration_id=iteration_id,
                )
                output = _output_for_iteration(
                    step,
                    workspace=workspace,
                    item=item,
                    iteration_id=iteration_id,
                    flow_input=flow_input,
                )
                outputs_to_claim.append((output, iteration_id))
            seen_output_keys: set[str] = set()
            for output, iteration_id in outputs_to_claim:
                _set_flow_diagnostic(
                    phase="preflight-check-output-path", flow=flow.source,
                    namespace=namespace, step=step.step_id,
                    iteration_id=iteration_id, output=output,
                )
                if output is None:
                    continue
                key = _filesystem_path_key(output)
                if key in seen_output_keys:
                    raise ValueError(
                        f"Schritt {step.step_id!r} erzeugt für mehrere "
                        "foreach-Elemente nicht eindeutige Output-Pfade."
                    )
                seen_output_keys.add(key)

        for output, iteration_id in outputs_to_claim:
            assert output is not None
            _set_flow_diagnostic(
                phase="preflight-claim-output", flow=flow.source,
                namespace=namespace, step=step.step_id,
                iteration_id=iteration_id, output=output,
            )
            key = _filesystem_path_key(output)
            reserved_input = reserved_inputs.get(key)
            if reserved_input is not None:
                raise ValueError(
                    f"Output-Datei von Schritt {step.step_id!r} kollidiert "
                    "mit einer reservierten Flow-Eingabe: "
                    f"{reserved_input}"
                )
            _claim_output(
                claimed_outputs,
                path=output,
                owner=namespace + _output_owner(
                    step,
                    iteration_id=iteration_id,
                ),
                allow_replace=step.overwrite_output,
            )


async def _run_flow_internal(
    flow: FlowDefinition,
    *,
    workspace: Path,
    context: _FlowExecutionContext,
    flow_input: dict[str, Any],
    call_stack: tuple[str, ...],
    namespace: str,
    inherited_excluded_paths: tuple[Path, ...],
    inherited_retry_policy: ModelRetryPolicy | None,
) -> _FlowResultValue | None:
    flow_key = _filesystem_path_key(flow.source)
    if flow_key in call_stack:
        raise ValueError("Zyklischer Subflow-Aufruf erkannt.")
    if len(call_stack) >= MAX_SUBFLOW_DEPTH:
        raise ValueError(
            f"Maximale Subflow-Tiefe von {MAX_SUBFLOW_DEPTH} überschritten."
        )

    expected_inputs = set(flow.inputs)
    provided_inputs = set(flow_input)
    if expected_inputs != provided_inputs:
        missing = sorted(expected_inputs - provided_inputs)
        unknown = sorted(provided_inputs - expected_inputs)
        parts: list[str] = []
        if missing:
            parts.append("fehlend: " + ", ".join(missing))
        if unknown:
            parts.append("unbekannt: " + ", ".join(unknown))
        raise ValueError(
            f"Flow-Inputs für {flow.source} stimmen nicht mit der Deklaration "
            f"überein ({'; '.join(parts)})."
        )

    next_stack = (*call_stack, flow_key)
    flow_dir = flow.source.parent
    effective_flow_excluded_paths = tuple(
        dict.fromkeys((*inherited_excluded_paths, *flow.excluded_paths))
    )
    outputs: dict[str, str] = {}
    output_json: dict[str, bool] = {}
    claimed_outputs = context.claimed_outputs
    reserved_inputs = context.reserved_inputs
    assert claimed_outputs is not None
    assert reserved_inputs is not None

    assert context.run_start_snapshots is not None
    run_start_snapshot = context.run_start_snapshots.get(namespace)
    invocation_known_at_run_start = run_start_snapshot is not None
    if run_start_snapshot is None:
        run_start_snapshot = _RunStartSnapshot(
            checkpoint_fingerprints={},
            checkpoint_paths=(),
            previous_output_fingerprints={},
            available_outputs={},
            output_json={},
        )
    initial_checkpoint_fingerprints = (
        run_start_snapshot.checkpoint_fingerprints
    )
    initial_checkpoint_paths = run_start_snapshot.checkpoint_paths
    initial_previous_output_fingerprints = (
        run_start_snapshot.previous_output_fingerprints
    )
    mutation_protected_paths = tuple(
        dict.fromkeys(
            (
                *reserved_inputs.values(),
                *context.run_start_checkpoint_paths,
                *initial_checkpoint_paths,
            )
        )
    )
    case_colliding_step_ids = _case_colliding_step_ids(
        flow,
        workspace=workspace,
    )

    # Resolve defaults per invocation, never by mutating cached flow definitions.
    # Root [retry] replaces the inherited policy; step-level partial overrides
    # keep all unspecified values from that effective flow default.
    flow_retry_policy = (
        flow.retry_policy
        if flow.retry_policy is not None
        else inherited_retry_policy
    )
    for step in flow.steps:
        _set_flow_diagnostic(
            phase="prepare-step", flow=flow.source,
            namespace=namespace, step=step.step_id,
        )
        if step.retry_overrides is not None:
            defaults = flow_retry_policy or ModelRetryPolicy()
            values = step.retry_overrides
            effective_retry_policy = ModelRetryPolicy(
                max_attempts=values.get("max_attempts", defaults.max_attempts),
                initial_delay_seconds=values.get(
                    "initial_delay_seconds", defaults.initial_delay_seconds
                ),
                backoff_multiplier=values.get(
                    "backoff_multiplier", defaults.backoff_multiplier
                ),
                max_delay_seconds=values.get(
                    "max_delay_seconds", defaults.max_delay_seconds
                ),
            )
        else:
            effective_retry_policy = flow_retry_policy
        items = _foreach_items(
            step,
            outputs=outputs,
            flow_input=flow_input,
        )
        iteration_ids = _iteration_ids(
            step,
            items=items,
            flow_input=flow_input,
        )

        preflight_outputs: list[Path | None] | None = None
        preflight_checkpoint_fingerprints: list[bytes | None] | None = None

        if step.flow is not None:
            _set_flow_diagnostic(
                phase="preflight-subflow", flow=flow.source,
                namespace=namespace, step=step.step_id,
            )
            assert context.flow_cache is not None
            child = _load_subflow_definition(
                step,
                workspace=workspace,
                cache=context.flow_cache,
            )
            _validate_subflow_interface(step, child)

            # Preflight the complete concrete subflow batch before the first
            # child starts. Use a copy so execution still performs the real
            # claims, while collisions across siblings/foreach items are
            # detected without any model/tool side effects.
            batch_claims = dict(claimed_outputs)
            for item, iteration_id in zip(items, iteration_ids):
                _set_flow_diagnostic(
                    phase="preflight-render-subflow-inputs",
                    flow=flow.source, namespace=namespace,
                    step=step.step_id, iteration_id=iteration_id,
                )
                child_input = _render_subflow_inputs(
                    step,
                    item=item,
                    iteration_id=iteration_id,
                    outputs=outputs,
                    output_json=output_json,
                    flow_input=flow_input,
                )
                child_namespace = _child_invocation_namespace(
                    namespace,
                    step,
                    iteration_id=iteration_id,
                    case_colliding_step_ids=case_colliding_step_ids,
                )
                assert context.run_start_snapshots is not None
                assert context.preflight_budget is not None
                _preflight_concrete_invocation_outputs(
                    child,
                    workspace=workspace,
                    cache=context.flow_cache,
                    flow_input=child_input,
                    reserved_inputs=reserved_inputs,
                    claimed_outputs=batch_claims,
                    namespace=child_namespace,
                    run_start_snapshots=context.run_start_snapshots,
                    budget=context.preflight_budget,
                    call_stack=next_stack,
                )

        if (
            step.flow is None
            and step.foreach is not None
            and step.output is not None
        ):
            _set_flow_diagnostic(
                phase="preflight-outputs", flow=flow.source,
                namespace=namespace, step=step.step_id,
            )
            preflight_outputs = []
            for item, iteration_id in zip(items, iteration_ids):
                _set_flow_diagnostic(
                    phase="preflight-resolve-output", flow=flow.source,
                    namespace=namespace, step=step.step_id,
                    iteration_id=iteration_id,
                )
                output = _output_for_iteration(
                    step,
                    workspace=workspace,
                    item=item,
                    iteration_id=iteration_id,
                    flow_input=flow_input,
                )
                preflight_outputs.append(output)
            seen_output_keys: set[str] = set()
            for output, iteration_id in zip(preflight_outputs, iteration_ids):
                _set_flow_diagnostic(
                    phase="preflight-check-output-path", flow=flow.source,
                    namespace=namespace, step=step.step_id,
                    iteration_id=iteration_id, output=output,
                )
                if output is None:
                    continue
                key = _filesystem_path_key(output)
                if key in seen_output_keys:
                    raise ValueError(
                        f"Schritt {step.step_id!r} erzeugt für mehrere "
                        "foreach-Elemente nicht eindeutige Output-Pfade."
                    )
                seen_output_keys.add(key)
            for output, iteration_id in zip(
                preflight_outputs,
                iteration_ids,
            ):
                assert output is not None
                _set_flow_diagnostic(
                    phase="preflight-output-collision", flow=flow.source,
                    namespace=namespace, step=step.step_id,
                    iteration_id=iteration_id, output=output,
                )
                reserved_input = reserved_inputs.get(
                    _filesystem_path_key(output)
                )
                if reserved_input is not None:
                    raise ValueError(
                        f"Output-Datei von Schritt {step.step_id!r} kollidiert "
                        "mit einer reservierten Flow-Eingabe: "
                        f"{reserved_input}"
                    )
                key = _filesystem_path_key(output)
                previous_owner = claimed_outputs.get(key)
                if previous_owner is not None and not step.overwrite_output:
                    owner = _output_owner(
                        step,
                        iteration_id=iteration_id,
                    )
                    raise ValueError(
                        f"Output-Datei {output} wird in diesem Flow-Lauf bereits "
                        f"von {previous_owner} beansprucht; {owner} darf denselben "
                        "Output nicht erneut verwenden."
                    )

            for output, iteration_id in zip(
                preflight_outputs,
                iteration_ids,
            ):
                assert output is not None
                _set_flow_diagnostic(
                    phase="preflight-claim-output", flow=flow.source,
                    namespace=namespace, step=step.step_id,
                    iteration_id=iteration_id, output=output,
                )
                _claim_output(
                    claimed_outputs,
                    path=output,
                    owner=namespace + _output_owner(
                        step,
                        iteration_id=iteration_id,
                    ),
                    allow_replace=step.overwrite_output,
                )

            preflight_checkpoint_fingerprints = []
            for output, iteration_id in zip(preflight_outputs, iteration_ids):
                assert output is not None
                _set_flow_diagnostic(
                    phase="preflight-checkpoint-or-output", flow=flow.source,
                    namespace=namespace, step=step.step_id,
                    iteration_id=iteration_id, output=output,
                )
                expected_fingerprint = _run_start_fingerprint(
                    step, output, initial_checkpoint_fingerprints,
                    context.run_start_dynamic_checkpoints,
                    flow_source=flow.source,
                )
                if expected_fingerprint is not None:
                    _verified_preflight_checkpoint(
                        step,
                        workspace=workspace,
                        output=output,
                        expected_fingerprint=expected_fingerprint,
                    )
                else:
                    _ensure_output_parent_directory(workspace, output)
                    prepare_output_target(
                        workspace,
                        output,
                        overwrite=step.overwrite_output,
                    )
                preflight_checkpoint_fingerprints.append(
                    expected_fingerprint
                )

        iteration_answers: list[tuple[str, bool]] = []
        foreach_parts: list[str] = []
        foreach_payload_bytes = 0

        def record_iteration_answer(
            answer: str,
            answer_is_json: bool,
            iteration_id: str | None,
        ) -> None:
            nonlocal foreach_payload_bytes
            if step.foreach is None:
                iteration_answers.append((answer, answer_is_json))
                return
            assert iteration_id is not None
            foreach_payload_bytes = _append_foreach_iteration(
                step,
                parts=foreach_parts,
                payload_bytes=foreach_payload_bytes,
                iteration_id=iteration_id,
                answer=answer,
                answer_is_json=answer_is_json,
            )

        for index, (item, iteration_id) in enumerate(
            zip(items, iteration_ids),
            start=1,
        ):
            _set_flow_diagnostic(
                phase="execute", flow=flow.source,
                namespace=namespace, step=step.step_id,
                iteration_id=iteration_id,
            )
            context.executed_steps += 1
            if context.executed_steps > MAX_FLOW_EXECUTIONS:
                raise ValueError(
                    f"Flow-Ausführung überschreitet das globale Limit von "
                    f"{MAX_FLOW_EXECUTIONS} Step-Iterationen."
                )

            suffix = (
                f" [{index}/{len(items)}; id={iteration_id}]"
                if step.foreach is not None
                else ""
            )
            display_name = namespace + step.step_id
            print(
                sanitize_terminal_text(
                    f"Flow-Schritt {display_name}{suffix}",
                    multiline=False,
                    escape_invisible_formatting=True,
                    escape_literal_backslashes=True,
                )
            )

            if step.flow is not None:
                assert context.flow_cache is not None
                child = _load_subflow_definition(
                    step,
                    workspace=workspace,
                    cache=context.flow_cache,
                )
                _validate_subflow_interface(step, child)
                child_input = _render_subflow_inputs(
                    step,
                    item=item,
                    iteration_id=iteration_id,
                    outputs=outputs,
                    output_json=output_json,
                    flow_input=flow_input,
                )
                child_namespace = _child_invocation_namespace(
                    namespace,
                    step,
                    iteration_id=iteration_id,
                    case_colliding_step_ids=case_colliding_step_ids,
                )
                child_result = await _run_flow_internal(
                    child,
                    workspace=workspace,
                    context=context,
                    flow_input=child_input,
                    call_stack=next_stack,
                    namespace=child_namespace,
                    inherited_excluded_paths=effective_flow_excluded_paths,
                    inherited_retry_policy=effective_retry_policy,
                )
                if child_result is None:
                    raise ValueError(
                        f"Subflow {child.source} besitzt kein Ergebnis."
                    )
                _set_flow_diagnostic(
                    phase="aggregate-subflow-result", flow=flow.source,
                    namespace=namespace, step=step.step_id,
                    iteration_id=iteration_id,
                )
                record_iteration_answer(
                    child_result.text,
                    child_result.is_json,
                    iteration_id,
                )
                continue

            output = (
                preflight_outputs[index - 1]
                if preflight_outputs is not None
                else _output_for_iteration(
                    step,
                    workspace=workspace,
                    item=item,
                    iteration_id=iteration_id,
                    flow_input=flow_input,
                )
            )
            _set_flow_diagnostic(
                phase="resolve-output", flow=flow.source,
                namespace=namespace, step=step.step_id,
                iteration_id=iteration_id, output=output,
            )
            if output is not None:
                reserved_input = reserved_inputs.get(
                    _filesystem_path_key(output)
                )
                if reserved_input is not None:
                    raise ValueError(
                        f"Output-Datei von Schritt {step.step_id!r} kollidiert "
                        "mit einer reservierten Flow-Eingabe: "
                        f"{reserved_input}"
                    )
                if preflight_outputs is None:
                    _claim_output(
                        claimed_outputs,
                        path=output,
                        owner=namespace + _output_owner(
                            step,
                            iteration_id=iteration_id,
                        ),
                        allow_replace=step.overwrite_output,
                    )

            if preflight_checkpoint_fingerprints is not None:
                expected_fingerprint = preflight_checkpoint_fingerprints[index - 1]
            else:
                expected_fingerprint = (
                    _run_start_fingerprint(
                        step, output, initial_checkpoint_fingerprints,
                        context.run_start_dynamic_checkpoints,
                        flow_source=flow.source,
                    )
                    if output is not None
                    else None
                )

            _set_flow_diagnostic(
                phase="checkpoint-or-output-preparation", flow=flow.source,
                namespace=namespace, step=step.step_id,
                iteration_id=iteration_id, output=output,
            )
            if expected_fingerprint is not None:
                checkpoint = _verified_preflight_checkpoint(
                    step,
                    workspace=workspace,
                    output=output,
                    expected_fingerprint=expected_fingerprint,
                )
            else:
                checkpoint = None
                if output is not None and preflight_outputs is None:
                    _ensure_output_parent_directory(workspace, output)
                    prepare_output_target(
                        workspace,
                        output,
                        overwrite=step.overwrite_output,
                    )

            if checkpoint is not None:
                assert output is not None
                assert context.completed_json_outputs is not None
                context.completed_json_outputs[_filesystem_path_key(output)] = (
                    _checkpoint_fingerprint(checkpoint)
                )
                print(
                    sanitize_terminal_text(
                        f"Flow-Schritt {display_name}{suffix}: "
                        f"vorhandenen JSON-Checkpoint verwendet: {output}",
                        multiline=False,
                        escape_invisible_formatting=True,
                        escape_literal_backslashes=True,
                    )
                )
                record_iteration_answer(
                    checkpoint,
                    True,
                    iteration_id,
                )
                continue

            try:
                if (
                    not invocation_known_at_run_start
                    and _uses_previous_output(step)
                ):
                    previous_output = None
                else:
                    previous_output = _previous_output_from_snapshot(
                        step,
                        workspace=workspace,
                        output=output,
                        fingerprints=initial_previous_output_fingerprints,
                    )
            except ValueError as exc:
                if step.foreach is not None and _uses_previous_output(step):
                    raise ValueError(
                        f"{exc} Bei foreach muss die Quelle vollständig aus "
                        "Run-Start-Checkpoints ableitbar sein."
                    ) from exc
                raise

            if step.filesystem_operation is not None:
                assert output is not None
                op = step.filesystem_operation
                directory = _render_input_text(op["directory"], flow_input)
                inventory_root = _workspace_directory(workspace, directory)
                if output == inventory_root or inventory_root in output.parents:
                    raise ValueError("Inventur-Output darf nicht im inventarisierten Verzeichnis liegen.")
                transformed = inventory_directory(
                    workspace, directory,
                    recursive=op.get("recursive", False),
                    extensions=tuple(op.get("extensions", [])),
                    exclude_names=tuple(op.get("exclude_names", [])),
                    max_entries=op.get("max_entries", MAX_INVENTORY_ENTRIES),
                )
                answer = _json_dumps_preserving_numbers(transformed)
                if len(answer.encode("utf-8")) > MAX_STRUCTURED_OUTPUT_BYTES:
                    raise ValueError("Inventur überschreitet Output-Größenlimit.")
                target = prepare_output_target(
                    workspace, output, overwrite=step.overwrite_output,
                )
                target.write_text(answer)
                context.completed_json_outputs[_filesystem_path_key(output)] = _checkpoint_fingerprint(answer)
                record_iteration_answer(answer, True, iteration_id)
                continue

            if step.json_operation is not None:
                assert output is not None
                assert context.completed_json_outputs is not None
                _set_flow_diagnostic(
                    phase="json-operation", flow=flow.source,
                    namespace=namespace, step=step.step_id, output=output,
                )
                json_values = _resolve_json_inputs(
                    step,
                    workspace=workspace,
                    flow_input=flow_input,
                    item=item,
                    iteration_id=iteration_id,
                    completed_json_outputs=context.completed_json_outputs,
                )
                operation = step.json_operation
                source = _parse_structured_output(
                    json_values[operation["input"]], step_id=step.step_id,
                )
                if operation["kind"] == "group_items":
                    transformed = group_assigned_items(
                        routes=_parse_structured_output(json_values["routes"], step_id=step.step_id),
                        assignments=_parse_structured_output(json_values["assignments"], step_id=step.step_id),
                        topic_plan=_parse_structured_output(json_values["topics"], step_id=step.step_id),
                        max_concept_bytes=operation.get("max_concept_bytes", 80000),
                        serializer=_json_dumps_preserving_numbers,
                    )
                elif operation["kind"] == "verify_coverage":
                    transformed = verify_assigned_coverage(
                        plan=_parse_structured_output(json_values["plan"], step_id=step.step_id),
                        results=_parse_structured_output(json_values["results"], step_id=step.step_id),
                    )
                else:
                    transformed = transform_json(
                        kind=operation["kind"],
                        value=source,
                        batch_size=operation.get("batch_size", 5),
                        max_batch_bytes=operation.get("max_batch_bytes", 80000),
                        serializer=_json_dumps_preserving_numbers,
                    )
                answer = _json_dumps_preserving_numbers(transformed)
                if len(answer.encode("utf-8")) > MAX_STRUCTURED_OUTPUT_BYTES:
                    raise ValueError("JSON-Operation überschreitet Output-Größenlimit.")
                target = prepare_output_target(
                    workspace, output, overwrite=step.overwrite_output,
                )
                target.write_text(answer)
                context.completed_json_outputs[_filesystem_path_key(output)] = (
                    _checkpoint_fingerprint(answer)
                )
                record_iteration_answer(answer, True, iteration_id)
                continue

            contexts = _contexts_for_step(
                step,
                workspace=workspace,
            )
            config_file = (
                _resolve_config(
                    step,
                    flow_dir=flow_dir,
                )
                if step.config is not None
                else None
            )
            callback = build_preapproval_callback(
                step.approve_tools,
                fallback=context.approval_callback,
            )
            excluded_paths = (
                tuple(
                    dict.fromkeys(
                        (
                            *effective_flow_excluded_paths,
                            *step.excluded_paths,
                        )
                    )
                )
                if step.workspace_access in {"read", "write"}
                else ()
            )
            local_dump_prefix = _dump_prefix_for_iteration(
                step,
                index=index,
                case_colliding_step_ids=case_colliding_step_ids,
                iteration_id=(
                    iteration_id
                    if step.iteration_id is not None
                    else None
                ),
            )
            dump_file_prefix = (
                namespace.replace("/", ".") + local_dump_prefix
                if namespace
                else local_dump_prefix
            )

            _set_flow_diagnostic(
                phase="model-or-mcp-execution", flow=flow.source,
                namespace=namespace, step=step.step_id,
                iteration_id=iteration_id, output=output,
            )
            assert context.completed_json_outputs is not None
            json_input_values = _resolve_json_inputs(
                step,
                workspace=workspace,
                flow_input=flow_input,
                item=item,
                iteration_id=iteration_id,
                completed_json_outputs=context.completed_json_outputs,
            )
            conversation_prompts = _conversation_prompts_for_iteration(
                step,
                workspace=workspace,
                outputs=outputs,
                item=item,
                iteration_id=iteration_id,
                previous_output=previous_output,
                flow_input=flow_input,
                json_input_values=json_input_values,
            )
            if conversation_prompts is None:
                prompt = _prompt_for_iteration(
                    step,
                    workspace=workspace,
                    item=item,
                    iteration_id=iteration_id,
                    previous_output=previous_output,
                    outputs=outputs,
                    flow_input=flow_input,
                    json_input_values=json_input_values,
                )
                result = await run_once(
                    OneShotRunOptions(
                        workspace=workspace,
                        prompt=prompt,
                        config_file=config_file,
                        model=step.model,
                        workspace_access=step.workspace_access,
                        with_data=step.with_data,
                        with_mcp=step.with_mcp,
                        retry_policy=effective_retry_policy,
                        response_format=step.response_format,
                        context_files=contexts,
                        add_web_context=step.add_web_context,
                        output=output,
                        overwrite_output=step.overwrite_output,
                        approval_callback=callback,
                        user_message_callback=context.user_message_callback,
                        mutation_protected_paths=mutation_protected_paths,
                        excluded_paths=excluded_paths,
                        dump_file_prefix=dump_file_prefix,
                    ),
                    dependencies=context.dependencies,
                )
            else:
                result = await run_conversation(
                    ConversationRunOptions(
                        workspace=workspace,
                        prompts=conversation_prompts,
                        config_file=config_file,
                        model=step.model,
                        workspace_access=step.workspace_access,
                        with_data=step.with_data,
                        with_mcp=step.with_mcp,
                        retry_policy=effective_retry_policy,
                        response_format=step.response_format,
                        context_files=contexts,
                        add_web_context=step.add_web_context,
                        output=output,
                        overwrite_output=step.overwrite_output,
                        approval_callback=callback,
                        user_message_callback=context.user_message_callback,
                        mutation_protected_paths=mutation_protected_paths,
                        excluded_paths=excluded_paths,
                        dump_file_prefix=dump_file_prefix,
                    ),
                    dependencies=context.dependencies,
                )
            if output is not None and step.response_format == "json":
                _parse_structured_output(result.answer, step_id=step.step_id)
                # OutputTarget writes UTF-8 text with unmodified newlines.
                # read_existing_output_text subsequently normalizes them and
                # strips an optional UTF-8 BOM. Match exactly that canonical
                # representation without an extra filesystem read: existing
                # one-shot execution mocks need not write an output file.
                canonical_json = (
                    result.answer.removeprefix("\ufeff")
                    .replace("\r\n", "\n")
                    .replace("\r", "\n")
                )
                context.completed_json_outputs[_filesystem_path_key(output)] = (
                    _checkpoint_fingerprint(canonical_json)
                )
            record_iteration_answer(
                result.answer,
                step.response_format == "json",
                iteration_id,
            )
            for status in getattr(result, "web_context_statuses", ()):
                print(
                    sanitize_terminal_text(
                        status,
                        multiline=True,
                    )
                )
            if output is None:
                print(
                    sanitize_terminal_text(
                        result.answer,
                        multiline=True,
                    )
                )

        if step.foreach is None:
            assert len(iteration_answers) == 1
            outputs[step.step_id] = iteration_answers[0][0]
            output_json[step.step_id] = iteration_answers[0][1]
        else:
            outputs[step.step_id] = _finish_foreach_output(
                foreach_parts
            )
            output_json[step.step_id] = True

    return _flow_result_value(
        flow,
        outputs,
        output_json,
    )


async def run_flow(
    flow: FlowDefinition,
    *,
    workspace: Path,
    dependencies: ExecutionDependencies | None = None,
    approval_callback: ApprovalCallback | None = None,
    user_message_callback: UserMessageCallback | None = _show_mcp_user_message,
) -> None:
    workspace = workspace.expanduser().resolve()
    deps = dependencies or ExecutionDependencies()
    cache = {_filesystem_path_key(flow.source): flow}
    validation_budget = _SubflowTraversalBudget()
    validate_flow(
        flow,
        workspace=workspace,
        config_loader=deps.load_config,
        admin_config_loader=deps.load_admin_config,
        _cache=cache,
        _budget=validation_budget,
    )
    if flow.inputs:
        raise ValueError(
            f"Flow {flow.source} deklariert Inputs und kann nicht direkt ohne "
            "aufrufenden Flow ausgeführt werden: "
            + ", ".join(flow.inputs)
        )
    _set_flow_diagnostic(phase="collect-reserved-inputs", flow=flow.source)
    reserved_inputs = _collect_reserved_flow_input_paths(
        flow,
        workspace=workspace,
        cache=cache,
        budget=_SubflowTraversalBudget(),
    )
    _set_flow_diagnostic(phase="run-start-snapshot", flow=flow.source)
    run_start_snapshots, run_start_checkpoint_paths = (
        _capture_run_start_snapshots(
            workspace=workspace,
            root_flow=flow,
            cache=cache,
        )
    )
    known_fingerprints: dict[str, bytes] = {}
    for snapshot in run_start_snapshots.values():
        for (_, path_key), fingerprint in snapshot.checkpoint_fingerprints.items():
            known_fingerprints[path_key] = fingerprint
        for (_, path_key), fingerprint in snapshot.previous_output_fingerprints.items():
            if fingerprint is not None:
                known_fingerprints[path_key] = fingerprint

    _set_flow_diagnostic(phase="dynamic-checkpoint-discovery", flow=flow.source)
    dynamic_fingerprints, dynamic_paths = (
        _snapshot_dynamic_checkpoint_candidates(
            workspace=workspace,
            cache=cache,
            known_checkpoint_fingerprints=known_fingerprints,
        )
    )
    context = _FlowExecutionContext(
        dependencies=deps,
        approval_callback=approval_callback,
        user_message_callback=user_message_callback,
        reserved_inputs=reserved_inputs,
        flow_cache=cache,
        run_start_snapshots=run_start_snapshots,
        run_start_checkpoint_paths=tuple(
            dict.fromkeys((*run_start_checkpoint_paths, *dynamic_paths))
        ),
        run_start_dynamic_checkpoints=dynamic_fingerprints,
    )
    _set_flow_diagnostic(phase="execute-flow", flow=flow.source)
    await _run_flow_internal(
        flow,
        workspace=workspace,
        context=context,
        flow_input={},
        call_stack=(),
        namespace="",
        inherited_excluded_paths=(),
        inherited_retry_policy=None,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli-agent-flow",
        description=(
            "Run deterministic multi-step cli-agent workflows "
            "in one fixed workspace."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )
    for name in ("validate", "run"):
        sub = subparsers.add_parser(name)
        sub.add_argument("flow", type=Path)
        sub.add_argument(
            "--debug",
            action="store_true",
            help="Bei einem Fehler zusätzlich den vollständigen Traceback ausgeben.",
        )
        sub.add_argument(
            "--workspace",
            type=Path,
            default=Path.cwd(),
            help=(
                "Fixed workspace for the complete flow "
                "(default: current directory)."
            ),
        )
    return parser


def _initial_flow_logging_config(
    flow: FlowDefinition,
    *,
    workspace: Path,
) -> tuple[Path, Path, str] | None:
    """Use a step config when no executable step depends on the default.

    Inspect the bounded, cached subflow graph so explicit-config-only flows
    never read or create an unrelated default user configuration.
    """
    cache = {_filesystem_path_key(flow.source): flow}
    visited: set[str] = set()
    pending = [(flow, 0)]
    first_explicit: tuple[Path, Path, str] | None = None
    while pending:
        current, depth = pending.pop()
        if depth >= MAX_SUBFLOW_DEPTH:
            raise ValueError(
                f"Maximale Subflow-Tiefe von {MAX_SUBFLOW_DEPTH} überschritten."
            )
        key = _filesystem_path_key(current.source)
        if key in visited:
            continue
        visited.add(key)
        if len(visited) > MAX_SUBFLOW_GRAPH_EXPANSIONS:
            raise ValueError("Subflow-Logging-Konfigurationssuche überschreitet das Limit.")
        for step in current.steps:
            _set_flow_diagnostic(
                phase="discover-logging-config",
                flow=current.source, step=step.step_id,
            )
            if step.flow is not None:
                if depth + 1 >= MAX_SUBFLOW_DEPTH:
                    raise ValueError(
                        f"Maximale Subflow-Tiefe von {MAX_SUBFLOW_DEPTH} überschritten."
                    )
                pending.append((
                    _load_subflow_definition(
                        step,
                        workspace=workspace,
                        cache=cache,
                    ),
                    depth + 1,
                ))
            elif step.json_operation is not None or step.filesystem_operation is not None:
                continue
            elif step.config is None:
                return None
            elif first_explicit is None:
                first_explicit = (
                    _resolve_config(step, flow_dir=current.source.parent),
                    current.source,
                    step.step_id,
                )
    return first_explicit


def main() -> None:
    args = build_parser().parse_args()
    workspace = args.workspace.expanduser().resolve()
    _flow_diagnostic_context.set(_FlowDiagnosticState())
    logging_initialized = False
    try:
        _set_flow_diagnostic(phase="load-flow", flow=args.flow)
        flow = load_flow(
            args.flow,
            workspace=workspace,
        )
        _set_flow_diagnostic(phase="configure-logging", flow=flow.source)
        config_source = (
            _initial_flow_logging_config(flow, workspace=workspace)
            if isinstance(flow, FlowDefinition)
            else None
        )
        if config_source is None:
            config = load_config()
        else:
            config_path, config_flow, config_step = config_source
            _set_flow_diagnostic(
                phase="configure-step-logging",
                flow=config_flow, step=config_step,
            )
            if not config_path.exists():
                raise ValueError(
                    f"Konfiguration von Schritt {config_step!r} "
                    f"existiert nicht: {config_path}"
                )
            if not config_path.is_file():
                raise ValueError(
                    f"Konfiguration von Schritt {config_step!r} "
                    f"ist keine Datei: {config_path}"
                )
            try:
                config = load_config(config_path)
            except Exception as exc:
                raise ValueError(
                    f"Konfiguration von Schritt {config_step!r} "
                    f"ist ungültig: {exc}"
                ) from exc
        configure_logging(config.logging)
        logging_initialized = True
        _set_flow_diagnostic(phase="validate-flow", flow=flow.source)
        validate_flow(
            flow,
            workspace=workspace,
        )
        if args.command == "validate":
            print(
                sanitize_terminal_text(
                    f"Flow gültig: {flow.source} "
                    f"({len(flow.steps)} Schritte, "
                    f"Workspace: {workspace})",
                    multiline=False,
                    escape_invisible_formatting=True,
                    escape_literal_backslashes=True,
                )
            )
            return
        asyncio.run(
            run_flow(
                flow,
                workspace=workspace,
                approval_callback=approve_tool_call,
            )
        )
    except Exception as exc:
        state = _flow_diagnostic_context.get()
        details = state.details if state is not None else ""
        if isinstance(exc, OSError):
            operation_path = exc.filename or getattr(exc, "filename2", None)
            if operation_path is not None:
                details += f" | os_path={operation_path}"
            if exc.errno is not None:
                details += f" | errno={exc.errno}"
            if getattr(exc, "winerror", None) is not None:
                details += f" | winerror={exc.winerror}"
        message = f"{type(exc).__name__}: {exc}"
        if details:
            message += f"\nFlow-Diagnose: {details}"
        print(
            "Fehler: " + sanitize_terminal_text(
                message, multiline=True,
                escape_invisible_formatting=True,
            ),
            file=sys.stderr,
        )
        # A failed logging setup may leave no handler. Avoid Python's
        # lastResort/root-handler fallback, which bypasses terminal sanitization.
        active_logger = logging.getLogger("cli_agent")
        if (
            logging_initialized
            and active_logger.handlers
            and active_logger.isEnabledFor(logging.ERROR)
        ):
            _flow_logger.error("flow_failed %s", details, exc_info=True)
        if args.debug:
            traceback_text = "".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
            print(
                sanitize_terminal_text(
                    traceback_text,
                    multiline=True,
                    escape_invisible_formatting=True,
                ),
                end="",
                file=sys.stderr,
            )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
