"""Opt-in, metadata-only OpenTelemetry spans for the local agent runtime.

No prompt, MCP arguments/results, paths, API keys or full exception strings are
attached to spans. The collector endpoint is loopback-only by policy.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from functools import wraps
from typing import Any, Iterator

from cli_agent.settings.config import TelemetryConfig

logger = logging.getLogger("cli_agent.telemetry")
_active_config: TelemetryConfig | None = None


def configure_telemetry(config: TelemetryConfig) -> None:
    """Initialize the process-wide OTLP trace provider once, when opted in."""
    global _active_config
    if not config.enabled:
        return
    if _active_config is not None:
        if _active_config != config:
            raise RuntimeError(
                "OpenTelemetry is already initialized with a different configuration "
                "in this process."
            )
        return
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError as exc:
        raise RuntimeError(
            "Telemetry is enabled; install cli-agent[telemetry] to use OTLP tracing."
        ) from exc

    provider = TracerProvider(
        resource=Resource.create({"service.name": config.service_name}),
        sampler=ParentBased(TraceIdRatioBased(config.sample_rate)),
    )
    exporter = OTLPSpanExporter(
        endpoint=config.otlp_endpoint.rstrip("/") + "/v1/traces",
        timeout=5,
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _active_config = config


def _safe_attribute(value: Any) -> str | int | float | bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[:128]
    return None


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Start a span without requiring OpenTelemetry when disabled."""
    if _active_config is None:
        yield None
        return
    from opentelemetry import trace

    with trace.get_tracer("cli_agent").start_as_current_span(
        name, record_exception=False, set_status_on_exception=False,
    ) as current:
        for key, raw in attributes.items():
            value = _safe_attribute(raw)
            if value is not None:
                current.set_attribute(key.replace("_", "."), value)
        try:
            yield current
        except BaseException as exc:
            mark_error(current, type(exc).__name__)
            raise


def set_attribute(current: Any, name: str, value: Any) -> None:
    if current is not None:
        safe = _safe_attribute(value)
        if safe is not None:
            current.set_attribute(name, safe)


def mark_error(current: Any, reason: str) -> None:
    """Mark a non-exception failure without exporting its payload."""
    if current is not None:
        from opentelemetry.trace.status import Status, StatusCode

        current.set_status(Status(StatusCode.ERROR, reason[:128]))


def trace_flow_step(kind: str):
    """Wrap a flow step; its immutable identifiers are the only attributes."""
    def decorate(function):
        @wraps(function)
        async def wrapped(step, *args, **kwargs):
            with span("flow.step", step_id=step.step_id, step_kind=kind,
                      namespace=kwargs.get("namespace", "")):
                return await function(step, *args, **kwargs)
        return wrapped
    return decorate


def trace_flow_operation(kind: str):
    """Wrap a synchronous flow step such as JSON or filesystem operations."""
    def decorate(function):
        @wraps(function)
        def wrapped(step, *args, **kwargs):
            with span("flow.step", step_id=step.step_id, step_kind=kind,
                      namespace=kwargs.get("namespace", "")):
                return function(step, *args, **kwargs)
        return wrapped
    return decorate


def trace_flow_invocation(function):
    """Preserve one parent span for a flow or nested subflow."""
    @wraps(function)
    async def wrapped(flow, *args, **kwargs):
        with span("flow.invocation", flow_name=flow.source.name,
                  namespace=kwargs.get("namespace", "")):
            return await function(flow, *args, **kwargs)
    return wrapped


def traced_agent_question(function):
    """One task trace; do not attach the user question."""
    @wraps(function)
    async def wrapped(self, prompt, *args, **kwargs):
        with span("agent.ask"):
            return await function(self, prompt, *args, **kwargs)
    return wrapped


def traced_model_loop(function):
    """Group all LLM and tool rounds of one model-loop invocation."""
    @wraps(function)
    async def wrapped(agent, *args, **kwargs):
        with span("agent.model_loop", phase=kwargs.get("phase", "unknown")):
            return await function(agent, *args, **kwargs)
    return wrapped


def traced_sub_run(function):
    """Sub-run remains a child of the coordinator model-loop trace."""
    @wraps(function)
    async def wrapped(agent, args, *other, **kwargs):
        with span("agent.sub_run") as current:
            result = await function(agent, args, *other, **kwargs)
            status = result.get("status") if isinstance(result, dict) else None
            if status in {"blocked", "failed"}:
                mark_error(current, "sub_run_" + status)
            set_attribute(current, "sub_run.status", status)
            return result
    return wrapped
