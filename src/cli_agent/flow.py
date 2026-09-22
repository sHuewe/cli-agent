from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any, Callable

from .cli import approve_tool_call
from .config import AppConfig, default_config_file, load_config
from .execution import (
    ApprovalCallback,
    ExecutionDependencies,
    ConversationRunOptions,
    OneShotRunOptions,
    build_preapproval_callback,
    resolve_excluded_paths,
    run_conversation,
    run_once,
)
from .file_context import (
    prepare_file_options,
    prepare_output_target,
    prepare_prompt_file,
    read_existing_output_text,
)
from .filesystem_security import path_entry_is_symlink_or_reparse
from .model import ModelRetryPolicy
from .prompt_template import PromptTemplate
from .terminal_output import sanitize_terminal_text

MAX_FLOW_STEPS = 100
MAX_FOREACH_ITEMS = 1000
MAX_FLOW_FILE_BYTES = 1_000_000
MAX_STRUCTURED_OUTPUT_BYTES = 10_000_000

_STEP_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
_ITEM_EXPR = re.compile(
    r"\$\{item(?:\.([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?\}"
)
_ITERATION_ID_EXPR = re.compile(r"\$\{iteration\.id\}")
_CONVERSATION_ITEM_EXPR = re.compile(
    r"\$\{conversation\.item(?:\.([A-Za-z_][A-Za-z0-9_-]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?\}"
)
_DYNAMIC_VALUE_EXPR = re.compile(
    r"\$\{(?:(?P<iteration>iteration\.id)"
    r"|item(?:\.(?P<item_path>[A-Za-z_][A-Za-z0-9_-]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?"
    r"|conversation\.item(?:\.(?P<conversation_path>[A-Za-z_][A-Za-z0-9_-]*"
    r"(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?)\}"
)
_ITERATION_ID = re.compile(r"[A-Za-z0-9_-]+\Z")
_FOREACH = re.compile(
    r"steps\.([A-Za-z_][A-Za-z0-9_-]*)\.output"
    r"(?:\.([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?\Z"
)


@dataclass(frozen=True)
class FlowStep:
    step_id: str
    config: Path | None
    model: str | None
    prompt_file: Path
    context_files: tuple[Path, ...]
    add_web_context: tuple[str, ...]
    output: str | None
    overwrite_output: bool
    workspace_access: str
    retry_policy: ModelRetryPolicy | None
    approve_tools: tuple[str, ...]
    variables: dict[str, str]
    foreach: str | None
    conversation_items: tuple[Any, ...] | str | None = None
    conversation_final_prompt_file: Path | None = None
    iteration_id: str | None = None
    excluded_paths: tuple[Path, ...] = ()
    response_format: str = "text"


@dataclass(frozen=True)
class FlowDefinition:
    source: Path
    steps: tuple[FlowStep, ...]
    excluded_paths: tuple[Path, ...] = ()


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


def _workspace_local_config_path(
    step: FlowStep,
    *,
    workspace: Path,
    flow_dir: Path,
) -> Path | None:
    if step.config is None:
        return None
    config = _resolve_config(step, flow_dir=flow_dir).expanduser()
    try:
        resolved = config.resolve(strict=True)
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

    allowed_root = {"version", "steps", "exclude_paths"}
    unknown_root = set(values) - allowed_root
    if unknown_root:
        raise ValueError(
            "Unbekannte Flow-Schlüssel: "
            + ", ".join(sorted(unknown_root))
        )
    if values.get("version") != 1:
        raise ValueError("Flow-Datei benötigt version = 1.")

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
        "exclude_paths",
        "retry",
        "response_format",
        "approve_tools",
        "vars",
        "foreach",
        "conversation_items",
        "conversation_final_prompt_file",
        "iteration_id",
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

        prompt_file = Path(
            _string(
                raw.get("prompt_file"),
                field=f"steps[{index}].prompt_file",
            )
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
                _ITEM_EXPR.search(value) or _CONVERSATION_ITEM_EXPR.search(value)
                for value in context_strings
            ):
                raise ValueError(
                    f"{context_field} darf nicht aus foreach- oder "
                    "Conversation-Daten parametrisiert werden."
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
            _ITEM_EXPR.search(value) or _CONVERSATION_ITEM_EXPR.search(value)
            for value in add_web_context
        ):
            raise ValueError(
                f"steps[{index}].add_web_context darf nicht aus foreach- oder "
                "Conversation-Daten parametrisiert werden."
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
            else None
        )
        model_value = raw.get("model")
        model = (
            _string(
                model_value,
                field=f"steps[{index}].model",
            )
            if model_value is not None
            else None
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
        if output is not None and _CONVERSATION_ITEM_EXPR.search(output):
            raise ValueError(
                f"steps[{index}].output darf nicht aus Conversation-Daten "
                "parametrisiert werden."
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
        step_excluded_paths = _path_list(
            raw.get("exclude_paths"),
            field=f"steps[{index}].exclude_paths",
        )
        if step_excluded_paths and workspace_access == "none":
            raise ValueError(
                f"steps[{index}].exclude_paths benötigen workspace_access='read' "
                "oder 'write'."
            )

        response_format_value = raw.get("response_format", "text")
        if response_format_value not in {"text", "json"}:
            raise ValueError(
                f"steps[{index}].response_format muss 'text' oder 'json' sein."
            )
        response_format = str(response_format_value)

        raw_retry = raw.get("retry")
        retry_policy: ModelRetryPolicy | None = None
        if raw_retry is not None:
            if not isinstance(raw_retry, dict):
                raise ValueError(
                    f"steps[{index}].retry muss eine Tabelle sein."
                )
            allowed_retry = {
                "max_attempts",
                "initial_delay_seconds",
                "backoff_multiplier",
                "max_delay_seconds",
            }
            unknown_retry = set(raw_retry) - allowed_retry
            if unknown_retry:
                raise ValueError(
                    f"steps[{index}].retry enthält unbekannte Schlüssel: "
                    + ", ".join(sorted(unknown_retry))
                )
            retry_policy = ModelRetryPolicy(
                max_attempts=raw_retry.get("max_attempts", 1),
                initial_delay_seconds=raw_retry.get(
                    "initial_delay_seconds",
                    1.0,
                ),
                backoff_multiplier=raw_retry.get(
                    "backoff_multiplier",
                    2.0,
                ),
                max_delay_seconds=raw_retry.get(
                    "max_delay_seconds",
                    10.0,
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
            _ITEM_EXPR.search(value) or _CONVERSATION_ITEM_EXPR.search(value)
            for value in approve_tools
        ):
            raise ValueError(
                f"steps[{index}].approve_tools darf nicht aus foreach- oder "
                "Conversation-Daten parametrisiert werden."
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
        if foreach is not None:
            match = _FOREACH.fullmatch(foreach)
            if match is None:
                raise ValueError(
                    f"steps[{index}].foreach muss auf "
                    "steps.<id>.output[.<pfad>] verweisen."
                )
            source_id = match.group(1)
            if source_id not in known_ids:
                raise ValueError(
                    f"steps[{index}].foreach darf nur auf einen "
                    f"vorherigen Schritt verweisen: {source_id!r}."
                )
        else:
            dynamic_values = list(variables.values())
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

        steps.append(
            FlowStep(
                step_id=step_id,
                config=config,
                model=model,
                prompt_file=prompt_file,
                context_files=context_files,
                add_web_context=add_web_context,
                output=output,
                overwrite_output=overwrite_output,
                workspace_access=workspace_access,
                retry_policy=retry_policy,
                response_format=response_format,
                approve_tools=approve_tools,
                variables=variables,
                foreach=foreach,
                conversation_items=conversation_items,
                conversation_final_prompt_file=conversation_final_prompt_file,
                iteration_id=iteration_id,
                excluded_paths=step_excluded_paths,
            )
        )

    return FlowDefinition(
        source=source,
        steps=tuple(steps),
        excluded_paths=excluded_paths,
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
    # Substitute flow-owned placeholders before inserting untrusted item values.
    # Item content that happens to contain '${iteration.id}' must stay literal
    # instead of being interpreted recursively as flow syntax.
    rendered = template
    if iteration_id is not None:
        rendered = _ITERATION_ID_EXPR.sub(iteration_id, rendered)
    return _render_item_text(rendered, item)


def _render_dynamic_text(
    template: str,
    *,
    item: Any,
    iteration_id: str | None,
    conversation_item: Any,
) -> str:
    def replace(match: re.Match[str]) -> str:
        if match.group("iteration") is not None:
            if iteration_id is None:
                raise ValueError("${iteration.id} ist ohne foreach nicht verfügbar.")
            value: Any = iteration_id
        elif match.group("item_path") is not None or match.group(0).startswith("${item"):
            value = _lookup(
                item,
                match.group("item_path"),
                label="foreach-Element",
            )
        else:
            value = _lookup(
                conversation_item,
                match.group("conversation_path"),
                label="Conversation-Element",
            )
        if isinstance(value, (dict, list)):
            return _json_dumps_preserving_numbers(value)
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return _DYNAMIC_VALUE_EXPR.sub(replace, template)


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
) -> list[str | None]:
    if step.foreach is None:
        return [None]
    if step.iteration_id is None:
        return [str(index) for index in range(1, len(items) + 1)]

    bases: list[str] = []
    for item in items:
        base = _render_item_text(step.iteration_id, item).strip()
        if not base or _ITERATION_ID.fullmatch(base) is None:
            raise ValueError(
                f"Schritt {step.step_id!r} erzeugt eine ungültige iteration_id "
                f"{base!r}; erlaubt sind ASCII-Buchstaben, Ziffern, '_' und '-'."
            )
        bases.append(base)

    # Reserve all natural IDs before assigning suffixes. Otherwise a duplicate
    # such as "card" could consume "card-2" before the item whose actual base
    # ID is "card-2" is processed, making resume checkpoints unstable.
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


def _existing_json_checkpoint(
    step: FlowStep,
    *,
    workspace: Path,
    output: Path | None,
) -> str | None:
    if (
        output is None
        or step.overwrite_output
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
    )
    return None


def _snapshot_initial_checkpoints(
    flow: FlowDefinition,
    *,
    workspace: Path,
) -> tuple[dict[tuple[str, str], bytes], tuple[Path, ...]]:
    """Snapshot resumable checkpoints before the first model call.

    Static JSON checkpoints are known directly. Foreach checkpoints are only
    eligible when their source step is itself resumed from a pre-existing
    static JSON checkpoint, so the concrete iteration outputs can also be
    derived before any model/tool execution.
    """

    foreach_consumers: dict[str, list[FlowStep]] = {}
    for candidate in flow.steps:
        if candidate.foreach is None:
            continue
        match = _FOREACH.fullmatch(candidate.foreach)
        assert match is not None
        foreach_consumers.setdefault(match.group(1), []).append(candidate)

    fingerprints: dict[tuple[str, str], bytes] = {}
    paths: dict[str, Path] = {}

    def remember(step: FlowStep, output: Path, checkpoint: str) -> None:
        path_key = _filesystem_path_key(output)
        fingerprints[_checkpoint_snapshot_key(step, output)] = (
            _checkpoint_fingerprint(checkpoint)
        )
        paths[path_key] = output

    for step in flow.steps:
        if (
            step.foreach is not None
            or step.output is None
            or step.overwrite_output
            or step.response_format != "json"
        ):
            continue

        output = _output_for_iteration(
            step,
            workspace=workspace,
            item=None,
        )
        assert output is not None
        checkpoint = _existing_json_checkpoint(
            step,
            workspace=workspace,
            output=output,
        )
        if checkpoint is None:
            continue

        remember(step, output, checkpoint)

        for consumer in foreach_consumers.get(step.step_id, ()):
            if (
                consumer.output is None
                or consumer.overwrite_output
                or consumer.response_format != "json"
            ):
                continue
            items = _foreach_items(
                consumer,
                outputs={step.step_id: checkpoint},
            )
            iteration_ids = _iteration_ids(
                consumer,
                items=items,
            )
            consumer_outputs = [
                _output_for_iteration(
                    consumer,
                    workspace=workspace,
                    item=item,
                    iteration_id=iteration_id,
                )
                for item, iteration_id in zip(items, iteration_ids)
            ]
            output_keys = [
                _filesystem_path_key(candidate)
                for candidate in consumer_outputs
                if candidate is not None
            ]
            if len(output_keys) != len(set(output_keys)):
                raise ValueError(
                    f"Schritt {consumer.step_id!r} erzeugt für mehrere "
                    "foreach-Elemente nicht eindeutige Output-Pfade."
                )

            for candidate in consumer_outputs:
                assert candidate is not None
                consumer_checkpoint = _existing_json_checkpoint(
                    consumer,
                    workspace=workspace,
                    output=candidate,
                )
                if consumer_checkpoint is not None:
                    remember(
                        consumer,
                        candidate,
                        consumer_checkpoint,
                    )

    return fingerprints, tuple(paths.values())


_FOREACH_AGGREGATE_PREFIX = '{"iterations":['
_FOREACH_AGGREGATE_SUFFIX = "]}"


def _serialize_foreach_iteration(
    step: FlowStep,
    *,
    iteration_id: str,
    answer: str,
) -> str:
    value: Any = answer
    if step.response_format == "json":
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
) -> int:
    fragment = _serialize_foreach_iteration(
        step,
        iteration_id=iteration_id,
        answer=answer,
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
) -> list[Any]:
    if step.foreach is None:
        return [None]

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


def _render_prompt_file(
    step: FlowStep,
    *,
    workspace: Path,
    prompt_file: Path,
    item: Any,
    iteration_id: str | None,
    conversation_item: Any,
    final_prompt: bool = False,
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
        if name not in step.variables:
            raise ValueError(
                f"Schritt {step.step_id!r} setzt Prompt-Variable {name!r} nicht."
            )
        raw = step.variables[name]
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
        )
    rendered = template.render(values)
    if not rendered or rendered.isspace():
        raise ValueError(
            f"Gerenderter Prompt von Schritt {step.step_id!r} darf nicht leer sein."
        )
    return rendered


def _prompt_for_iteration(
    step: FlowStep,
    *,
    workspace: Path,
    item: Any,
    iteration_id: str | None = None,
    conversation_item: Any = None,
) -> str:
    return _render_prompt_file(
        step,
        workspace=workspace,
        prompt_file=step.prompt_file,
        item=item,
        iteration_id=iteration_id,
        conversation_item=conversation_item,
    )


def _conversation_prompts_for_iteration(
    step: FlowStep,
    *,
    workspace: Path,
    outputs: dict[str, str],
    item: Any,
    iteration_id: str | None,
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
                final_prompt=True,
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
    path = Path(rendered)
    return _workspace_path(
        workspace,
        path,
        purpose=f"Output-Datei von Schritt {step.step_id!r}",
        must_exist=False,
    )


def validate_flow(
    flow: FlowDefinition,
    *,
    workspace: Path,
    config_loader: Callable[[Path | None], AppConfig] = load_config,
) -> None:
    workspace = workspace.expanduser().resolve()
    flow_dir = flow.source.parent
    resolve_excluded_paths(workspace, flow.excluded_paths)
    produced: set[str] = set()
    planned_static_outputs: dict[str, str] = {}

    for step in flow.steps:
        prompt_path = _workspace_path(
            workspace,
            step.prompt_file,
            purpose=f"Prompt-Datei von Schritt {step.step_id!r}",
            must_exist=True,
        )
        prompt = prepare_prompt_file(workspace, prompt_path)
        template = PromptTemplate.parse(prompt.content)
        templates = [template]
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
            templates.append(final_template)
            for name in final_template.variables:
                raw = step.variables.get(name)
                if raw is not None and _CONVERSATION_ITEM_EXPR.search(raw):
                    raise ValueError(
                        f"Finaler Conversation-Prompt von Schritt "
                        f"{step.step_id!r} kann Variable {name!r} mit "
                        "${conversation.item...} nicht verwenden."
                    )

        supplied = set(step.variables)
        required = {
            name
            for current_template in templates
            for name in current_template.variables
        }
        unknown = sorted(supplied - required)
        missing = sorted(required - supplied)
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
            config_loader(config_argument)
        except Exception as exc:
            raise ValueError(
                f"Konfiguration von Schritt {step.step_id!r} "
                f"ist ungültig: {exc}"
            ) from exc

        if step.foreach is None and step.output is not None:
            static_output = _output_for_iteration(
                step,
                workspace=workspace,
                item=None,
            )
            assert static_output is not None
            _prepare_flow_output(
                step,
                workspace=workspace,
                output=static_output,
            )
            static_output_key = _filesystem_path_key(static_output)
            previous_writer = planned_static_outputs.get(static_output_key)
            if previous_writer is not None and not step.overwrite_output:
                raise ValueError(
                    f"Output-Datei von Schritt {step.step_id!r} kollidiert "
                    f"mit dem geplanten Output von Schritt {previous_writer!r}; "
                    "der spätere Schritt erlaubt kein Überschreiben."
                )
            planned_static_outputs[static_output_key] = step.step_id

        if step.foreach is not None:
            match = _FOREACH.fullmatch(step.foreach)
            assert match is not None
            source_id = match.group(1)
            if source_id not in produced:
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

    reserved_inputs = _reserved_flow_input_paths(
        flow,
        workspace=workspace,
    )
    for step in flow.steps:
        if step.foreach is not None or step.output is None:
            continue
        static_output = _output_for_iteration(
            step,
            workspace=workspace,
            item=None,
        )
        assert static_output is not None
        reserved_input = reserved_inputs.get(
            _filesystem_path_key(static_output)
        )
        if reserved_input is not None:
            raise ValueError(
                f"Output-Datei von Schritt {step.step_id!r} kollidiert "
                "mit einer reservierten Flow-Eingabe: "
                f"{reserved_input}"
            )


async def run_flow(
    flow: FlowDefinition,
    *,
    workspace: Path,
    dependencies: ExecutionDependencies | None = None,
    approval_callback: ApprovalCallback | None = None,
) -> None:
    workspace = workspace.expanduser().resolve()
    deps = dependencies or ExecutionDependencies()
    validate_flow(
        flow,
        workspace=workspace,
        config_loader=deps.load_config,
    )
    flow_dir = flow.source.parent
    outputs: dict[str, str] = {}
    claimed_outputs: dict[str, str] = {}
    reserved_inputs = _reserved_flow_input_paths(
        flow,
        workspace=workspace,
    )
    (
        initial_checkpoint_fingerprints,
        initial_checkpoint_paths,
    ) = _snapshot_initial_checkpoints(
        flow,
        workspace=workspace,
    )
    mutation_protected_paths = tuple(
        dict.fromkeys(
            (
                *reserved_inputs.values(),
                *initial_checkpoint_paths,
            )
        )
    )
    case_colliding_step_ids = _case_colliding_step_ids(
        flow,
        workspace=workspace,
    )

    for step in flow.steps:
        items = _foreach_items(
            step,
            outputs=outputs,
        )
        iteration_ids = _iteration_ids(
            step,
            items=items,
        )

        preflight_outputs: list[Path | None] | None = None
        preflight_checkpoint_fingerprints: list[bytes | None] | None = None
        if step.foreach is not None and step.output is not None:
            preflight_outputs = [
                _output_for_iteration(
                    step,
                    workspace=workspace,
                    item=item,
                    iteration_id=iteration_id,
                )
                for item, iteration_id in zip(items, iteration_ids)
            ]
            output_keys = [
                _filesystem_path_key(path)
                for path in preflight_outputs
                if path is not None
            ]
            if len(output_keys) != len(set(output_keys)):
                raise ValueError(
                    f"Schritt {step.step_id!r} erzeugt für mehrere "
                    "foreach-Elemente nicht eindeutige Output-Pfade."
                )
            for output, iteration_id in zip(
                preflight_outputs,
                iteration_ids,
            ):
                assert output is not None
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
                _claim_output(
                    claimed_outputs,
                    path=output,
                    owner=_output_owner(
                        step,
                        iteration_id=iteration_id,
                    ),
                    allow_replace=step.overwrite_output,
                )

            preflight_checkpoint_fingerprints = []
            for output in preflight_outputs:
                assert output is not None
                expected_fingerprint = initial_checkpoint_fingerprints.get(
                    _checkpoint_snapshot_key(step, output)
                )
                if expected_fingerprint is not None:
                    _verified_preflight_checkpoint(
                        step,
                        workspace=workspace,
                        output=output,
                        expected_fingerprint=expected_fingerprint,
                    )
                else:
                    prepare_output_target(
                        workspace,
                        output,
                        overwrite=step.overwrite_output,
                    )
                preflight_checkpoint_fingerprints.append(
                    expected_fingerprint
                )

        iteration_answers: list[str] = []
        foreach_parts: list[str] = []
        foreach_payload_bytes = 0

        def record_iteration_answer(
            answer: str,
            iteration_id: str | None,
        ) -> None:
            nonlocal foreach_payload_bytes
            if step.foreach is None:
                iteration_answers.append(answer)
                return
            assert iteration_id is not None
            foreach_payload_bytes = _append_foreach_iteration(
                step,
                parts=foreach_parts,
                payload_bytes=foreach_payload_bytes,
                iteration_id=iteration_id,
                answer=answer,
            )

        for index, (item, iteration_id) in enumerate(
            zip(items, iteration_ids),
            start=1,
        ):
            suffix = (
                f" [{index}/{len(items)}; id={iteration_id}]"
                if step.foreach is not None
                else ""
            )
            print(
                sanitize_terminal_text(
                    f"Flow-Schritt {step.step_id}{suffix}",
                    multiline=False,
                    escape_invisible_formatting=True,
                    escape_literal_backslashes=True,
                )
            )

            output = (
                preflight_outputs[index - 1]
                if preflight_outputs is not None
                else _output_for_iteration(
                    step,
                    workspace=workspace,
                    item=item,
                    iteration_id=iteration_id,
                )
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
                        owner=_output_owner(
                            step,
                            iteration_id=iteration_id,
                        ),
                        allow_replace=step.overwrite_output,
                    )

            if preflight_checkpoint_fingerprints is not None:
                expected_fingerprint = preflight_checkpoint_fingerprints[index - 1]
            else:
                expected_fingerprint = (
                    initial_checkpoint_fingerprints.get(
                        _checkpoint_snapshot_key(step, output)
                    )
                    if output is not None
                    else None
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
                    prepare_output_target(
                        workspace,
                        output,
                        overwrite=step.overwrite_output,
                    )

            if checkpoint is not None:
                print(
                    sanitize_terminal_text(
                        f"Flow-Schritt {step.step_id}{suffix}: "
                        f"vorhandenen JSON-Checkpoint verwendet: {output}",
                        multiline=False,
                        escape_invisible_formatting=True,
                        escape_literal_backslashes=True,
                    )
                )
                record_iteration_answer(
                    checkpoint,
                    iteration_id,
                )
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
                fallback=approval_callback,
            )
            excluded_paths = (
                tuple(
                    dict.fromkeys(
                        (
                            *flow.excluded_paths,
                            *step.excluded_paths,
                        )
                    )
                )
                if step.workspace_access in {"read", "write"}
                else ()
            )
            dump_file_prefix = _dump_prefix_for_iteration(
                step,
                index=index,
                case_colliding_step_ids=case_colliding_step_ids,
                iteration_id=(
                    iteration_id
                    if step.iteration_id is not None
                    else None
                ),
            )

            conversation_prompts = _conversation_prompts_for_iteration(
                step,
                workspace=workspace,
                outputs=outputs,
                item=item,
                iteration_id=iteration_id,
            )
            if conversation_prompts is None:
                prompt = _prompt_for_iteration(
                    step,
                    workspace=workspace,
                    item=item,
                    iteration_id=iteration_id,
                )
                result = await run_once(
                    OneShotRunOptions(
                        workspace=workspace,
                        prompt=prompt,
                        config_file=config_file,
                        model=step.model,
                        workspace_access=step.workspace_access,
                        retry_policy=step.retry_policy,
                        response_format=step.response_format,
                        context_files=contexts,
                        add_web_context=step.add_web_context,
                        output=output,
                        overwrite_output=step.overwrite_output,
                        approval_callback=callback,
                        mutation_protected_paths=mutation_protected_paths,
                        excluded_paths=excluded_paths,
                        dump_file_prefix=dump_file_prefix,
                    ),
                    dependencies=deps,
                )
            else:
                result = await run_conversation(
                    ConversationRunOptions(
                        workspace=workspace,
                        prompts=conversation_prompts,
                        config_file=config_file,
                        model=step.model,
                        workspace_access=step.workspace_access,
                        retry_policy=step.retry_policy,
                        response_format=step.response_format,
                        context_files=contexts,
                        add_web_context=step.add_web_context,
                        output=output,
                        overwrite_output=step.overwrite_output,
                        approval_callback=callback,
                        mutation_protected_paths=mutation_protected_paths,
                        excluded_paths=excluded_paths,
                        dump_file_prefix=dump_file_prefix,
                    ),
                    dependencies=deps,
                )
            record_iteration_answer(
                result.answer,
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
            outputs[step.step_id] = iteration_answers[0]
        else:
            outputs[step.step_id] = _finish_foreach_output(
                foreach_parts
            )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli-agent-flow",
        description=(
            "Run deterministic multi-step cli-agent workflows "
            "in one fixed workspace."
        ),
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )
    for name in ("validate", "run"):
        sub = subparsers.add_parser(name)
        sub.add_argument("flow", type=Path)
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


def main() -> None:
    args = build_parser().parse_args()
    workspace = args.workspace.expanduser().resolve()
    try:
        flow = load_flow(
            args.flow,
            workspace=workspace,
        )
        validate_flow(
            flow,
            workspace=workspace,
        )
        if args.command == "validate":
            print(
                f"Flow gültig: {flow.source} "
                f"({len(flow.steps)} Schritte, "
                f"Workspace: {workspace})"
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
        print(
            "Fehler: "
            + sanitize_terminal_text(
                f"{type(exc).__name__}: {exc}",
                multiline=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
