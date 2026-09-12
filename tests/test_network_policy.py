from __future__ import annotations

import pytest

from cli_agent.network_policy import NetworkConfig, validate_http_url


def test_default_network_policy_is_local_only() -> None:
    policy = NetworkConfig()

    assert policy.model_allowed_hosts == ("localhost", "127.0.0.1", "::1")
    assert policy.mcp_allowed_hosts == policy.model_allowed_hosts
    assert policy.web_allowed_hosts == ()


def test_validate_http_url_requires_exact_host_allowlist() -> None:
    assert (
        validate_http_url(
            "HTTPS://LLM.INTERNAL.EXAMPLE./v1",
            allowed_hosts=("llm.internal.example",),
            purpose="Modell",
        )
        == "HTTPS://LLM.INTERNAL.EXAMPLE./v1"
    )

    with pytest.raises(ValueError, match="nicht erlaubt"):
        validate_http_url(
            "https://llm.internal.example.evil.test/v1",
            allowed_hosts=("llm.internal.example",),
            purpose="Modell",
        )


def test_validate_http_url_rejects_credentials() -> None:
    with pytest.raises(ValueError, match="Credentials"):
        validate_http_url(
            "http://user:password@localhost:11434",
            allowed_hosts=("localhost",),
            purpose="Modell",
        )


def test_validate_http_url_requires_tls_for_remote_hosts() -> None:
    with pytest.raises(ValueError, match="Unverschlüsseltes HTTP"):
        validate_http_url(
            "http://llm.internal:8000/v1",
            allowed_hosts=("llm.internal",),
            purpose="Modell",
        )
