from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

from .config import default_config_file
from .file_context import prepare_prompt_file
from .filesystem_security import path_entry_is_symlink_or_reparse
from .prompt_template import PromptTemplate

MAX_FLOW_STEPS = 100
MAX_FOREACH_ITEMS = 1000
MAX_FLOW_FILE_BYTES = 1_000_000
MAX_STRUCTURED_OUTPUT_BYTES = 10_000_000
_STEP_ID = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*\Z")
_ITEM_EXPR = re.compile(r"\$\{item(?:\.([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?\}")
_FOREACH = re.compile(
    r"steps\.([A-Za-z_][A-Za-z0-9_-]*)\.output(?:\.([A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*))?\Z"
)


@dataclass(frozen=True)
class FlowStep:
    step_id: str
    config: Path | None
    prompt_file: Path
    context_file: Path | None
    output: str | None
    overwrite_output: bool
    os_access: str | None
    variables: dict[str, str]
    foreach: str | None


@dataclass(frozen=True)
class FlowDefinition:
    source: Path
    steps: tuple[FlowStep, ...]


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
        raise ValueError(f"{purpose} verwendet ein nicht unterstütztes Windows-Laufwerk.")

    lexical = raw if raw.is_absolute() else workspace / raw
    if path_entry_is_symlink_or_reparse(lexical):
        raise ValueError(f"{purpose} darf kein Symlink oder Reparse Point sein: {lexical}")

    try:
        resolved = lexical.resolve(strict=must_exist)
    except (OSError, RuntimeError) as exc:
        raise ValueError(f"{purpose} konnte nicht aufgelöst werden: {value}: {exc}") from exc
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValueError(f"{purpose} muss innerhalb des festen Workspace liegen: {value}") from exc
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


def _string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} muss ein nichtleerer String sein.")
    return value


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
                f"Flow-Datei überschreitet das Limit von {MAX_FLOW_FILE_BYTES} Bytes."
            )
        values = tomllib.loads(raw_flow.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError("Flow-Datei ist nicht als UTF-8 lesbar.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Flow-Datei enthält ungültiges TOML: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"Flow-Datei konnte nicht gelesen werden: {exc}") from exc

    allowed_root = {"version", "steps"}
    unknown_root = set(values) - allowed_root
    if unknown_root:
        raise ValueError(
            "Unbekannte Flow-Schlüssel: " + ", ".join(sorted(unknown_root))
        )
    if values.get("version") != 1:
        raise ValueError("Flow-Datei benötigt version = 1.")

    raw_steps = values.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise ValueError("Flow-Datei benötigt mindestens einen [[steps]]-Eintrag.")
    if len(raw_steps) > MAX_FLOW_STEPS:
        raise ValueError(f"Flow enthält mehr als {MAX_FLOW_STEPS} Schritte.")

    steps: list[FlowStep] = []
    known_ids: set[str] = set()
    allowed_step = {
        "id",
        "config",
        "prompt_file",
        "context_file",
        "output",
        "overwrite_output",
        "os_access",
        "vars",
        "foreach",
    }

    for index, raw in enumerate(raw_steps, start=1):
        if not isinstance(raw, dict):
            raise ValueError(f"Flow-Schritt {index} muss eine Tabelle sein.")
        unknown = set(raw) - allowed_step
        if unknown:
            raise ValueError(
                f"Flow-Schritt {index} enthält unbekannte Schlüssel: "
                + ", ".join(sorted(unknown))
            )

        step_id = _string(raw.get("id"), field=f"steps[{index}].id")
        if not _STEP_ID.fullmatch(step_id):
            raise ValueError(
                f"Ungültige Step-ID {step_id!r}; erlaubt sind ASCII-Buchstaben, "
                "Ziffern, '_' und '-'."
            )
        if step_id in known_ids:
            raise ValueError(f"Doppelte Step-ID: {step_id!r}.")
        known_ids.add(step_id)

        prompt_file = Path(
            _string(raw.get("prompt_file"), field=f"steps[{index}].prompt_file")
        )
        context_value = raw.get("context_file")
        context_file = (
            Path(_string(context_value, field=f"steps[{index}].context_file"))
            if context_value is not None
            else None
        )
        config_value = raw.get("config")
        config = (
            Path(_string(config_value, field=f"steps[{index}].config"))
            if config_value is not None
            else None
        )

        output_value = raw.get("output")
        output = (
            _string(output_value, field=f"steps[{index}].output")
            if output_value is not None
            else None
        )
        overwrite_output = raw.get("overwrite_output", False)
        if not isinstance(overwrite_output, bool):
            raise ValueError(f"steps[{index}].overwrite_output muss boolesch sein.")

        os_access_value = raw.get("os_access")
        if os_access_value is None or os_access_value == "none":
            os_access = None
        elif os_access_value in {"read", "write"}:
            os_access = str(os_access_value)
        else:
            raise ValueError(
                f"steps[{index}].os_access muss 'none', 'read' oder 'write' sein."
            )

        raw_vars = raw.get("vars", {})
        if not isinstance(raw_vars, dict):
            raise ValueError(f"steps[{index}].vars muss eine Tabelle sein.")
        variables: dict[str, str] = {}
        for name, value in raw_vars.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError(
                    f"steps[{index}].vars darf nur String-zu-String-Werte enthalten."
                )
            variables[name] = value

        foreach_value = raw.get("foreach")
        foreach = (
            _string(foreach_value, field=f"steps[{index}].foreach")
            if foreach_value is not None
            else None
        )
        if foreach is not None:
            match = _FOREACH.fullmatch(foreach)
            if match is None:
                raise ValueError(
                    f"steps[{index}].foreach muss auf steps.<id>.output[.<pfad>] verweisen."
                )
            source_id = match.group(1)
            if source_id not in known_ids:
                raise ValueError(
                    f"steps[{index}].foreach darf nur auf einen vorherigen Schritt verweisen: "
                    f"{source_id!r}."
                )
        else:
            dynamic_values = list(variables.values())
            if output is not None:
                dynamic_values.append(output)
            if any(_ITEM_EXPR.search(value) for value in dynamic_values):
                raise ValueError(
                    f"steps[{index}] verwendet ${{item...}} ohne foreach."
                )

        steps.append(
            FlowStep(
                step_id=step_id,
                config=config,
                prompt_file=prompt_file,
                context_file=context_file,
                output=output,
                overwrite_output=overwrite_output,
                os_access=os_access,
                variables=variables,
                foreach=foreach,
            )
        )

    return FlowDefinition(source=source, steps=tuple(steps))


def _lookup(value: Any, path: str | None, *, label: str) -> Any:
    current = value
    if path:
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                raise ValueError(f"{label} enthält kein Feld {part!r}.")
            current = current[part]
    return current


def _render_item_text(template: str, item: Any) -> str:
    def replace(match: re.Match[str]) -> str:
        path = match.group(1)
        value = _lookup(item, path, label="foreach-Element") if path else item
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return _ITEM_EXPR.sub(replace, template)


def _parse_step_output(path: Path, *, step_id: str) -> Any:
    try:
        with path.open("rb") as handle:
            raw_bytes = handle.read(MAX_STRUCTURED_OUTPUT_BYTES + 1)
        if len(raw_bytes) > MAX_STRUCTURED_OUTPUT_BYTES:
            raise ValueError(
                f"Output von Schritt {step_id!r} überschreitet das JSON-Limit von "
                f"{MAX_STRUCTURED_OUTPUT_BYTES} Bytes."
            )
        raw = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            f"Output von Schritt {step_id!r} ist nicht als UTF-8 lesbar."
        ) from exc
    except OSError as exc:
        raise ValueError(
            f"Output von Schritt {step_id!r} konnte nicht gelesen werden: {exc}"
        ) from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Output von Schritt {step_id!r} muss für foreach gültiges JSON sein: {exc}"
        ) from exc


def _foreach_items(
    step: FlowStep,
    *,
    outputs: dict[str, tuple[Path, Any]],
) -> list[Any]:
    if step.foreach is None:
        return [None]
    match = _FOREACH.fullmatch(step.foreach)
    assert match is not None
    source_id, path = match.groups()
    if source_id not in outputs:
        raise ValueError(
            f"foreach-Quelle {source_id!r} besitzt keinen verfügbaren Output."
        )
    source_path, parsed = outputs[source_id]
    if parsed is None:
        parsed = _parse_step_output(source_path, step_id=source_id)
        outputs[source_id] = (source_path, parsed)
    items = _lookup(parsed, path, label=f"Output von Schritt {source_id!r}")
    if not isinstance(items, list):
        raise ValueError(f"foreach-Quelle {step.foreach!r} ist keine Liste.")
    if len(items) > MAX_FOREACH_ITEMS:
        raise ValueError(
            f"foreach-Quelle enthält {len(items)} Elemente; erlaubt sind höchstens "
            f"{MAX_FOREACH_ITEMS}."
        )
    return items


def _resolve_config(step: FlowStep, *, flow_dir: Path) -> Path:
    if step.config is None:
        return default_config_file()
    path = step.config.expanduser()
    return path if path.is_absolute() else (flow_dir / path)


def _step_command(
    step: FlowStep,
    *,
    workspace: Path,
    flow_dir: Path,
    item: Any,
) -> tuple[list[str], Path | None]:
    prompt = _workspace_path(
        workspace,
        flow_dir / step.prompt_file if not step.prompt_file.is_absolute() else step.prompt_file,
        purpose=f"Prompt-Datei von Schritt {step.step_id!r}",
        must_exist=True,
    )
    context: Path | None = None
    if step.context_file is not None:
        context = _workspace_path(
            workspace,
            flow_dir / step.context_file if not step.context_file.is_absolute() else step.context_file,
            purpose=f"Context-Datei von Schritt {step.step_id!r}",
            must_exist=True,
        )

    command = [
        sys.executable,
        "-m",
        "cli_agent.cli",
        "--workspace",
        str(workspace),
        "--config",
        str(_resolve_config(step, flow_dir=flow_dir)),
        "--prompt-file",
        str(prompt),
    ]
    if context is not None:
        command.extend(["--context-file", str(context)])
    if step.os_access == "read":
        command.append("--with-os-read")
    elif step.os_access == "write":
        command.append("--with-os-write")

    for name, template in step.variables.items():
        rendered = _render_item_text(template, item) if item is not None else template
        command.extend(["--var", f"{name}={rendered}"])

    output_path: Path | None = None
    if step.output is not None:
        rendered_output = (
            _render_item_text(step.output, item) if item is not None else step.output
        )
        output_path = _workspace_path(
            workspace,
            flow_dir / Path(rendered_output)
            if not Path(rendered_output).is_absolute()
            else Path(rendered_output),
            purpose=f"Output-Datei von Schritt {step.step_id!r}",
            must_exist=False,
        )
        command.extend(["--output", str(output_path)])
        if step.overwrite_output:
            command.append("--overwrite-output")

    return command, output_path


def validate_flow(flow: FlowDefinition, *, workspace: Path) -> None:
    flow_dir = flow.source.parent
    produced: set[str] = set()

    for step in flow.steps:
        prompt_path = _workspace_path(
            workspace,
            flow_dir / step.prompt_file
            if not step.prompt_file.is_absolute()
            else step.prompt_file,
            purpose=f"Prompt-Datei von Schritt {step.step_id!r}",
            must_exist=True,
        )
        prompt = prepare_prompt_file(workspace, prompt_path)
        template = PromptTemplate.parse(prompt.content)
        supplied = set(step.variables)
        required = set(template.variables)
        unknown = sorted(supplied - required)
        missing = [name for name in template.variables if name not in supplied]
        if unknown:
            raise ValueError(
                f"Schritt {step.step_id!r} setzt unbekannte Prompt-Variable(n): "
                + ", ".join(unknown)
            )
        if missing:
            raise ValueError(
                f"Schritt {step.step_id!r} setzt nicht alle Prompt-Variablen: "
                + ", ".join(missing)
            )
        if step.context_file is not None:
            _workspace_path(
                workspace,
                flow_dir / step.context_file
                if not step.context_file.is_absolute()
                else step.context_file,
                purpose=f"Context-Datei von Schritt {step.step_id!r}",
                must_exist=True,
            )
        config = _resolve_config(step, flow_dir=flow_dir).expanduser()
        if not config.exists():
            raise ValueError(
                f"Konfiguration von Schritt {step.step_id!r} existiert nicht: {config}"
            )
        if step.foreach is not None:
            source_id = _FOREACH.fullmatch(step.foreach).group(1)  # type: ignore[union-attr]
            if source_id not in produced:
                raise ValueError(
                    f"foreach-Quelle {source_id!r} ist nicht vor Schritt {step.step_id!r} verfügbar."
                )
        if step.output is not None:
            produced.add(step.step_id)


def run_flow(flow: FlowDefinition, *, workspace: Path) -> None:
    workspace = workspace.expanduser().resolve()
    validate_flow(flow, workspace=workspace)
    flow_dir = flow.source.parent
    outputs: dict[str, tuple[Path, Any]] = {}

    for step in flow.steps:
        items = _foreach_items(step, outputs=outputs)
        if step.foreach is not None and step.output is not None and len(items) > 1:
            rendered_paths = {
                _render_item_text(step.output, item)
                for item in items
            }
            if len(rendered_paths) != len(items):
                raise ValueError(
                    f"Schritt {step.step_id!r} erzeugt für mehrere foreach-Elemente "
                    "nicht eindeutige Output-Pfade."
                )

        last_output: Path | None = None
        for index, item in enumerate(items, start=1):
            suffix = f" [{index}/{len(items)}]" if step.foreach is not None else ""
            print(f"Flow-Schritt {step.step_id}{suffix}")
            command, output_path = _step_command(
                step,
                workspace=workspace,
                flow_dir=flow_dir,
                item=item,
            )
            completed = subprocess.run(command, cwd=workspace, check=False)
            if completed.returncode != 0:
                raise RuntimeError(
                    f"Flow-Schritt {step.step_id!r}{suffix} ist mit Exit-Code "
                    f"{completed.returncode} fehlgeschlagen."
                )
            last_output = output_path

        if step.foreach is None and last_output is not None:
            outputs[step.step_id] = (last_output, None)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli-agent-flow",
        description="Run deterministic multi-step cli-agent workflows in one fixed workspace.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "run"):
        sub = subparsers.add_parser(name)
        sub.add_argument("flow", type=Path)
        sub.add_argument(
            "--workspace",
            type=Path,
            default=Path.cwd(),
            help="Fixed workspace for the complete flow (default: current directory).",
        )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    workspace = args.workspace.expanduser().resolve()
    try:
        flow = load_flow(args.flow, workspace=workspace)
        validate_flow(flow, workspace=workspace)
        if args.command == "validate":
            print(
                f"Flow gültig: {flow.source} ({len(flow.steps)} Schritte, "
                f"Workspace: {workspace})"
            )
            return
        run_flow(flow, workspace=workspace)
    except Exception as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
