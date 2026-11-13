"""Optional OpenTelemetry configuration and safe instrumentation tests."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager

import pytest

from cli_agent.settings.config import TelemetryConfig, _parse_config_values
from cli_agent.platform import telemetry


def test_telemetry_disabled_by_default():
    assert _parse_config_values({}).telemetry.enabled is False
    assert _parse_config_values({}).telemetry.otlp_endpoint == "http://127.0.0.1:4317"


@pytest.mark.parametrize("endpoint", [
    "https://example.org:4318", "http://192.168.1.1:4318",
    "http://localhost:4318/other", "http://localhost:4318?foo=bar",
    "http://user:password@localhost:4318", "file:///tmp/trace",
    "http://localhost", "http://localhost:0",
    "http://localhost:99999", "http://localhost:4318\\evil",
])
def test_telemetry_rejects_non_local_or_malformed_endpoints(endpoint):
    with pytest.raises(ValueError, match="telemetry.otlp_endpoint"):
        _parse_config_values({"telemetry": {"enabled": True, "otlp_endpoint": endpoint}})


@pytest.mark.parametrize("values", [
    {"enabled": "yes"},
    {"sample_rate": -1},
    {"sample_rate": 1.1},
    {"sample_rate": True},
    {"service_name": ""},
    {"service_name": "bad service"},
    {"unknown": 5},
])
def test_telemetry_rejects_invalid_configuration(values):
    with pytest.raises(ValueError):
        _parse_config_values({"telemetry": values})


def test_valid_local_collector_configuration():
    config = _parse_config_values({"telemetry": {
        "enabled": True,
        "service_name": "my-agent",
        "otlp_endpoint": "http://[::1]:4318",
        "sample_rate": 0.25,
    }}).telemetry
    assert config.enabled is True
    assert config.sample_rate == 0.25
    assert config.otlp_endpoint == "http://[::1]:4318"


def test_disabled_telemetry_does_not_import_sdk(monkeypatch):
    telemetry.configure_telemetry(TelemetryConfig())
    with telemetry.span("agent.ask", prompt="sensitive") as current:
        assert current is None
        telemetry.set_attribute(current, "prompt", "sensitive")
        telemetry.mark_error(current, "error")


def test_async_decorators_preserve_calls_without_sdk(monkeypatch):

    @telemetry.traced_model_loop
    async def run_loop(agent, *, phase):
        return phase

    @telemetry.traced_sub_run
    async def sub_run(agent, args):
        return {"status": "completed"}

    class Flow:
        source = type("Source", (), {"name": "flow.toml"})()

    @telemetry.trace_flow_invocation
    async def run_flow(flow, *, namespace):
        return namespace

    assert asyncio.run(run_loop(object(), phase="sub")) == "sub"
    assert asyncio.run(sub_run(object(), {})) == {"status": "completed"}
    assert asyncio.run(run_flow(Flow(), namespace="child/")) == "child/"


def test_local_endpoint_is_required_even_when_disabled():
    with pytest.raises(ValueError, match="telemetry.otlp_endpoint"):
        _parse_config_values({"telemetry": {
            "enabled": False, "otlp_endpoint": "http://remote.example:4318"
        }})


class _FakeSpan:
    def __init__(self, name):
        self.name = name
        self.attributes = {}
        self.status = None

    def set_attribute(self, key, value):
        self.attributes[key] = value

    def set_status(self, status):
        self.status = status


class _FakeTracer:
    def __init__(self):
        self.events = []

    @contextmanager
    def start_as_current_span(self, name, **kwargs):
        self.events.append(name)
        yield _FakeSpan(name)


def test_disabled_run_after_enabled_does_not_export(monkeypatch):
    tracer = _FakeTracer()
    monkeypatch.setattr(telemetry, "_tracer_for", lambda config: tracer)
    enabled = TelemetryConfig(enabled=True)
    disabled = TelemetryConfig(enabled=False)
    with telemetry.telemetry_scope(enabled):
        with telemetry.span("enabled"):
            pass
    with telemetry.telemetry_scope(disabled):
        with telemetry.span("disabled") as current:
            assert current is None
    assert tracer.events == ["enabled"]


def test_nested_disabled_scope_and_parallel_runs_are_isolated(monkeypatch):
    tracers = {"agent-one": _FakeTracer(), "agent-two": _FakeTracer()}
    monkeypatch.setattr(
        telemetry, "_tracer_for", lambda config: tracers[config.service_name],
    )

    async def run(config, name):
        with telemetry.telemetry_scope(config):
            with telemetry.span(name):
                await asyncio.sleep(0)
                with telemetry.telemetry_scope(TelemetryConfig()):
                    with telemetry.span("disabled") as current:
                        assert current is None
                with telemetry.span(name + ".child"):
                    pass

    async def parallel():
        await asyncio.gather(
            run(TelemetryConfig(enabled=True, service_name="agent-one"), "one"),
            run(TelemetryConfig(enabled=True, service_name="agent-two"), "two"),
        )

    asyncio.run(parallel())
    assert tracers["agent-one"].events == ["one", "one.child"]
    assert tracers["agent-two"].events == ["two", "two.child"]
    with telemetry.span("after-runs") as current:
        assert current is None


def test_agent_question_uses_its_own_configuration(monkeypatch):
    enabled_tracer = _FakeTracer()
    monkeypatch.setattr(telemetry, "_tracer_for", lambda _config: enabled_tracer)

    class Agent:
        telemetry_config = TelemetryConfig(enabled=True)

        @telemetry.traced_agent_question
        async def ask(self, prompt):
            with telemetry.span("child"):
                return "ok"

    async def scenario():
        agent = Agent()
        assert await agent.ask("do not export this prompt") == "ok"
        agent.telemetry_config = TelemetryConfig()
        assert await agent.ask("also private") == "ok"

    asyncio.run(scenario())
    assert enabled_tracer.events == ["agent.ask", "child"]


def test_locally_owned_tracer_does_not_modify_global_provider(monkeypatch):
    pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry import trace

    config = TelemetryConfig(enabled=True, service_name="isolated-jaeger",
                             sample_rate=0.0)
    monkeypatch.setattr(trace, "set_tracer_provider",
                        lambda *_: pytest.fail("must not modify global provider"))
    initial_provider = trace.get_tracer_provider()
    with telemetry.telemetry_scope(config):
        with telemetry.span("safe-span") as current:
            assert current is not None
    assert trace.get_tracer_provider() is initial_provider
    provider = telemetry._providers.pop(config)
    provider.shutdown()
