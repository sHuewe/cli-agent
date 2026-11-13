"""Opt-in, metadata-only OpenTelemetry spans for the local agent runtime.

No prompt, MCP arguments/results, paths, API keys or full exception strings are
attached to spans. The collector endpoint is loopback-only by policy.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict
from threading import Lock
from functools import wraps
from typing import Any, Iterator

from cli_agent.settings.config import TelemetryConfig, _telemetry_config

logger = logging.getLogger("cli_agent.telemetry")
# A tracer selected by the current logical execution context, never by the
# process-global OpenTelemetry provider. Disabled runs explicitly select None.
_tracer_context: ContextVar[Any | None] = ContextVar("cli_agent_tracer", default=None)
_provider_lock = Lock()
_providers: dict[TelemetryConfig, Any] = {}


def _tracer_for(config: TelemetryConfig) -> Any:
    """Return an SDK tracer owned by cli-agent; do not mutate OTel globals."""
    # TelemetryConfig can also originate from SDK programmatic overrides.
    # Recheck it before creating a network exporter, not only while parsing TOML.
    validated = _telemetry_config(asdict(config))
    if validated != config or not validated.enabled:
        raise ValueError("Telemetry requires a validated, enabled configuration.")
    with _provider_lock:
        provider = _providers.get(config)
        if provider is None:
            try:
                from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
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
                endpoint=config.otlp_endpoint,
                insecure=config.otlp_endpoint.startswith("http://"),
                timeout=5,
                headers={},
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            _providers[config] = provider
        return provider.get_tracer("cli_agent")


@contextmanager
def telemetry_scope(config: TelemetryConfig) -> Iterator[None]:
    """Select tracing only for this run/task, restoring the previous scope."""
    tracer = _tracer_for(config) if config.enabled else None
    token = _tracer_context.set(tracer)
    try:
        yield
    finally:
        _tracer_context.reset(token)


def configure_telemetry(config: TelemetryConfig) -> None:
    """Eagerly validate an enabled export without activating another run.

    A disabled configuration does nothing. Each agent/flow uses telemetry_scope
    independently, so a previous enabled run cannot opt in subsequent runs.
    """
    if config.enabled:
        _tracer_for(config)


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
    tracer = _tracer_context.get()
    if tracer is None:
        yield None
        return

    with tracer.start_as_current_span(
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
        with telemetry_scope(getattr(self, "telemetry_config", TelemetryConfig())):
            with span("agent.ask"):
                return await function(self, prompt, *args, **kwargs)
    return wrapped

def traced_agent_resume(function):
    """Resume interrupted frontend tool calls in their original agent scope."""
    @wraps(function)
    async def wrapped(self, *args, **kwargs):
        with telemetry_scope(getattr(self, "telemetry_config", TelemetryConfig())):
            with span("agent.resume"):
                return await function(self, *args, **kwargs)
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
