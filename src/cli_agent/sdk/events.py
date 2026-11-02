"""Structured SDK events and an opt-in, safe console presenter.

No MCP results, tool arguments or credentials are included in these events.
Approval callbacks receive arguments separately through the existing trusted
Python callback interface.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Literal, TextIO


@dataclass(frozen=True)
class AgentEvent:
    type: str
    run_id: str
    tool: str | None = None
    call_id: str | None = None
    status: str | None = None
    phase: str | None = None
    duration_ms: float | None = None


@dataclass(frozen=True)
class ToolCallRecord:
    name: str
    call_id: str
    status: str
    duration_ms: float | None = None


def default_event_callback(
    event: AgentEvent,
    *,
    verbose: bool = False,
    stream: TextIO | None = None,
) -> None:
    """Render safe, concise progress to stdout (or an explicitly given stream).

    Never prints tool inputs, tool results, raw model messages, or credentials.
    This is a synchronous display callback; the SDK invokes it from a separate
    event-consumer task, not from the MCP-owning worker.
    """
    output = stream if stream is not None else sys.stdout
    label: str | None = None
    if event.type == "run_started":
        label = "● Anfrage gestartet"
    elif event.type == "run_finished":
        label = f"✓ Anfrage abgeschlossen ({event.status or 'success'})"
    elif event.type == "model_started":
        if verbose:
            label = f"● Modellanfrage gestartet ({event.phase or 'main'})"
        else:
            label = "● Modellanfrage gestartet"
    elif event.type == "model_finished":
        label = f"✓ Modellaufruf: {event.status or 'finished'}"
    elif event.type == "tool_started":
        label = f"→ {event.tool or 'Tool'}"
    elif event.type == "tool_finished":
        duration = (
            f" ({event.duration_ms / 1000:.2f} s)"
            if event.duration_ms is not None else ""
        )
        label = f"✓ {event.tool or 'Tool'}: {event.status or 'finished'}{duration}"
    elif event.type == "approval_requested":
        label = f"? Freigabe erforderlich: {event.tool or 'Tool'}"
    elif event.type == "approval_decided":
        label = f"  Freigabe {event.status or 'unknown'}: {event.tool or 'Tool'}"
    elif verbose and event.type in {"sub_run_started", "sub_run_finished"}:
        label = f"  {event.type}: {event.status or 'running'}"
    if label is not None:
        print(label, file=output, flush=True)
