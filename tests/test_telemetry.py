"""Optional OpenTelemetry configuration and safe instrumentation tests."""
from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from cli_agent.settings.config import TelemetryConfig, _parse_config_values
from cli_agent import telemetry


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
    monkeypatch.setattr(telemetry, "_active_config", None)
    telemetry.configure_telemetry(TelemetryConfig())
    with telemetry.span("agent.ask", prompt="sensitive") as current:
        assert current is None
        telemetry.set_attribute(current, "prompt", "sensitive")
        telemetry.mark_error(current, "error")


def test_async_decorators_preserve_calls_without_sdk(monkeypatch):
    monkeypatch.setattr(telemetry, "_active_config", None)

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
